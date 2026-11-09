#!/usr/bin/env python3
"""Stop hook: speak the reply's closing clip.

A reply ends with one line that opens with the speaker glyph. This speaks it
(at most MAX_WORDS words) through speak.sh. Any failure is silent and exits 0:
a comms nicety must never get in the way of a turn.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

GLYPH = "\U0001F50A"
MAX_WORDS = 40
# The marker may sit behind a quote, bullet or emphasis the model wrapped it in.
CLIP_RE = re.compile(r"^[ \t>*_-]*" + GLYPH + r"️?[ \t*_:]*(.*)$", re.M)


def clean(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_`]", "", text.replace(GLYPH, " "))
    return " ".join(text.split())


def clip_line(msg: str) -> str:
    """The LAST glyph line plus the rest of its paragraph, capped; '' if none."""
    found = list(CLIP_RE.finditer(msg or ""))
    if not found:
        return ""
    m = found[-1]
    text = clean(m.group(1) + msg[m.end():].split("\n\n", 1)[0])
    words = text.split()
    if len(words) > MAX_WORDS:
        text = " ".join(words[:MAX_WORDS]).rstrip(",;:") + "..."
    return text


def speak_script() -> str:
    return os.environ.get("COMMS_SPEAK") or str(Path(__file__).resolve().parent / "speak.sh")


def main() -> int:
    try:
        if os.environ.get("COMMS_JOB") or os.environ.get("AGENT_COMPANION_JOB"):
            return 0
        payload = json.load(sys.stdin)
        msg = payload.get("last_assistant_message") if isinstance(payload, dict) else None
        if not isinstance(msg, str):
            return 0
        text = clip_line(msg)
        if text:
            subprocess.run(["bash", speak_script(), text], timeout=120,
                           stdin=subprocess.DEVNULL, capture_output=True)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
