"""The add-on controlled hold for routine spoken comms."""
from __future__ import annotations

from pathlib import Path

from . import addons, agentvibes


def reason(_world: dict | None = None) -> str | None:
    """A configured add-on may hold routine clips; the core never invents one."""
    return "add-on" if addons.comms_busy() else None


def held(tts: Path) -> bool:
    """An add-on can add a hold but cannot remove a manual one."""
    return bool(reason() or agentvibes.held(tts))
