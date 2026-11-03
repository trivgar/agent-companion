"""Connections: what is wired up and what is not, one honest row each.

`rows(probes)` is pure: it turns facts into rows. `run_probes()` gathers the
facts with cheap checks and short timeouts (a version flag, a status command,
files on disk, one loopback call). Results are cached for 60 seconds; the
Recheck button clears the cache. Under pytest nothing is probed unless a test
injects facts through INJECT.

Each row: {id, name, state: ok|off|broken, detail, fix}. "off" means not set
up (fine if you do not want it); "broken" means set up and not working. A fix
is one plain sentence pointing at the step that sets the thing up.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import APIRouter, Depends

from .. import config
from . import auth

TITLE = "Connections"
router = APIRouter()
STATIC = Path(__file__).resolve().parent / "static"
TTL = 60
INJECT = None            # tests: a callable returning probe facts
_cache: dict = {"at": 0.0, "rows": None}
_lock = threading.Lock()


def _row(id_, name, state, detail, fix=""):
    return {"id": id_, "name": name, "state": state, "detail": detail, "fix": "" if state == "ok" else fix}


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _claude(p):
    if p.get("found"):
        return _row("claude", "Claude Code", "ok", f"Found, version {p.get('version') or 'unknown'}.")
    return _row("claude", "Claude Code", "broken", "The claude command was not found.",
                "Install Claude Code and make sure `claude --version` works (INSTALL.md step 2).")


def _sessions(p):
    if p.get("exists"):
        return _row("sessions", "Sessions", "ok", "Claude Code's session files are readable.")
    return _row("sessions", "Sessions", "broken", "The Claude Code folder was not found.",
                "Run Claude Code once, or set claude_home in the config (INSTALL.md step 2).")


def _tailscale(p):
    if not p.get("installed"):
        return _row("tailscale", "Phone access", "off", "Tailscale is not installed on this computer.",
                    "Install Tailscale and run `tailscale serve` for this port (INSTALL.md, Phone access with Tailscale).")
    if not p.get("serving"):
        return _row("tailscale", "Phone access", "off", "Tailscale is not serving this port.",
                    "Run `tailscale serve` for this port so your phone can reach it (INSTALL.md, Phone access with Tailscale).")
    return _row("tailscale", "Phone access", "ok", "Tailscale serves this port.")


def _push(p):
    n = int(p.get("devices") or 0)
    if not p.get("vapid"):
        return _row("push", "Push", "off", "No push key yet, so no device can subscribe.",
                    "Open the Push tab on your phone and press Enable (INSTALL.md, Phone access with Tailscale).")
    if n == 0:
        return _row("push", "Push", "off", "Push is ready, but no device is subscribed.",
                    "Open the Push tab on your phone and press Enable (INSTALL.md, Phone access with Tailscale).")
    return _row("push", "Push", "ok", f"{_plural(n, 'device')} subscribed.")


def _devices(p):
    n = int(p.get("count") or 0)
    if n == 0:
        return _row("devices", "Paired devices", "off", "No device is paired.",
                    "Run `python -m agent_companion.server pair` and open the link on your phone (INSTALL.md step 4).")
    return _row("devices", "Paired devices", "ok", f"{_plural(n, 'device')} paired.")


def _mail(p):
    src = p.get("source")
    if not p.get("accounts"):
        return _row("mail", "Mail drafts", "off", "No mail accounts are configured.",
                    "Add mail_accounts entries that choose a source in the config.")
    if not src:
        return _row("mail", "Mail drafts", "off", "No mail source is set.",
                    "Choose a source for every mail_accounts entry and turn on the timer (INSTALL.md, Mail drafting).")
    if not p.get("known"):
        return _row("mail", "Mail drafts", "broken", f"The mail source {src} is not one this install knows.",
                    "Choose gmail or imap, or add the provider as docs/mail-outlook.md describes.")
    run = p.get("last_run")
    if run and not run.get("ok"):
        return _row("mail", "Mail drafts", "broken",
                    f"The last drafter run failed ({run.get('note') or 'no detail'}) at {run.get('at') or 'an unknown time'}.",
                    "Check the mail log and that Claude Code can reach the mail connector (INSTALL.md, Mail drafting).")
    when = f"last run {run['at']}" if run and run.get("at") else "no run recorded yet"
    return _row("mail", "Mail drafts", "ok", f"Source {src}, {when}.")


def _comms(p):
    if not p.get("piper"):
        return _row("comms", "Spoken replies", "off", "The Piper voice program was not found.",
                    "Run comms/install.sh to fetch Piper and the stock voices (INSTALL.md, Comms and Piper).")
    if not int(p.get("voices") or 0):
        return _row("comms", "Spoken replies", "off", "Piper is there, but no cast voice is installed.",
                    "Run comms/install.sh to fetch the voices in comms/cast.tsv (INSTALL.md, Comms and Piper).")
    hold = " Comms are on hold." if p.get("held") else ""
    return _row("comms", "Spoken replies", "ok", f"Piper and {_plural(int(p['voices']), 'voice')} installed.{hold}")


def _voice_server(p):
    st = p.get("state")
    if st == "up":
        return _row("voice_server", "Voice server", "ok", "The voice server answers on this computer.")
    if st == "refused":
        return _row("voice_server", "Voice server", "broken", "The voice server answered but refused the voice token.",
                    "Restart voicelab/serve.py so it and ~/.config/agent-companion/voice-token agree (INSTALL.md step 12).")
    return _row("voice_server", "Voice server", "off", "No voice server is running. Stock voices still work.",
                "Only needed for a custom voice: start voicelab/serve.py (INSTALL.md step 12).")


def _collab(p):
    if not p.get("configured"):
        return _row("collab", "Notes between Claudes", "off", "collab_label, collab_from or collab_repo is not set.",
                    "Set the three collab keys in the config (INSTALL.md step 13, COLLAB.md).")
    if not p.get("gh"):
        return _row("collab", "Notes between Claudes", "broken", "The GitHub command is missing or not signed in.",
                    "Install gh and run `gh auth login` (INSTALL.md step 13).")
    return _row("collab", "Notes between Claudes", "ok", "Configured, and gh is signed in.")


def _source(id_, name, key, p):
    src = p.get("source")
    if not src:
        return _row(id_, name, "off", f"No {key} is set.", f"Set {key} in the config (config.example.toml).")
    if not p.get("known"):
        return _row(id_, name, "broken", f"{key} is {src}, which is not a registered source.",
                    f"Set {key} to one of the registered sources (config.example.toml).")
    return _row(id_, name, "ok", f"Source {src}.")


def _config(p):
    if p["problem"]:
        return _row("config", "Settings file", "broken", p["problem"],
                    "Fix the line the error names: each key may appear only once in the file. See config.example.toml.")
    if not p["exists"]:
        return _row("config", "Settings file", "off", "No settings file, so every setting is at its default.",
                    "Copy config.example.toml to ~/.config/agent-companion/config.toml (INSTALL.md).")
    return _row("config", "Settings file", "ok", f"Read from {p['path']}.")


_BUILDERS = [
    ("config", _config), ("claude", _claude), ("sessions", _sessions), ("devices", _devices), ("tailscale", _tailscale),
    ("push", _push), ("mail", _mail), ("comms", _comms), ("voice_server", _voice_server), ("collab", _collab),
    ("money", lambda p: _source("money", "Money", "money_source", p)),
    ("fitness", lambda p: _source("fitness", "Fitness", "fitness_source", p)),
]


def rows(probes: dict) -> list[dict]:
    """Facts in, rows out. A probe left out gives no row; one that failed gives a broken row."""
    out = []
    for key, build in _BUILDERS:
        p = probes.get(key)
        if p is None:
            continue
        if p.get("error"):
            out.append(_row(key, key.replace("_", " ").capitalize(), "broken", "The check itself failed.",
                            "Try Recheck; if it keeps failing, see INSTALL.md, When something is wrong."))
            continue
        out.append(build(p))
    return out


# --- the probes -------------------------------------------------------------

def _probe_config():
    return {"path": str(config.path()), "exists": config.path().is_file(), "problem": config.problem()}


def _run(cmd, timeout=2.0):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _probe_claude():
    exe = shutil.which("claude")
    if not exe:
        return {"found": False}
    try:
        r = _run([exe, "--version"])
    except (OSError, subprocess.TimeoutExpired):
        return {"found": False}
    return {"found": r.returncode == 0, "version": (r.stdout.strip().split() or [""])[0]}


def _probe_sessions():
    from .sessions import claude_home
    return {"exists": claude_home().is_dir()}


def _probe_tailscale():
    exe = shutil.which("tailscale")
    if not exe:
        return {"installed": False, "serving": False}
    port = str(config.get("server_port", 8770))
    try:
        r = _run([exe, "serve", "status"])
    except (OSError, subprocess.TimeoutExpired):
        return {"installed": True, "serving": False}
    text = r.stdout + r.stderr
    return {"installed": True, "serving": r.returncode == 0 and (f":{port}" in text or f" {port}" in text)}


def _probe_push():
    from . import push
    from ..webpush import VAPID_NAME
    s = push.store()
    try:
        k = json.loads((s.home / VAPID_NAME).read_text())
        vapid = bool(k.get("private") and k.get("public"))
    except (OSError, ValueError, AttributeError):
        vapid = False
    return {"vapid": vapid, "devices": s.devices()}


def _probe_devices():
    return {"count": len(auth.devices())}


def _last_mail_run(source):
    from ..mail import queue
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "agent-companion"
    for log in (state / "mail.log", queue.queue_dir() / "mail.log"):
        try:
            lines = [l for l in log.read_text().splitlines() if " draft rc=" in l]
        except OSError:
            continue
        if lines:
            line = lines[-1]
            rc = line.split(" draft rc=", 1)[1].split(" ", 1)[0]
            return {"ok": rc == "0", "at": line[:19], "note": f"rc={rc}"}
    return None


def _probe_mail():
    from .. import mail
    configured = mail.accounts()
    sources = []
    for account in configured:
        try:
            sources.append(mail.account_source(account))
        except LookupError:
            pass
    source = sources[0] if len(set(sources)) == 1 else ("multiple" if sources else None)
    known = len(sources) == len(configured)
    return {"source": source, "known": known, "accounts": len(configured),
            "last_run": _last_mail_run(source) if source == "gmail" else None}


def _probe_comms():
    from . import comms
    data = comms._data()
    voices_dir = Path(os.environ.get("PIPER_VOICES") or data / "piper" / "voices")
    piper = os.environ.get("PIPER_BIN") or (str(data / "piper/piper/piper") if (data / "piper/piper/piper").is_file()
                                          else shutil.which("piper"))
    cast = Path(os.environ.get("COMMS_CAST") or Path(__file__).resolve().parents[2] / "comms" / "cast.tsv")
    names = set()
    try:
        for line in cast.read_text().splitlines():
            cols = line.split("\t")
            if len(cols) > 1 and not cols[1].startswith("server:") and not line.startswith("#"):
                names.add(cols[1].strip())
    except OSError:
        pass
    have = sum(1 for n in names if (voices_dir / f"{n}.onnx").is_file())
    return {"piper": bool(piper) and Path(piper).exists(), "voices": have, "held": comms.held()}


def _probe_voice_server():
    port = os.environ.get("COMMS_VOICE_PORT") or "9880"
    tokfile = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "agent-companion" / "voice-token"
    try:
        tok = tokfile.read_text().strip()
    except OSError:
        return {"state": "down"}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/voices", headers={"X-Voice-Token": tok})
    try:
        with urllib.request.urlopen(req, timeout=1) as r:
            return {"state": "up" if r.status == 200 else "refused"}
    except urllib.error.HTTPError as exc:
        return {"state": "refused" if exc.code in (401, 403) else "down"}
    except (OSError, ValueError):
        return {"state": "down"}


def _probe_collab():
    from .. import collab
    configured = bool(collab.label() and collab.trusted() and collab.repo())
    gh = False
    if configured and shutil.which("gh"):
        try:
            gh = _run(["gh", "auth", "status"], 3).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            gh = False
    return {"configured": configured, "gh": gh}


def _probe_registry(module, key):
    """None when this install has no such module (no row). Else configured + known."""
    try:
        mod = importlib.import_module(f"agent_companion.{module}")
    except ImportError:
        return None
    reg = getattr(mod, "registered", None)
    names = list(reg()) if callable(reg) else []
    src = str(config.get(key, "") or "").strip()
    return {"registry": True, "source": src or None, "known": bool(src) and src in names}


_PROBES = [
    ("config", _probe_config), ("claude", _probe_claude), ("sessions", _probe_sessions), ("tailscale", _probe_tailscale),
    ("push", _probe_push), ("devices", _probe_devices), ("mail", _probe_mail), ("comms", _probe_comms),
    ("voice_server", _probe_voice_server), ("collab", _probe_collab),
    ("money", lambda: _probe_registry("money", "money_source")),
    ("fitness", lambda: _probe_registry("fitness", "fitness_source")),
]


def run_probes() -> dict:
    if INJECT is not None:
        return INJECT()
    if "pytest" in sys.modules:
        return {}
    out = {}
    for key, fn in _PROBES:
        try:
            out[key] = fn()
        except Exception as exc:  # one broken check never takes the page down
            out[key] = {"error": type(exc).__name__}
    return out


def clear_cache() -> None:
    with _lock:
        _cache.update(at=0.0, rows=None)


def current() -> list[dict]:
    with _lock:
        if _cache["rows"] is not None and time.time() - _cache["at"] < TTL:
            return _cache["rows"]
    result = rows(run_probes())
    with _lock:
        _cache.update(at=time.time(), rows=result)
    return result


@router.get("/api/connections", dependencies=[Depends(auth.require_token)])
def listing():
    return current()


@router.post("/api/connections/recheck", dependencies=[Depends(auth.require_write)])
def recheck():
    clear_cache()
    return {"ok": True}
