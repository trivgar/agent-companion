"""Live Codex CLI sessions: found in /proc, read from their rollout files.

The native `codex` process of an interactive session holds its rollout
(`<codex home>/sessions/YYYY/MM/DD/rollout-<stamp>-<thread id>.jsonl`) open,
so its file descriptors name the thread with no guessing. Only a rollout whose
`session_meta` says `source: "cli"` is a session; `codex exec` jobs say
`exec` and never get a card.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from agent_companion.codex_limits import codex_home


def _ts(value) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def meta(rollout: Path) -> dict:
    """The rollout's session_meta payload, or {} when it has none."""
    try:
        with Path(rollout).open("rb") as fh:
            first = json.loads(fh.readline().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return {}
    payload = first.get("payload") if isinstance(first, dict) else None
    if first.get("type") != "session_meta" or not isinstance(payload, dict):
        return {}
    return payload


def _argv0(d: Path) -> str:
    try:
        raw = (d / "cmdline").read_bytes()
    except OSError:
        return ""
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


def _rollout_of(d: Path, root: str) -> Path | None:
    try:
        fds = sorted((d / "fd").iterdir(), key=lambda p: p.name)
    except OSError:
        return None
    for fd in fds:
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        name = os.path.basename(target)
        if (name.startswith("rollout-") and name.endswith(".jsonl")
                and os.path.realpath(target).startswith(root + os.sep)):
            return Path(target)
    return None


def _pane(d: Path) -> str:
    try:
        raw = (d / "environ").read_bytes()
    except OSError:
        return ""
    for item in raw.split(b"\0"):
        if item.startswith(b"TMUX_PANE="):
            pane = item.split(b"=", 1)[1].decode("utf-8", "replace")
            return pane if re.fullmatch(r"%\d+", pane) else ""
    return ""


def live(proc: Path = Path("/proc"), home: Path | None = None) -> list[dict]:
    """Every interactive Codex session running now, by pid order."""
    root = os.path.realpath(str(Path(home) if home is not None else codex_home()))
    out = []
    try:
        entries = sorted((p for p in Path(proc).iterdir() if p.name.isdigit()), key=lambda p: int(p.name))
    except OSError:
        return []
    for d in entries:
        if os.path.basename(_argv0(d)) != "codex":
            continue
        rollout = _rollout_of(d, root)
        if rollout is None:
            continue
        m = meta(rollout)
        if m.get("source") != "cli" or not m.get("id"):
            continue
        git = m.get("git") if isinstance(m.get("git"), dict) else {}
        out.append({"pid": int(d.name), "thread_id": str(m["id"]), "rollout": rollout,
                    "cwd": str(m.get("cwd") or ""), "branch": str(git.get("branch") or ""),
                    "pane": _pane(d), "started_at": _ts(m.get("timestamp"))})
    return out
