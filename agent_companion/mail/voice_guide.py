"""A short guide to how the owner writes, built from their own SENT mail, so
drafts sound like them. Read tools only; the file is private (mode 600) and
lives outside every repo."""
from __future__ import annotations

import os
from pathlib import Path

from . import claude
from .gmail import PREFIX, drafter_allowed_tools
from .. import config

PROMPT = Path(__file__).with_name("prompts") / "voice_guide.md"


def path() -> Path:
    env = config.env("VOICE_GUIDE")
    if env:
        return Path(env)
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "agent-companion" / "voice-guide.md"


def load() -> str:
    try:
        return path().read_text().strip()
    except OSError:
        return ""


def build() -> Path:
    text = claude.run(PROMPT.read_text(), drafter_allowed_tools(), timeout=600,
                      need_tools=PREFIX).strip()
    if len(text) < 20:
        raise RuntimeError("the guide came back empty")
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text + "\n")
    os.chmod(p, 0o600)
    return p
