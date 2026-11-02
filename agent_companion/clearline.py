"""The configurable context line that flags a session for /clear."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

from . import config

DEFAULT = 400_000
LOW, HIGH = 50_000, 2_000_000


def _path() -> Path:
    override = config.env("CLEARLINE")
    if override:
        return Path(override)
    from .server import sessions
    return sessions.data_dir() / "clear-line.json"


def _default() -> int:
    value = config.get("clear_line", DEFAULT)
    return value if isinstance(value, int) and not isinstance(value, bool) and LOW <= value <= HIGH else DEFAULT


def get() -> int:
    """Return the saved line, or the configured default before it is saved."""
    try:
        data = json.loads(_path().read_text())
    except FileNotFoundError:
        return _default()
    except (OSError, ValueError):
        return DEFAULT
    value = data.get("tokens") if isinstance(data, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and LOW <= value <= HIGH else DEFAULT


def put(tokens) -> int:
    """Save a whole-number context line within the supported range."""
    if isinstance(tokens, bool) or not isinstance(tokens, int) or not LOW <= tokens <= HIGH:
        raise ValueError(f"tokens must be a whole number from {LOW:,} to {HIGH:,}")
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"tokens": tokens}))
    os.replace(tmp, path)
    return tokens


def over(row: dict, line: int) -> bool:
    """Whether a top-level session has reached the context line."""
    used = row.get("context_used")
    return not row.get("child") and isinstance(used, (int, float)) and not isinstance(used, bool) and used >= line


def over_file() -> Path:
    """Who is past the line, for whatever else reads it (a session's own
    prompt hook can). Set by clear_over_file; under data_dir by default."""
    override = config.env("CLEAR_OVER") or config.get("clear_over_file")
    if override:
        return Path(str(override)).expanduser()
    from .server import sessions
    return sessions.data_dir() / "needs-clear.json"


def read_over(path: Path | None = None) -> dict:
    try:
        data = json.loads(Path(path or over_file()).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _short(tokens: int) -> str:
    return f"{round(tokens / 1000)}k"


class Watcher:
    """Record sessions past the line and push once per crossing. While
    asleep() says the owner sleeps, the push waits, unsent, for them to
    wake; the flag is written either way."""

    def __init__(self, snapshot, send, path: Path | None = None, *, asleep=None):
        self.snapshot, self.send = snapshot, send
        self.asleep = asleep or (lambda: False)
        self.path = Path(path) if path else None
        self._stop = threading.Event()
        self.thread = None

    def tick(self) -> list[str]:
        path = self.path or over_file()
        try:
            rows = self.snapshot()
        except Exception:
            return []
        try:
            sleeping = bool(self.asleep())
        except Exception:
            sleeping = False
        before = read_over(path).get("over") or {}
        before = before if isinstance(before, dict) else {}
        line, now, pushed, flagged = get(), time.time(), [], {}
        for row in rows:
            sid = row.get("session_id")
            if not isinstance(sid, str) or not sid or not over(row, line):
                continue
            was = before.get(sid)
            was = was if isinstance(was, dict) else {}
            entry = {"speaker": row.get("speaker") or row.get("name") or "",
                     "project": row.get("project") or "", "used": int(row["context_used"]),
                     "since": was.get("since") or now, "pushed": bool(was.get("pushed"))}
            if not entry["pushed"] and not sleeping:
                name = entry["speaker"] or "A session"
                project = f" ({entry['project']})" if entry["project"] else ""
                url = f"/?agent={quote(entry['speaker'], safe='')}" if entry["speaker"] else "/"
                try:
                    sent = self.send("Needs a /clear", f"{name}{project} is at {_short(entry['used'])}: needs a /clear",
                                     url, f"clear-{sid[:8]}")
                except Exception:
                    sent = False
                if sent:
                    entry["pushed"] = True
                    pushed.append(sid)
            flagged[sid] = entry
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps({"line": line, "at": now, "over": flagged}))
            os.replace(tmp, path)
        except OSError:
            pass
        return pushed

    def _loop(self) -> None:
        while not self._stop.wait(60):
            self.tick()

    def start(self) -> None:
        if "pytest" in sys.modules:
            return
        self.thread = threading.Thread(target=self._loop, name="clear-line", daemon=True)
        self.thread.start()
