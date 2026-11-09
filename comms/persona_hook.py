#!/usr/bin/env python3
"""Tell a session how its speaker talks (SessionStart and UserPromptSubmit).

Reads personas/<Name>.md for the session's speaker and hands it to the model as
context: on start, when the speaker changed, and every REMIND prompts. Nothing
for a speaker with no persona file. Fails silent.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TTS = Path(os.environ.get("TTS_DIR") or HERE)
STATE_DIR = Path(os.environ.get("COMMS_STATE_DIR")
                 or Path.home() / ".local/share/agent-companion/comms")
REMIND = 30
KEEP = 200


def speaker() -> str:
    try:
        out = subprocess.run(["bash", str(TTS / "identity.sh"), "get"], capture_output=True,
                             text=True, timeout=8)
        return out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


def context(name: str, text: str) -> str:
    return (f"YOUR SPEAKING VOICE: you speak to the owner as {name}. Your clip for each reply is "
            f"its LAST paragraph: one line starting with \U0001F50A, under ~40 words; the Stop hook "
            f"speaks it. Write it in this character's voice, as described below. Your written "
            f"replies stay plain and clear. Anything that needs the owner always comes through "
            f"plainly. The clip is opened for you: never introduce yourself in it."
            f"\n\n{text.strip()}")


def main() -> None:
    if os.environ.get("COMMS_JOB") or os.environ.get("AGENT_COMPANION_JOB"):
        return
    try:
        ev = json.load(sys.stdin)
    except ValueError:
        ev = {}
    event = ev.get("hook_event_name") or "UserPromptSubmit"
    sid = str(ev.get("session_id") or "")
    name = speaker()
    f = TTS / "personas" / f"{name}.md" if name else None
    if not f or not f.is_file():
        return
    state_file = STATE_DIR / "persona-sent.json"
    try:
        state = json.loads(state_file.read_text())
    except (OSError, ValueError):
        state = {}
    last = state.get(sid) or {}
    n = int(last.get("n") or 0) + 1
    send = event == "SessionStart" or last.get("name") != name or n >= REMIND
    state[sid] = {"name": name, "n": 0 if send else n}
    if len(state) > KEEP:
        state = dict(list(state.items())[-KEEP:])
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = state_file.with_name(state_file.name + ".tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, state_file)
    except OSError:
        pass
    if send:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                                 "additionalContext": context(name, f.read_text())}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
