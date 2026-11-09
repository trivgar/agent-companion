#!/usr/bin/env python3
"""Claude Code hook that asks the owner on the dashboard, not in the terminal.

Installed twice by `python -m agent_companion.server.install_hooks`: as the
PermissionRequest hook (every permission prompt) and as the PreToolUse hook on
AskUserQuestion (an agent's multiple-choice question). Each posts the ask to
the dashboard, which pushes a notification and shows it in the Asks tab, and
waits for the answer. Allow or deny goes back to Claude Code as the decision; a
question's picks go back as the tool's own `answers`. It never decides.

It waits as long as the ask is open. The prompt also shows in the session's own
window the whole time (Claude Code's status reads "waiting" while this runs),
so it is answered in whichever place comes first. Answered there, this ends:
No and Esc end it with SIGTERM, and Yes is seen in the session's status,
stamped after this began. When the dashboard goes quiet for QUIET_S or WAIT_S
runs out, the ask leaves the screen and the session, still at its prompt, is
marked waiting again.

Fast and quiet on any failure: it exits 0 and the session's own dialog shows.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
import urllib.request
from pathlib import Path


def _env(key, default=None):
    """AGENT_COMPANION_<KEY>; this bare script cannot import package config."""
    value = os.environ.get("AGENT_COMPANION_" + key)
    return default if value is None else value

WAIT_S = float(_env("ASK_WAIT_S", str(24 * 3600 - 300)))  # under the hook's timeout
QUIET_S = 120                  # dashboard silent this long: the session's own dialog after all
MARK_S = 60                    # how often the session is marked waiting again
POST_S = 25                    # past the push the dashboard sends before it answers
FLUSH_S = 0.3                  # past Claude Code's 100 ms transcript drain
UNSHOWN_S = 540                # a session that never shows it as a prompt
EVERY_S = 1.5


def _url() -> str:
    """Always loopback: the hook routes refuse anything that came through a
    proxy, and `url` (the phone's address) goes through tailscale serve."""
    try:
        from agent_companion import config
        return f"http://127.0.0.1:{int(config.get('server_port', 8770))}"
    except Exception:
        return "http://127.0.0.1:8770"


def _token() -> str:
    """The same file agent_companion.server.auth keeps; read here so the hook
    never imports the web framework."""
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    try:
        return (base / "agent-companion" / "agent-companion-token").read_text().strip()
    except OSError:
        return ""


def _call(path: str, body: dict | None = None, timeout: float = 5):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(_url() + path, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json", "User-Agent": "permission-hook",
                                          "x-agent-companion-token": _token()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _core():
    """agent_companion.notify, or None: the hook never fails over it."""
    try:
        from agent_companion import notify
        return notify
    except Exception:
        return None


def waiting(ev: dict) -> str:
    """Mark the session waiting (its card's mark) and say so; returns the
    speaker, or "". The push is the dashboard's, below."""
    core = _core()
    try:
        return core.announce(ev, push=False) if core else ""
    except Exception:
        return ""


def holding(ev: dict) -> dict | None:
    """Its session, found exactly (by session id, else up the process tree,
    never by folder) while the hook's parents are all alive."""
    core = _core()
    return core.holding_session(ev) if core else None


def status(me: dict | None) -> tuple[str, float]:
    """Claude Code's own word for the session (waiting, busy, idle, gone) and
    when it last changed, from its registry entry."""
    core = _core()
    return core.session_status(me) if core and me else ("", 0.0)


def still_waiting(ev: dict, me: dict | None = None) -> None:
    """Marked waiting again, without a word: the first mark can land before the
    transcript's last write, and a session at a prompt must never read as free
    to type into."""
    core = _core()
    if core:
        core.still_waiting(ev, me)


def decision(behavior: str, message: str = "") -> str:
    d = {"behavior": behavior}
    if message:
        d["message"] = message
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": d}})


def answered(inp: dict, answers: dict) -> str:
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow",
        "permissionDecisionReason": "Answered on the dashboard",
        "updatedInput": dict(inp, answers=answers)}})


def _post(ev: dict, who: str, tool: str, inp: dict) -> dict:
    try:
        a = _call("/api/asks", {"speaker": who, "project": Path(str(ev.get("cwd") or "")).name,
                                "session_id": ev.get("session_id") or "", "tool": tool, "input": inp},
                  timeout=POST_S)
    except Exception:
        a = {}
    return a if isinstance(a, dict) else {}


def _withdrawer(ask):
    def withdraw() -> None:
        if ask:
            try:
                _call(f"/api/asks/{ask}/withdraw", {}, timeout=2)
            except Exception:
                pass
    return withdraw


def question(ev: dict) -> int:
    inp = ev.get("tool_input") if isinstance(ev.get("tool_input"), dict) else {}
    if not inp.get("questions") or inp.get("answers"):
        return 0
    who = waiting(ev)
    me, parent = holding(ev), os.getppid()          # found now, while its tree is whole
    # Esc can end this while the ask is being posted: held until there is an
    # ask to take back off the screen.
    early = []
    signal.signal(signal.SIGTERM, lambda signum, frame: early.append(signum))
    a = _post(ev, who, "AskUserQuestion", inp)
    ask = a.get("id")
    withdraw = _withdrawer(ask)
    if early:
        withdraw()
        return 0
    if not ask or a.get("answerable") is False:
        # The dashboard is down: the terminal's dialog, marked once the line
        # that asked has landed, so the session never reads as free to type into.
        withdraw()
        time.sleep(FLUSH_S)
        still_waiting(ev, me)
        return 0

    def ended(signum, frame):
        withdraw()            # an abort or a shutdown: no dialog follows, so no mark
        sys.exit(0)
    signal.signal(signal.SIGTERM, ended)
    end = time.monotonic() + WAIT_S
    heard = time.monotonic()
    marked = heard - MARK_S                         # marked again at the first look
    while time.monotonic() < end:
        time.sleep(EVERY_S)
        if os.getppid() != parent or status(me)[0] == "gone":
            withdraw()            # its session died: nobody to answer, nothing to mark
            return 0
        if time.monotonic() - marked >= MARK_S:
            still_waiting(ev, me)
            marked = time.monotonic()
        try:
            got = _call(f"/api/asks/{ask}")
        except Exception:
            if time.monotonic() - heard > QUIET_S:
                break                               # the dashboard is gone: the terminal's dialog
            continue
        heard = time.monotonic()
        if got.get("status") == "answered" and isinstance(got.get("answers"), dict):
            print(answered(inp, got["answers"]))
            return 0
        if got.get("status") != "pending":
            break                                   # anything else is not an answer
    withdraw()
    still_waiting(ev, me)                           # the terminal's dialog has it now
    return 0


def permission(ev: dict, t0: float) -> int:
    tool = str(ev.get("tool_name") or "")
    who = waiting(ev)
    me, parent = holding(ev), os.getppid()          # found now, while its tree is whole
    # No or Esc in its window can end this while the ask is being posted:
    # held until there is an ask to take back off the screen.
    early = []
    signal.signal(signal.SIGTERM, lambda signum, frame: early.append(signum))
    a = _post(ev, who, tool, ev.get("tool_input") if isinstance(ev.get("tool_input"), dict) else {})
    ask = a.get("id")
    withdraw = _withdrawer(ask)
    if early:
        withdraw()
        return 0
    if not ask:
        # The dashboard is down: the session's own dialog, open already,
        # marked once the transcript's last line has landed.
        time.sleep(FLUSH_S)
        still_waiting(ev, me)
        return 0

    def ended(signum, frame):
        withdraw()            # answered No or Esc in its window, Stop, an exit: no mark
        sys.exit(0)
    signal.signal(signal.SIGTERM, ended)
    # Settled in its own window, or by Claude Code: Yes there does not end this
    # hook, so the session's own status says it. Its registry stamps each
    # change, so a status that moved after this hook began is the prompt
    # answered, however quick, and a second "waiting" stamped apart from the
    # first is the next prompt, not this one. Only an interactive session shows
    # a prompt: any other keeps the old nine minutes.
    window = (me or {}).get("kind", "interactive") == "interactive"
    start = time.monotonic()
    end = start + WAIT_S
    heard = start
    marked = start - MARK_S                         # marked again at the first look
    shown, latched, off = False, None, 0
    while time.monotonic() < end:
        time.sleep(EVERY_S)
        now_is, at = status(me)
        if os.getppid() != parent or now_is == "gone":
            withdraw()            # its session died: nobody to answer, nothing to mark
            return 0
        settled = False
        if now_is == "waiting":
            if latched is None:
                latched = at
                settled = bool(at) and at > t0 + 1.0          # already the next prompt
            elif at and latched and at != latched:
                settled = True                                 # a new prompt since
            shown, off = True, 0
        elif now_is:
            off += 1
            settled = (bool(at) and at > t0) or (shown and off >= 2)
        settled = settled and window
        if settled:
            withdraw()
            return 0
        if now_is == "waiting" and time.monotonic() - marked >= MARK_S:
            still_waiting(ev, me)
            marked = time.monotonic()
        if not shown and time.monotonic() - start > UNSHOWN_S:
            break                 # never shown as a prompt (a session with no window)
        try:
            st = _call(f"/api/asks/{ask}").get("status")
        except Exception:
            if time.monotonic() - heard > QUIET_S:
                break                               # the dashboard is gone: the session's own dialog
            continue
        heard = time.monotonic()
        if st == "allow":
            print(decision("allow"))
            return 0
        if st == "deny":
            print(decision("deny", "The owner said no, from the dashboard."))
            return 0
        if st != "pending":
            break                                   # anything else is not an answer
    withdraw()
    if status(me)[0] == "waiting":
        still_waiting(ev, me)                       # its own dialog has it now
    return 0


def main() -> int:
    if _env("JOB") or os.environ.get("COMMS_JOB"):
        return 0                                    # a headless job (mail, comms): nobody to ask
    t0 = time.time()                                # the prompt is showing by now
    try:
        ev = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(ev, dict):
        return 0
    tool = str(ev.get("tool_name") or "")
    event = str(ev.get("hook_event_name") or "")
    if tool == "AskUserQuestion":
        if event == "PermissionRequest":
            return 0              # a question, not a permission: its PreToolUse hook asks it
        return question(ev)
    if event == "PreToolUse":
        return 0                  # only AskUserQuestion is hooked there
    return permission(ev, t0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
