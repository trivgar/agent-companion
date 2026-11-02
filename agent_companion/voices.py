"""Whether spoken clips are on the real voices or the backup ones.

Remote voice hosts come only from ``remote-hosts.conf`` in the configured
coordinator directory, and the voices in use from ``remote-voices.conf``. No
host, address or local hardware name is built into the core. Every host that
serves a voice in use is asked for its ``/health`` at once, and the answer is
cached. A coordinator that renders on a remote host logs each clip to
``speak.log`` as ``YYYY-MM-DD HH:MM:SS REMOTE(voice[@host]) [FALLBACK ]...``,
FALLBACK when the clip went to the backup voices after all, and the newest
such line counts too.

The answer is backup voices while no host can take the clips, with each
host's reason, and a quiet "Voices on X only" line while one host carries
them all. The ``voice_host_names`` setting names a host where its bare name
reads wrong, and the add-on's ``voice_host_status`` gives a reason only its
owner knows, or marks a host resting (down on purpose), which the quiet line
leaves out.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from pathlib import Path

from . import addons, config

CACHE_S = 20
PROBE_S = 1.5
DEADLINE_S = PROBE_S + 0.3  # urlopen bounds each socket step, not the call
RECENT_S = 300
LOG_TAIL_BYTES = 40_000
_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) REMOTE\(([\w-]+)(?:@[\w-]+)?\) (FALLBACK )?")
_cache: dict[str, tuple[float, dict]] = {}
_flight = threading.Lock()  # one refresh at a time


def _rows(tts: Path) -> list[tuple[str, str, list[str]]]:
    """remote-hosts.conf's usable rows in order, as (name, url, the words after
    the url). Comments, blank lines and rows without an http(s) url are
    skipped, so a row's place here is its place among the hosts."""
    try:
        lines = (Path(tts) / "remote-hosts.conf").read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        name, separator, rest = line.split("#", 1)[0].strip().partition("=")
        name, parts = name.strip(), rest.replace(",", " ").split()
        if not separator or not name or not parts or not parts[0].startswith(("http://", "https://")):
            continue
        out.append((name[:80], parts[0], parts[1:]))
    return out


def hosts(tts: Path) -> list[tuple[str, str, set[str]]]:
    """``name=url [voice,...]`` rows, malformed ones ignored. The first host
    takes every voice ('*'); a later one only the voices listed after its url,
    where ``voice+``, that host's own version of the voice, is still that voice."""
    out: list[tuple[str, str, set[str]]] = []
    for name, url, words in _rows(tts):
        out.append((name, url, {"*"} if not out else {w.rstrip("+") for w in words if w.rstrip("+")}))
    return out


def own_versions(tts: Path) -> dict[str, str]:
    """{voice: host} for the voices a later host renders its own way (``voice+``)."""
    out: dict[str, str] = {}
    for number, (name, _url, words) in enumerate(_rows(tts)):
        if number:
            out.update({w[:-1]: name for w in words if w.endswith("+") and w[:-1]})
    return out


def wanted(tts: Path) -> set[str]:
    out = set()
    try:
        lines = (Path(tts) / "remote-voices.conf").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            voice = line.split("=", 1)[1].strip()
            if voice:
                out.add(voice)
    return out


def _names(host: str) -> dict:
    """The host's row in the ``voice_host_names`` setting, or nothing."""
    table = config.get("voice_host_names")
    row = table.get(host) if isinstance(table, dict) else None
    return row if isinstance(row, dict) else {}


def subject(host: str) -> str:
    """The host as a subject: its name, or the setting's ("the voice host on this PC")."""
    return str(_names(host).get("subject") or host)


def possessive(host: str) -> str:
    """The host's: "<name>'s", or the setting's ("this PC's")."""
    return str(_names(host).get("possessive") or f"{host}'s")


def place(host: str) -> str:
    """Where the host's voices are, as in "Voices on <place> only"."""
    return str(_names(host).get("place") or host)


def _health(url: str, timeout: float = PROBE_S) -> dict | None:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as response:
            body = json.loads(response.read().decode())
        return body if isinstance(body, dict) else None
    except (OSError, ValueError):
        return None


def _ask(items, health) -> dict[str, dict | None]:
    result: dict[str, dict | None] = {}
    threads = [threading.Thread(target=lambda name=name, url=url: result.update({name: health(url)}), daemon=True)
               for name, url, _voices in items]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + DEADLINE_S
    for thread in threads:
        thread.join(max(0, deadline - time.monotonic()))
    snapshot = dict(result)  # a host still silent at the deadline is out of reach
    return {name: snapshot.get(name) for name, _url, _voices in items}


def _reason(host: str, report: dict | None) -> str:
    """'' when the host is ready, else why not, in the page's words."""
    if report is None:
        return f"{subject(host)} is off or out of reach"
    state = report.get("state") or ("busy" if report.get("busy") else "ready")
    if state == "ready":
        return ""
    if report.get("why"):
        return str(report["why"])[:200]
    return f"{possessive(host)} graphics card is busy" if state == "busy" else f"{subject(host)} stood down"


def _status(host: str, report: dict | None, now: float) -> tuple[str, bool]:
    """(why, resting) for one host, '' when it is ready. The add-on's words
    replace the core's where it has some, and resting marks a host down on
    purpose; nothing it says makes a host ready."""
    why = _reason(host, report)
    if not why:
        return "", False
    told = addons.voice_host_status(host, report, now)
    said = told.get("why")
    if isinstance(said, str) and said.strip():
        why = said.strip()[:200]
    return why, told.get("resting") is True


def _last_clip(log: Path) -> tuple[float, bool] | None:
    """(when, whether it went to the backup voices) of the newest remote render."""
    try:
        with log.open("rb") as source:
            source.seek(0, 2)
            source.seek(max(0, source.tell() - LOG_TAIL_BYTES))
            lines = source.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        match = _LINE.match(line)
        if not match:
            continue
        try:
            return time.mktime(time.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")), bool(match.group(3))
        except ValueError:
            continue
    return None


def _compute(tts: Path, now: float, health) -> dict:
    selected, required = hosts(tts), wanted(tts)
    if not selected or not required:
        return {"real": True, "why": "", "note": "", "at": now, "configured": bool(selected)}
    # A later host serving none of the voices in use says nothing either way.
    applicable = [item for index, item in enumerate(selected) if index == 0 or "*" in item[2] or item[2] & required]
    reports = _ask(applicable, health)
    status = {name: _status(name, reports[name], now) for name, _url, _voices in applicable}
    ready = [name for name, _url, _voices in applicable if not status[name][0]]
    down = ", ".join(why for why, _resting in status.values() if why)
    awake = ", ".join(why for why, resting in status.values() if why and not resting)
    served: set[str] = set()
    for name, _url, listed in applicable:
        if name in ready:
            served |= required if "*" in listed else listed & required
    on = " and ".join(place(name) for name in ready)
    last = _last_clip(Path(tts) / "speak.log")
    if not ready:
        value = {"real": False, "why": down, "note": ""}
    elif not required <= served:
        only = ", ".join(sorted(voice.capitalize() for voice in served))
        value = {"real": False, "why": f"{down}; only {only} on {on}", "note": ""}
    elif last and last[1] and now - last[0] < RECENT_S:
        value = {"real": False, "why": "the last clip used them; the voices are back now", "note": ""}
    elif awake:
        value = {"real": True, "why": "", "note": f"Voices on {on} only: {awake}"}
    else:
        value = {"real": True, "why": "", "note": ""}
    return {**value, "at": now, "configured": True}


def state(tts: Path, now: float | None = None, health=_health) -> dict:
    """{"real", "why", "note", "at", "configured"}. ``real`` is False only when
    the next clip would go to the backup voices, or the last one did."""
    now, key = time.time() if now is None else now, str(Path(tts))
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_S:
        return hit[1]
    # One refresh at a time: while it runs, other callers take the last answer
    # at once, or wait for that refresh when there is none yet.
    if not _flight.acquire(blocking=False):
        if hit:
            return hit[1]
        with _flight:
            if key not in _cache:
                _cache[key] = (now, _compute(Path(tts), now, health))
            return _cache[key][1]
    try:
        value = _compute(Path(tts), now, health)
        _cache[key] = (now, value)
        return value
    finally:
        _flight.release()
