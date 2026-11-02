"""Codex plan-limit readings from local rollout logs.

Codex records the plan usage that its CLI received in recent rollout logs. A
dashboard poll must not reread each log, so this reader considers only recent
dated folders, tails the newest candidate with a usable reading, and retains
that parsed reading until the file changes.

It answers in the shape the pages read: the plan's long window (a week on
most plans, whatever its length) at the top level, a 5-hour window under
five_hour when the plan has one, and why when there is nothing to show. A
window is not shown once its reset has passed or once its reading is older
than the window itself, and a 5-hour window dropping never hides the long one.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

TAIL_BYTES = 256 * 1024
DAYS = 8
MAX_FILES = 12
FIVE_HOUR_MINUTES = 5 * 60


def codex_home() -> Path:
    """The configurable Codex home, without creating it."""
    return Path(str(config.get("codex_home") or Path.home() / ".codex")).expanduser()


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _timestamp(value: object) -> float | None:
    number = _number(value)
    if number is not None:
        return number / 1000 if number > 1e11 else number
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _reset_at(window: dict, read_at: float) -> float | None:
    reset = _timestamp(window.get("resets_at"))
    if reset is not None:
        return reset
    seconds = _number(window.get("resets_in_seconds"))
    return read_at + seconds if seconds is not None and seconds >= 0 else None


class CodexLimits:
    """Read the newest local Codex plan limit without repeatedly parsing logs."""

    def __init__(self, home: Path | str | None = None) -> None:
        self.home = Path(home) if home is not None else codex_home()
        # path -> (mtime nanoseconds, size, parsed reading or None)
        self._parsed: dict[Path, tuple[int, int, dict | None]] = {}

    @property
    def sessions(self) -> Path:
        return self.home / "sessions"

    def _recent_logs(self, now: float) -> list[Path]:
        logs: list[Path] = []
        day = datetime.fromtimestamp(now)
        for offset in range(DAYS):
            folder = self.sessions / (day - timedelta(days=offset)).strftime("%Y/%m/%d")
            try:
                logs.extend(path for path in folder.iterdir()
                            if path.name.startswith("rollout-") and path.suffix == ".jsonl")
            except OSError:
                continue

        def mtime(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        return sorted(logs, key=mtime, reverse=True)[:MAX_FILES]

    def _reading(self, path: Path) -> dict | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        marker = (stat.st_mtime_ns, stat.st_size)
        cached = self._parsed.get(path)
        if cached and cached[:2] == marker:
            return cached[2]
        value = self._tail_reading(path)
        self._parsed[path] = (*marker, value)
        return value

    @staticmethod
    def _tail_reading(path: Path) -> dict | None:
        try:
            with path.open("rb") as source:
                source.seek(0, 2)
                source.seek(max(0, source.tell() - TAIL_BYTES))
                lines = source.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                event = json.loads(line)
                payload = event.get("payload")
                if (event.get("type") != "event_msg" or not isinstance(payload, dict)
                        or payload.get("type") != "token_count"):
                    continue
                limits = payload.get("rate_limits")
                read_at = _timestamp(event.get("timestamp"))
                if not isinstance(limits, dict) or read_at is None:
                    continue
                windows = []
                for name in ("primary", "secondary"):
                    window = limits.get(name)
                    used = _number(window.get("used_percent")) if isinstance(window, dict) else None
                    minutes = _number(window.get("window_minutes")) if isinstance(window, dict) else None
                    if used is None or minutes is None or minutes <= 0:
                        continue
                    windows.append({"used_percent": used, "window_minutes": minutes,
                                    "resets_at": _reset_at(window, read_at)})
                # A reading needs the plan's long window: a line with only a
                # 5-hour one is passed over for the line before it.
                if any(round(window["window_minutes"]) != FIVE_HOUR_MINUTES for window in windows):
                    plan = limits.get("plan_type")
                    return {"read_at": read_at, "windows": windows,
                            "plan": plan if isinstance(plan, str) else None}
            except (TypeError, ValueError, AttributeError):
                continue
        return None

    @staticmethod
    def _window(window: dict | None, read_at: float, now: float) -> tuple[dict | None, str]:
        """One window as the page reads it, or why it is not shown: its reset
        has passed, or its reading is older than the window itself."""
        if window is None:
            return None, ""
        reset = window.get("resets_at")
        known = isinstance(reset, (int, float))
        if known and reset <= now:
            return None, "reset"
        if read_at + float(window["window_minutes"]) * 60 <= now:
            return None, "old"
        return {"utilization": round(float(window["used_percent"])),
                "resets_at": datetime.fromtimestamp(reset, timezone.utc).isoformat() if known else None,
                "resets_in_seconds": round(max(0.0, reset - now), 1) if known else None}, ""

    def read(self, now: float | None = None) -> dict:
        """The plan's long window at the top level, a 5-hour one under
        five_hour, or why there is no figure. enabled is False only when there
        is no Codex sessions folder at all."""
        now = time.time() if now is None else now
        if not self.sessions.is_dir():
            return {"enabled": False, "available": False, "why": "no Codex sessions folder"}
        reading = next((value for path in self._recent_logs(now) if (value := self._reading(path))), None)
        if reading is None:
            return {"enabled": True, "available": False, "why": f"no Codex run in {DAYS} days"}
        windows = reading["windows"]
        five = next((window for window in windows
                     if round(window["window_minutes"]) == FIVE_HOUR_MINUTES), None)
        long_window = next(window for window in windows if window is not five)
        age = round(max(0.0, now - reading["read_at"]), 1)
        top, problem = self._window(long_window, reading["read_at"], now)
        if top is None:
            why = ("the window reset since the last Codex run" if problem == "reset"
                   else "the last Codex reading is older than its window")
            return {"enabled": True, "available": False, "why": why, "age_seconds": age}
        five_hour, _ = self._window(five, reading["read_at"], now)
        return {"enabled": True, "available": True, **top, "age_seconds": age,
                "window_minutes": round(float(long_window["window_minutes"])), "plan": reading["plan"],
                "why": "", "five_hour": five_hour}


_readers: dict[str, CodexLimits] = {}


def read(now: float | None = None) -> dict:
    """Read the configured Codex home, reusing its mtime-aware reader."""
    home = codex_home()
    key = str(home)
    if key not in _readers:
        _readers[key] = CodexLimits(home)
    return _readers[key].read(now)
