"""The sessions view: one row per live Claude Code session, plus the spend
strip. Reads only; the rows are the library's, nothing is assembled here."""
from __future__ import annotations

import os
import threading
from pathlib import Path

from fastapi import APIRouter, Depends

from .. import bgjobs, clearline, codex_limits, config
from ..gitstate import GitState
from ..limits import LimitsCache
from ..reader import SessionReader, order
from ..usage import UsageIndex
from . import auth

TITLE = "Sessions"
router = APIRouter()

_lock = threading.Lock()
_cache: dict[str, dict] = {}


def claude_home() -> Path:
    return Path(str(config.get("claude_home") or Path.home() / ".claude")).expanduser()


def data_dir() -> Path:
    return Path(str(config.get("data_dir") or Path.home() / ".local" / "share" / "agent-companion")).expanduser()


def _parts() -> dict:
    """Readers keep watermarks, so one set per home, made on first use."""
    home = claude_home()
    with _lock:
        if str(home) not in _cache:
            cache = data_dir() if config.get("data_dir") else home
            if config.get("data_dir"):
                os.makedirs(cache, exist_ok=True)
            _cache[str(home)] = {
                "reader": SessionReader(home), "git": GitState(),
                "usage": UsageIndex(home, cache / "usage-cache.json"),
                "limits": LimitsCache(home, cache / "limits-cache.json"),
            }
        return _cache[str(home)]


# Reads carry prompts, replies and folders: the key, as every write.
@router.get("/api/sessions", dependencies=[Depends(auth.require_token)])
def sessions():
    p = _parts()
    rows = order(p["reader"].snapshot())
    line = clearline.get()
    for r in rows:
        try:
            g = p["git"].for_repo(r["cwd"]) if r.get("cwd") else None
        except Exception:
            g = None
        r["branch"] = (g or {}).get("branch", "")
        r["clear_line"] = line
        r["needs_clear"] = clearline.over(r, line)
    _add_card_data(rows)
    return rows


def _add_card_data(rows: list[dict]) -> None:
    """Put current chat-only facts on the rows without changing their identity."""
    from . import chat

    try:
        targets = chat.targets(rows, chat.claude_home() / "sessions")
    except Exception:
        targets = {}
    try:
        seen_at = chat.seen()
    except Exception:
        seen_at = {}
    for row in rows:
        try:
            row["unread"] = chat.unread(row, seen_at)
        except Exception:
            row["unread"] = False
        name = str(row.get("speaker") or row.get("name") or "").lower()
        target = targets.get(name)
        try:
            row["suggestion"] = chat.suggestion(target) if target and chat.addons.chat_target(dict(target)) is True else None
        except Exception:
            row["suggestion"] = None
    bgjobs.annotate(rows)


@router.get("/api/usage", dependencies=[Depends(auth.require_token)])
def usage():
    p = _parts()
    p["usage"].refresh()
    out = p["usage"].snapshot()
    try:
        out["limits"] = p["limits"].get()
    except Exception as exc:
        out["limits"] = {"available": False, "reason": type(exc).__name__}
    try:
        out["codex"] = codex_limits.read()
    except Exception:
        out["codex"] = {"enabled": False}
    return out


