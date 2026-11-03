"""The voice lab: a speech coordinator's voice builds, its cast, and what waits
on a decision. Read only, but for a build's decision, a cast voice's tier and a
test line, which the routes in server/voicelab.py take.

Pure functions over paths. Each section is {"ok": True, ...} or {"ok": False,
"why": words}, so a source that cannot be read hides only its own section.
Limits: run.log is read from its last 4 KB only; no clip, wav or model file is
ever opened; git reads the coordinator's repository only, its last 400
commits, at most once a minute.

THE STAGE LIST IS READ FROM THE LAB'S OWN SCRIPT, by regex, not copied here.
<tts_dir>/voicelab/voicelab.py is a script with no import guard, so it is
never imported; but a copy here would drift the day a stage is added, and the
page would then draw a build as done while one still runs.

An install with no coordinator has the voicelab/ pipeline instead
(pipeline_summary). The addons module may give the "right now" line
(voicelab_now), a voice's notes (voicelab_notes) and what follows a newly
wanted build (voicelab_decided).
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import addons, agentvibes, auditions, config, safe_paths, voices as vhosts

RUNNING_MIN = 15             # voicelab_running_minutes: part done and moved this recently is running
HISTORY_S = 14 * 86400       # audition done this long ago, nothing waiting: history
LOG_TAIL = 4096
GIT_N = 400
GIT_TTL = 60
BASE_TAGS = ("live",)        # voicelab_baseline_tags: the live voice's own takes in scores.json
FIGURES = ("ecapa", "rz", "wer")
DIR = re.compile(r"[A-Za-z0-9_.-]{1,64}")
STAMP = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")
CLIP = re.compile(r"(new|live)-(\d{1,2})\.mp3")
_STAGES = re.compile(r"^STAGES\s*=\s*(\[[^\]]*\])", re.M)
_CONF = re.compile(r"([A-Za-z0-9_-]{1,32})\s*=\s*([A-Za-z0-9_.+-]{1,64})")
_TIER_DECL = re.compile(r"^declare -A VOICE_TIER=\((.*)\)\s*$", re.M)
_TIER_PAIR = re.compile(r"\[([\w-]+)\]=([1-3])\b")
_ENGINE = re.compile(r"^\s+'([\w-]+)':\s*dict\(\s*(gpt|kokoro)=", re.M)
ENGINES = {"gpt": "GPT-SoVITS", "kokoro": "Kokoro"}
# Agents are often named after the cast, so a commit subject naming a voice alone is
# usually an agent's work, not the voice's: it counts only with a voice word too.
# Whole words, so "invoice" is not one.
VOICE_WORD = re.compile(r"\b(voices?|trained|IndexTTS2|GSV|GPT-SoVITS|rebuild|clips?|takes?|refs?)\b", re.I)

TEST_STOCK = "Testing, testing. This is how I sound on comms right now."
TEST_MAX = 200
TEST_TIMEOUT_S = 120
_test_lock = threading.Lock()  # one test render at a time, server-wide (the route takes it)

_git_cache: dict = {}
_git_lock = threading.Lock()


def tts_dir() -> Path:
    raw = config.get("tts_dir")
    return Path(str(raw)).expanduser() if raw else Path.home() / ".local" / "share" / "agent-companion" / "tts"


def lab_root() -> Path:
    raw = config.get("voicelab_dir")
    return Path(str(raw)).expanduser() if raw else tts_dir() / "voicelab" / "builds"


def configured() -> bool:
    return agentvibes.available(tts_dir())


def running_s() -> float:
    """voicelab_running_minutes, in seconds: a part-done build that moved this recently is running.
    The environment gives a string, so a string that is a number counts as one."""
    raw = config.get("voicelab_running_minutes")
    if isinstance(raw, str):
        try:
            raw = float(raw)
        except ValueError:
            raw = None
    ok = isinstance(raw, (int, float)) and not isinstance(raw, bool) and 0 < raw < float("inf")
    return float(raw if ok else RUNNING_MIN) * 60


def baseline_tags() -> tuple[str, ...]:
    """voicelab_baseline_tags: the tags scores.json gives the live voice's own takes. In the
    environment, os.pathsep-separated, as the core's other lists."""
    raw = config.get("voicelab_baseline_tags")
    if isinstance(raw, str):
        raw = raw.split(os.pathsep)
    tags = tuple(x for x in raw if isinstance(x, str) and x) if isinstance(raw, list) else ()
    return tags or BASE_TAGS


# --- the voicelab/ pipeline, for an install with no coordinator ---
# What voicelab/prep.py, train.sh and serve.py leave under voices_dir. Training
# and serving happen from the shell (voicelab/README.md), never from this page.
VOICE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
WEIGHTS = (".ckpt", ".pth")
AUDIO = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".flac": "audio/flac"}


def voices_dir() -> Path:
    raw = config.env("VOICES_DIR") or config.get("voices_dir")
    return Path(str(raw)).expanduser() if raw else Path.home() / ".local/share/agent-companion/voices"


def _size(d: Path) -> int:
    total = 0
    for p in d.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _stage(d: Path) -> str:
    if (d / "voice.json").is_file():
        return "served"
    if any(p.suffix in WEIGHTS for p in d.rglob("*") if p.is_file()):
        return "trained"
    return "prepped"


def _pipeline_row(d: Path) -> dict:
    aud = d / "audition"
    clips = sorted(p.name for p in aud.iterdir() if p.is_file() and p.suffix.lower() in AUDIO) if aud.is_dir() else []
    return {"name": d.name, "stage": _stage(d), "bytes": _size(d), "audition": clips}


def pipeline_summary() -> dict:
    root = voices_dir()
    dirs = sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and VOICE_NAME.match(p.name)) \
        if root.is_dir() else []
    rows = [_pipeline_row(d) for d in dirs]
    text = (f"{len(rows)} voice{'' if len(rows) == 1 else 's'} in {root}" if rows
            else f"No voices yet: voicelab/README.md says how to make one (they land in {root}).")
    why = "Tiers and test lines need a speech coordinator; this install gives a speaker its voice in comms/cast.tsv."
    return {"now": {"kind": "pipeline", "text": text},
            "needs": {"ok": True, "items": []},
            "running": {"ok": True, "builds": []},
            "voices": {"ok": False, "voices": [], "why": why},
            "other": {"ok": True, "builds": []},
            "pipeline": {"root": str(root), "voices": rows}}


def stages(tts: Path) -> list[str] | None:
    """voicelab.py's STAGES, or None when it cannot be read."""
    try:
        text = (Path(tts) / "voicelab" / "voicelab.py").read_text(errors="replace")
    except OSError:
        return None
    m = _STAGES.search(text)
    if not m:
        return None
    try:
        v = ast.literal_eval(m.group(1))
    except (ValueError, SyntaxError):
        return None
    ok = isinstance(v, list) and v and all(isinstance(s, str) and DIR.fullmatch(s) for s in v)
    return v if ok else None


def _tail(log: Path) -> tuple[str | None, float | None]:
    """(the last non-empty line, the file's mtime), from the last LOG_TAIL bytes only."""
    try:
        with log.open("rb") as f:
            size = os.fstat(f.fileno()).st_size
            f.seek(max(0, size - LOG_TAIL))
            chunk = f.read(LOG_TAIL)
            at = os.fstat(f.fileno()).st_mtime
    except OSError:
        return None, None
    lines = [ln.strip() for ln in chunk.decode("utf-8", "replace").splitlines() if ln.strip()]
    return (lines[-1] if lines else None), at


def _take(a) -> dict | None:
    """One ranked take: its tag and the figures it has. A missing figure is absent, never 0."""
    if not isinstance(a, dict):
        return None
    out = {"tag": str(a.get("tag") or "")}
    for k in FIGURES:
        v = a.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = round(float(v), 3)
    return out


def _scores(d: Path) -> dict | None:
    """{"best": take, "baseline": take or None} from scores.json, read as voicelab.py's audition
    stage reads it: best is the first ranked take that is not the live voice's."""
    try:
        s = json.loads((d / "scores.json").read_text())
        rank = s["rank"]
    except (OSError, ValueError, KeyError, TypeError):
        return {"best": None, "baseline": None, "why": "scores.json could not be read"}
    if not isinstance(rank, list):
        return {"best": None, "baseline": None, "why": "scores.json has no ranking"}
    tags = baseline_tags()
    best = next((a for a in rank if isinstance(a, dict) and a.get("tag") not in tags), None)
    base = next((a for a in rank if isinstance(a, dict) and a.get("tag") in tags), None)
    return {"best": _take(best), "baseline": _take(base)}


def is_build_dir(name: str) -> bool:
    """A name safe to join under lab_root(): DIR allows dots, so "." and ".." need ruling out."""
    return bool(DIR.fullmatch(name)) and name not in (".", "..")


def _live_text(tts: Path) -> str | None:
    """voice_server.py's text, or None when it cannot be read."""
    try:
        return (Path(tts) / "voice-server" / "voice_server.py").read_text(errors="replace")
    except OSError:
        return None


def _clips(d: Path) -> dict:
    """{"new": [...], "live": [...]}: the mp3 takes in <build>/audition, by number. Names only."""
    out: dict = {"new": [], "live": []}
    try:
        names = os.listdir(d / "audition")
    except OSError:
        return out
    found = [(m.group(1), int(m.group(2)), n) for n in names if (m := CLIP.fullmatch(n))]
    for kind, _, n in sorted(found):
        out[kind].append(n)
    return out


def _ceiling(d: Path):
    """result.json's ceiling[0] (the real lines' ECAPA) when it is a number, else None."""
    try:
        c = json.loads((d / "audition" / "result.json").read_text())["ceiling"][0]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None
    return round(float(c), 3) if isinstance(c, (int, float)) and not isinstance(c, bool) else None


def decision_of(d: Path) -> dict | None:
    """<build>/decision.json as {"want": bool, "at": float}, or None (missing or malformed)."""
    try:
        v = json.loads((d / "decision.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(v, dict) or not isinstance(v.get("want"), bool):
        return None
    at = v.get("at")
    return {"want": v["want"], "at": float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None}


def decide(lab: Path, dirname: str, want: bool, now: float | None = None) -> None:
    """Want it / Not this one on a build, written whole, inside the lab only."""
    target = safe_paths.directory(Path(lab), dirname) if is_build_dir(dirname) else None
    if target is None:
        raise ValueError(f"not a build: {dirname!r}")
    auditions._atomic(target / "decision.json",
                      json.dumps({"want": bool(want), "at": time.time() if now is None else now}))


def _history(tts: Path, name: str, audition_at: float | None, now: float) -> bool:
    """Audition done more than 14 days ago and nothing of it waiting on a decision: its
    folder under the coordinator's auditions is gone, or carries the decision."""
    if audition_at is None or now - audition_at <= HISTORY_S:
        return False
    d = auditions.root(tts) / name
    return not d.is_dir() or (d / "decision.json").is_file()


def build(d: Path, stage_list: list[str], tts: Path, now: float, live_text: str | None = None) -> dict | None:
    """One build, or None for a folder that is not one (no voice.json) or is history."""
    try:
        v = json.loads((d / "voice.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(v, dict):
        return None
    name = str(v.get("Name") or v.get("name") or d.name)
    if not auditions.NAME.fullmatch(name):        # it becomes a path under tts/auditions/
        name = d.name
    if (eng := zero_shot(v)) is not None:
        return _zero_shot_build(d, name, eng, tts, now)
    marks, newest, audition_at = [], 0.0, None
    for s in stage_list:
        f = d / f".done-{s}"
        try:
            mtime = f.stat().st_mtime
            text = f.read_text(errors="replace").strip()[:19]
        except OSError:
            marks.append({"stage": s, "done": False, "at": None})
            continue
        marks.append({"stage": s, "done": True, "at": text if STAMP.fullmatch(text) else None})
        newest = max(newest, mtime)
        if s == "audition":
            audition_at = mtime
    if _history(tts, name, audition_at, now):
        return None
    nxt = next((i for i, m in enumerate(marks) if not m["done"]), None)
    line, line_at = _tail(d / "run.log")
    active = max(newest, line_at or 0.0)
    done = {m["stage"] for m in marks if m["done"]}
    run = v.get("run")
    live_now = bool(live_text and isinstance(run, str) and DIR.fullmatch(run)
                    and f"/gsv-train/{run}/" in live_text)
    scores = _scores(d) if "score" in done else None
    if scores is not None and (c := _ceiling(d)) is not None:
        scores["ceiling"] = c
    return {"dir": d.name, "name": name, "stages": marks,
            "next": marks[nxt]["stage"] if nxt is not None else None,
            "step": nxt + 1 if nxt is not None else None, "of": len(stage_list),
            "line": line, "line_at": line_at, "active_at": active or None,
            "scores": scores, "clips": _clips(d), "live_now": live_now,
            "decision": decision_of(d)}


# ZERO-SHOT BUILDS: <lab>/<voice>-index/ holds an IndexTTS2 copy of the comms reference clip,
# nothing trained. voice.json's "engine" says so ("IndexTTS2 zero-shot"). One step, the
# audition: no stage chips claiming a train or sweep that never ran, and the engine named so
# it can be told from the trained build of the same voice.
def zero_shot(v: dict) -> str | None:
    """The engine's name ("IndexTTS2") when voice.json marks a zero-shot build, else None."""
    eng = v.get("engine")
    if not isinstance(eng, str) or "zero-shot" not in eng.lower():
        return None
    label = re.sub(r"(?i)\s*zero-shot\s*", " ", eng).strip()
    return label[:40] or "zero-shot"


def _zero_shot_build(d: Path, name: str, engine: str, tts: Path, now: float) -> dict | None:
    try:
        audition_at = (d / ".done-audition").stat().st_mtime
    except OSError:
        audition_at = None
    if _history(tts, name, audition_at, now):
        return None
    line, line_at = _tail(d / "run.log")
    return {"dir": d.name, "name": name, "engine": engine, "zero_shot": True, "stages": [],
            "next": None if audition_at else "audition", "step": None if audition_at else 1, "of": 1,
            "line": line, "line_at": line_at, "active_at": max(audition_at or 0.0, line_at or 0.0) or None,
            "scores": None, "clips": _clips(d), "live_now": False,
            "decision": decision_of(d)}


def builds(tts: Path, lab: Path, now: float) -> dict:
    stage_list = stages(tts)
    if stage_list is None:
        return {"ok": False, "why": "could not read the stage list in tts/voicelab/voicelab.py"}
    try:
        dirs = sorted(p for p in Path(lab).iterdir() if p.is_dir() and DIR.fullmatch(p.name))
    except OSError:
        return {"ok": False, "why": f"could not read {lab}"}
    live_text = _live_text(tts)
    out = [b for b in (build(d, stage_list, Path(tts), now, live_text) for d in dirs) if b]
    out.sort(key=lambda b: -(b["active_at"] or 0))
    return {"ok": True, "builds": out}


def finished(b: dict) -> bool:
    """Through its last stage, with something to hear."""
    return b.get("next") is None and bool((b.get("clips") or {}).get("new"))


def phase(b: dict, now: float) -> str:
    """finished, running (part done and moved within voicelab_running_minutes) or stopped."""
    if b.get("next") is None:
        return "finished" if finished(b) else "stopped"
    at = b.get("active_at")
    return "running" if at and now - at < running_s() else "stopped"


def verdict(b: dict, name: str) -> str:
    """One plain line about a build's scores (ECAPA x100: how like the real voice)."""
    if b.get("zero_shot"):
        return f"{b.get('engine') or 'Zero-shot'} copy of the comms clip, not scored"
    sc = b.get("scores") or {}
    best, base = sc.get("best") or {}, sc.get("baseline") or {}
    if "ecapa" not in best:
        return sc.get("why") or "Not scored yet"
    n = round(best["ecapa"] * 100)
    if b.get("live_now") or "ecapa" not in base:
        line = f"Sounds like {name} {n}"
    else:
        lv = round(base["ecapa"] * 100)
        line = (f"New sounds more like {name}: {n} against live {lv}" if n - lv >= 5
                else f"Live sounds more like {name}: {lv} against new {n}" if lv - n >= 5
                else f"A wash: new {n}, live {lv}")
    if "wer" in best:
        line += f" · words misheard {round(best['wer'] * 100)}%"
    return line


def right_now(vs: dict | None, blist: list[dict], now: float) -> dict:
    """The voice host in one line, from what voices.state() already knows; never asks a
    host. The addons module's voicelab_now gives words of its own instead."""
    hooked = addons.voicelab_now(vs, blist, now)
    if hooked:
        return hooked
    if vs is None:
        return {"kind": "unknown", "text": "the voice host's state is not known here"}
    if vs.get("real"):
        return {"kind": "free", "text": "free"}
    return {"kind": "busy", "text": str(vs.get("why") or "").strip() or "the voice host is not free"}


def tiers(tts: Path) -> dict[str, tuple[int, str]]:
    """{lowercased name: (tier, where it came from)}: identity.sh's VOICE_TIER, then tiers.conf
    lines (name=1..3, the last one wins), as identity.sh tier_of() reads them. Parsed, never run."""
    out: dict[str, tuple[int, str]] = {}
    try:
        m = _TIER_DECL.search((Path(tts) / "identity.sh").read_text(errors="replace"))
    except OSError:
        m = None
    if m:
        for n, t in _TIER_PAIR.findall(m.group(1)):
            out[n.lower()] = (int(t), "identity.sh")
    try:
        lines = (Path(tts) / "tiers.conf").read_text(errors="replace").splitlines()
    except OSError:
        lines = []
    for ln in lines:
        k, _, t = ln.strip().partition("=")
        if k.strip() and t.strip() in ("1", "2", "3"):
            out[k.strip().lower()] = (int(t.strip()), "tiers.conf")
    return out


def engines(tts: Path) -> dict[str, str]:
    """{engine voice: "GPT-SoVITS" | "Kokoro"} from voice_server.py's VOICES, by regex. Never imported."""
    try:
        text = (Path(tts) / "voice-server" / "voice_server.py").read_text(errors="replace")
    except OSError:
        return {}
    return {v: ENGINES[e] for v, e in _ENGINE.findall(text)}


def index_engine(tts: Path, voice: str) -> str:
    """What a host's own version of a voice is: "IndexTTS2" when index_voices.py lists it, else "own version"."""
    try:
        text = (Path(tts) / "voice-server" / "index_voices.py").read_text(errors="replace")
    except OSError:
        return "own version"
    return "IndexTTS2" if re.search(rf"'{re.escape(voice)}'", text) else "own version"


def _clip_subject(t: str) -> str:
    """A subject is one line; some were committed with their body glued on (a 600-character
    "subject" naming five agents). Only its first 120 characters are the subject."""
    return t if len(t) <= 120 else t[:119].rstrip() + "\u2026"


def commits(tts: Path, now: float, run=subprocess.run) -> list[dict] | None:
    """The tts repo's last 400 commits, newest first, cached 60 s; None (also cached) when git cannot read it."""
    key = str(tts)
    with _git_lock:
        hit = _git_cache.get(key)
        if hit and now - hit[0] < GIT_TTL:
            return hit[1]
    try:
        p = run(["git", "-C", key, "log", "-n", str(GIT_N), "--format=%H%x09%ct%x09%s"],
                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        p = None
    out = None  # a failure is cached too: a hung git would otherwise cost 10 s on every poll
    if p is not None and p.returncode == 0:
        out = []
        for line in p.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3 and parts[1].isdigit():
                out.append({"sha": parts[0][:10], "at": int(parts[1]), "subject": _clip_subject(parts[2])})
    with _git_lock:
        _git_cache[key] = (now, out)
    return out


def _word(w: str) -> re.Pattern:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(w)}(?![A-Za-z0-9])", re.I)


def cast(tts: Path, now: float) -> dict:
    """Every voice in remote-voices.conf with its engine, tier and newest tts-repo change."""
    try:
        lines = (Path(tts) / "remote-voices.conf").read_text(errors="replace").splitlines()
    except OSError:
        return {"ok": False, "why": "could not read tts/remote-voices.conf"}
    tier, engine = tiers(tts), engines(tts)
    hl, own = vhosts.hosts(tts), vhosts.own_versions(tts)
    first = hl[0][0] if hl else ""  # the reference host, named in the engine words
    listed_later = set().union(*(h[2] for h in hl[1:])) if len(hl) > 1 else set()
    index = index_voices(tts)
    log = commits(tts, now)
    voices = []
    for ln in lines:
        m = _CONF.fullmatch(ln.strip())
        if not m:
            continue
        name, voice = m.group(1), m.group(2)
        t = tier.get(name.lower())
        changes = None
        if log is not None:
            rx = [_word(name)] + ([_word(voice)] if voice.lower() != name.lower() else [])
            changes = [c for c in log if VOICE_WORD.search(c["subject"])
                       and any(r.search(c["subject"]) for r in rx)][:10]
        base = engine.get(voice, "unknown")
        eng = base
        if voice in own:  # IndexTTS2 there; the reference host's copy is its backup
            eng = f"{index_engine(tts, voice)} on {vhosts.place(own[voice])}, {eng} on {first}"
        voices.append({"name": name, "voice": voice, "engine": eng,
                       "words": _words(voice, base, own, listed_later, index, first),
                       "tier": t[0] if t else 2, "tier_from": t[1] if t else "default",
                       "changes": changes})
    out = {"ok": True, "voices": voices}
    if log is None:
        out["note"] = "the tts repo's history could not be read"
    return out


def cast_name(tts: Path, name: str) -> str | None:
    """The cast's own spelling of a voice's name (any case in), or None when it is not in
    remote-voices.conf. Matched with _CONF as cast() does."""
    try:
        lines = (Path(tts) / "remote-voices.conf").read_text(errors="replace").splitlines()
    except OSError:
        return None
    for ln in lines:
        m = _CONF.fullmatch(ln.strip())
        if m and m.group(1).lower() == str(name).lower():
            return m.group(1)
    return None


_BUILD_SUFFIX = re.compile(r"-(?:index|gsv)$")
_INDEX_TUPLE = re.compile(r"for\s+v\s+in\s+\(([^)]*)\)")


def voice_of(dirname: str, voices: list[dict]) -> str | None:
    """The cast voice id a build folder belongs to (spec: builds belong to voices): the name,
    lowercased, minus -index or -gsv, tried whole and by its first hyphen part, each with and
    without trailing digits, against the cast's voice ids and names. ada2-index is ada,
    kit47-index is kit47, lin-lol-index is lin; comms-index-test is nobody's."""
    known: dict[str, str] = {}
    for v in voices:
        known.setdefault(str(v.get("voice") or "").lower(), v.get("voice"))
        known.setdefault(str(v.get("name") or "").lower(), v.get("voice"))
    known.pop("", None)
    n = _BUILD_SUFFIX.sub("", str(dirname or "").lower())
    for cand in (n, n.split("-", 1)[0]):
        for c in (cand, re.sub(r"\d+$", "", cand)):
            if c in known:
                return known[c]
    return None


def fold(voices: list[dict]) -> list[dict]:
    """One row per voice id: the row named after the voice (Ada for ada) leads, others
    sharing it (Max) become its aliases."""
    by: dict[str, dict] = {}
    for v in voices:
        vid = v.get("voice")
        lead = by.get(vid)
        if lead is None:
            by[vid] = dict(v, aliases=[])
        elif v["name"].lower() == str(vid).lower() and lead["name"].lower() != str(vid).lower():
            by[vid] = dict(v, aliases=[lead["name"]] + lead["aliases"])
        else:
            lead["aliases"].append(v["name"])
    return list(by.values())


def index_voices(tts: Path) -> set[str]:
    """The voices index_voices.py hands to IndexTTS2 (its VOICES tuple), by regex. Never imported."""
    try:
        text = (Path(tts) / "voice-server" / "index_voices.py").read_text(errors="replace")
    except OSError:
        return set()
    m = _INDEX_TUPLE.search(text)
    return set(re.findall(r"'([\w-]+)'", m.group(1))) if m else set()


def _words(voice: str, base: str, own: dict, listed_later: set[str], index: set[str], first: str) -> str:
    """The engine as the row reads it; first is the hosts file's first host, the reference."""
    if voice in own:
        eng = "IndexTTS2" if voice in index else "Own version"
        return f"{eng}, {first} backup" if base != "unknown" else f"{eng} only"
    if base == "GPT-SoVITS":
        return "Trained, both PCs" if voice in listed_later else f"Trained, {first or 'one host'} only"
    if base == "Kokoro":
        return "Kokoro"
    return "Engine not known"


class TestLineError(Exception):
    """A test line that could not be made; the message is plain words for the page."""
    __test__ = False


def test_text(text) -> str:
    """The line to say: stripped, the stock line when empty, ValueError past TEST_MAX characters."""
    if text is None:
        return TEST_STOCK
    if not isinstance(text, str):
        raise ValueError("text must be words")
    t = text.strip()
    if len(t) > TEST_MAX:
        raise ValueError(f"at most {TEST_MAX} characters")
    return t or TEST_STOCK


def _rendered_by(added: str, name: str) -> str:
    """Who made this clip, from the speak.log lines the render appended (the last one for this
    speaker): REMOTE(voice[+]@host) [woke] -> the host; REMOTE(voice) FALLBACK reasons -> Piper. The
    line after ' :: ' can hold the spoken text, so it is never read."""
    nm = re.escape(name)
    found = "unknown"
    for ln in added.splitlines():
        m = re.search(rf"REMOTE\([^)@]*@([\w.-]+)\) (woke )?[\d.]+s {nm}(?: |$)", ln)
        if m:
            found = m.group(1) + (" (woken first)" if m.group(2) else "")
            continue
        m = re.search(rf"REMOTE\([^)]*\) FALLBACK (.*?) {nm}(?: :: |$)", ln)
        if m:
            found = f"Piper (fallback: {m.group(1).strip() or 'no reason given'})"
    return found


def test_line(tts: Path, name: str, text: str, run=subprocess.run) -> tuple[bytes, str]:
    """Say one line as a cast voice through the real speak.sh, rendered to a temp wav and not
    played: (the wav bytes, who rendered it). speak.log is read only from the size it had
    before the run. The line is never logged. TestLineError says why it failed."""
    tts = Path(tts)
    log = tts / "speak.log"
    tmp = tempfile.mkdtemp(prefix="vl-test-")
    try:
        wav = Path(tmp) / "clip.wav"
        env = {**os.environ, "TTS_SPEAKER": name, "TTS_RENDER_TO": str(wav),
               "TTS_FLUSHING": "1", "TTS_NO_OPENER": "1"}
        try:
            before = log.stat().st_size
        except OSError:
            before = None
        try:
            r = run([str(tts / "speak.sh"), text], env=env, timeout=TEST_TIMEOUT_S,
                    capture_output=True, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise TestLineError("the voice took too long (over 2 minutes)") from None
        except OSError as e:
            raise TestLineError(f"could not run speak.sh: {e.strerror or e}") from None
        if r.returncode != 0:
            raise TestLineError(f"speak.sh exited {r.returncode}")
        try:
            data = wav.read_bytes()
        except OSError:
            data = b""
        if not data:
            raise TestLineError("speak.sh made no sound")
        added = ""
        if before is not None:
            try:
                with log.open("rb") as f:
                    f.seek(before)
                    added = f.read(65536).decode("utf-8", "replace")
            except OSError:
                pass
        return data, _rendered_by(added, name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def summary(tts: Path, lab: Path, voices_state: dict | None, now: float) -> dict:
    """GET /api/voicelab's body: {now, needs, running, voices, other}."""
    tts = Path(tts)
    b = builds(tts, lab, now)
    blist = b.get("builds", []) if b.get("ok") else []
    c = cast(tts, now)
    rows = fold(c.get("voices", [])) if c.get("ok") else []
    index = index_voices(tts)
    names = {r["voice"]: r["name"] for r in rows}
    mine: dict[str, list[dict]] = {r["voice"]: [] for r in rows}
    other, running = [], []
    for x in blist:                                  # builds() sorts newest first
        vid = voice_of(x["dir"], c.get("voices", []))
        if x.get("zero_shot") and vid in index:
            x["live_now"] = True
        x["phase"] = phase(x, now)
        x["verdict"] = verdict(x, names.get(vid, x["name"]))
        if x["phase"] == "running":
            running.append(dict(x, voice_name=names.get(vid, x["name"])))
        (mine[vid] if vid in mine else other).append(x)
    needs = [{"kind": "audition", "name": a["name"], "note": a["note"], "clips": a["clips"]}
             for a in auditions.listing(tts) if not a["decision"]]
    voices = []
    for r in rows:
        mb = mine[r["voice"]]
        newest = next((x for x in mb if x["phase"] == "finished"), None)
        waiting = bool(newest and not newest["live_now"] and newest["decision"] is None)
        if waiting:
            needs.append({"kind": "build", "voice": r["voice"], "name": r["name"], "dir": newest["dir"],
                          "engine": newest.get("engine") or "Trained", "verdict": newest["verdict"],
                          "clips": newest["clips"]})
        voices.append(dict(r, waiting=waiting, builds=mb, changes=(r.get("changes") or [])[:3],
                           notes=addons.voicelab_notes([r["name"]] + r["aliases"])))
    voices.sort(key=lambda v: (not v["waiting"], v["tier"], v["name"].lower()))
    vout = {"ok": bool(c.get("ok")), "voices": voices}
    for k in ("note", "why"):
        if c.get(k):
            vout[k] = c[k]
    rout = {"ok": bool(b.get("ok")), "builds": running}
    if not b.get("ok"):
        rout["why"] = b.get("why")
    return {"now": right_now(voices_state, blist, now), "needs": {"ok": True, "items": needs},
            "running": rout, "voices": vout, "other": {"ok": bool(b.get("ok")), "builds": other}}
