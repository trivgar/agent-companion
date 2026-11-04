"""Periodically terminate stuck Claude Code snapshot-shell jobs.

An orphan, a snapshot shell whose Claude owner has gone, is stuck once it is
``job_reaper_after_min`` old. A job under a live session is that session's own
and left alone, unless ``job_reaper_max_hours`` is set: then it is stuck past
that age too (a forgotten ``until ...; do sleep 5; done`` waiter runs for
days). A command matching ``job_reaper_keep``, meant to run until someone stops
it, is never stuck. Each job is judged again before every signal pass, and
every pid's identity checked, so a recycled pid or a job that stopped being
stuck is never signalled. ``python3 -m agent_companion.jobreaper --dry-run``
lists what a pass would stop and signals nothing.

The server's worker is off until ``job_reaper_every_min`` is set: it
terminates processes, and an owner who never asked for that should not find a
dev server gone. A pass run by a timer of your own (``main``) does not read it.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time
from typing import NamedTuple

from . import bgjobs, config

DEFAULT_EVERY_MIN = 0             # off unless the owner sets job_reaper_every_min
DEFAULT_AFTER_MIN = 60
GRACE_S = 10.0
LOG_NAME = "job-reaper.jsonl"


def _minutes(key: str, default: int) -> int:
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def every_s() -> int:
    """The interval between reaper passes.  Zero disables the worker."""
    return _minutes("job_reaper_every_min", DEFAULT_EVERY_MIN) * 60


def after_s() -> int:
    """The minimum age of an orphan snapshot shell before it is terminated."""
    return _minutes("job_reaper_after_min", DEFAULT_AFTER_MIN) * 60


def max_s() -> int:
    """How old a job under a live session may grow before it is stuck too
    (``job_reaper_max_hours``). 0, the default, leaves every such job alone."""
    raw = config.get("job_reaper_max_hours", 0)
    if isinstance(raw, bool):
        return 0
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        return 0
    return int(hours * 3600) if math.isfinite(hours) and hours > 0 else 0


def keep_pattern() -> re.Pattern | None:
    """``job_reaper_keep``: commands never reaped, matched case-insensitively
    against the whole command before any redaction. None when unset. Raises
    re.error, or TypeError for a setting that is not text."""
    raw = config.get("job_reaper_keep", "")
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise TypeError("job_reaper_keep must be text")
    return re.compile(raw, re.IGNORECASE)


def problem() -> str:
    """Why a pass reaps nothing, or "" when the settings can be used. A keep
    pattern that cannot be read stops the reaper: it is there to protect jobs."""
    try:
        keep_pattern()
    except (re.error, TypeError) as exc:
        return f"job_reaper_keep is not a valid pattern ({exc}); nothing is reaped"
    return ""


class _Rules(NamedTuple):
    after_s: int
    max_s: int
    keep: re.Pattern | None


def _rules(minimum_age_s: int | None = None) -> _Rules | None:
    """One pass's rules, or None while ``problem()`` says why."""
    try:
        keep = keep_pattern()
    except (re.error, TypeError):
        return None
    after = after_s() if minimum_age_s is None else max(0, int(minimum_age_s))
    return _Rules(after, max_s(), keep)


def _span(seconds: int) -> str:
    if seconds and seconds % 3600 == 0:
        return f"{seconds // 3600} h"
    if seconds and seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds} s"


def data_dir() -> Path:
    raw = config.get("data_dir") or Path.home() / ".local" / "share" / "agent-companion"
    return Path(str(raw)).expanduser()


def log_path(directory: Path | None = None) -> Path:
    return Path(directory or data_dir()) / LOG_NAME


def is_claude(proc: str, pid: int) -> bool:
    """Whether a process is a live Claude Code launcher."""
    try:
        with open(f"{proc}/{pid}/comm") as fh:
            if fh.read().strip().lower() == "claude":
                return True
    except OSError:
        pass
    command = bgjobs.argv(proc, pid)
    if not command:
        return False
    name = os.path.basename(command[0]).lower()
    if name == "claude":
        return True
    return name in {"node", "nodejs"} and any("claude" in part.lower() for part in command[1:])


def has_claude_ancestor(proc: str, pid: int) -> bool:
    """Whether *pid* or any current ancestor is a live Claude process."""
    seen: set[int] = set()
    current = int(pid)
    for _ in range(64):
        if current <= 1 or current in seen:
            return False
        seen.add(current)
        if is_claude(proc, current):
            return True
        details = bgjobs.stat(proc, current)
        if not details:
            return False
        current = details[0]
    return True                 # unknown ancestry is not safe to reap


def _snapshot(proc: str, pid: int) -> tuple[int, int, list[str]] | None:
    details = bgjobs.stat(proc, pid)
    if not details:
        return None
    parent, started = details
    command = bgjobs.argv(proc, pid)
    if not bgjobs.is_snapshot_shell(command):
        return None
    return parent, started, command


def _judge(proc: str, pid: int, now: float, boot: float | None, ticks: int,
           rules: _Rules) -> dict | None:
    """The stuck row for one process, or None when it is not a stuck job."""
    candidate = _snapshot(proc, pid)
    if not candidate:
        return None
    parent, started, command = candidate
    # A nested shell belongs to the outer snapshot-shell job.
    if bgjobs.is_snapshot_shell(bgjobs.argv(proc, parent)):
        return None
    if rules.keep and rules.keep.search(bgjobs.raw_command(command)):
        return None
    age = bgjobs._age(started, boot, now, ticks)
    if age is None:
        return None
    # A live Claude owner, direct or further up, makes it that session's job.
    if has_claude_ancestor(proc, parent):
        if not rules.max_s or age <= rules.max_s:
            return None
        why = f"over {_span(rules.max_s)}"
    elif age < rules.after_s:
        return None
    else:
        why = f"orphaned, over {_span(rules.after_s)}"
    return {"pid": pid, "ppid": parent, "start_ticks": started, "age_s": age,
            "cmd": bgjobs.command_of(command, 160), "why": why}


def stuck(proc: str = "/proc", now: float | None = None,
          minimum_age_s: int | None = None) -> list[dict]:
    """The snapshot-shell jobs a pass would stop, oldest first, each with why:
    orphans past ``job_reaper_after_min`` (or *minimum_age_s*) and, once
    ``job_reaper_max_hours`` is set, jobs under a live session past it."""
    rules = _rules(minimum_age_s)
    if rules is None:
        return []
    try:
        moment = time.time() if now is None else float(now)
        boot = bgjobs.boot_time(proc)
        if boot is None:
            return []
        ticks = bgjobs._clock_ticks()
        rows = []
        for name in os.listdir(proc):
            if name.isdigit():
                row = _judge(proc, int(name), moment, boot, ticks, rules)
                if row:
                    rows.append(row)
        return sorted(rows, key=lambda row: (-row["age_s"], row["pid"]))
    except (OSError, ValueError):
        return []


def _same_process(proc: str, pid: int, start_ticks: int) -> bool:
    details = bgjobs.stat(proc, pid)
    return bool(details and details[1] == start_ticks)


def _tree(proc: str, root: int) -> list[tuple[int, int]]:
    """The current process subtree in children-first order, with identities."""
    try:
        children: dict[int, list[int]] = {}
        starts: dict[int, int] = {}
        for name in os.listdir(proc):
            if not name.isdigit():
                continue
            pid = int(name)
            details = bgjobs.stat(proc, pid)
            if details:
                children.setdefault(details[0], []).append(pid)
                starts[pid] = details[1]
    except OSError:
        return []
    for group in children.values():
        group.sort()
    found: list[tuple[int, int]] = []
    todo: list[tuple[int, bool]] = [(root, False)]
    seen: set[int] = set()
    while todo:
        pid, visited = todo.pop()
        if pid in seen and not visited:
            continue
        if visited:
            if pid in starts:
                found.append((pid, starts[pid]))
            continue
        seen.add(pid)
        # A command can itself launch another Claude process.  Its descendants
        # are a separate live session, never part of an abandoned shell tree.
        if pid != root and is_claude(proc, pid):
            continue
        todo.append((pid, True))
        todo.extend((child, False) for child in reversed(children.get(pid, [])))
    return found


def kill(row: dict, proc: str = "/proc", *, signal_fn=None, sleep_fn=None,
         grace_s: float = GRACE_S, own_pid: int | None = None,
         now: float | None = None, minimum_age_s: int | None = None) -> list[int]:
    """Terminate a stuck snapshot-shell tree and return signalled pids.

    The root is judged again by stuck()'s rules before each SIGTERM, so a job
    that is no longer stuck is left alone. After the grace, every process of
    the tree still running is forced by its own identity, even once the shell
    itself has gone, and a recycled pid is never signalled.
    """
    try:
        root = int(row["pid"])
        start = int(row["start_ticks"])
    except (KeyError, TypeError, ValueError):
        return []
    rules = _rules(minimum_age_s)
    if rules is None:
        return []
    moment = time.time() if now is None else float(now)
    boot, ticks = bgjobs.boot_time(proc), bgjobs._clock_ticks()

    def still_stuck() -> bool:
        again = _judge(proc, root, moment, boot, ticks, rules)
        return bool(again and again["start_ticks"] == start)

    if not still_stuck():
        return []
    signal_fn = os.kill if signal_fn is None else signal_fn
    sleep_fn = time.sleep if sleep_fn is None else sleep_fn
    mine = os.getpid() if own_pid is None else own_pid
    targets = [(pid, started) for pid, started in _tree(proc, root)
               if pid not in {1, mine} and not is_claude(proc, pid)]
    sent: list[int] = []
    for pid, started in targets:
        if not still_stuck():
            return sent
        if not _same_process(proc, pid, started):
            continue
        try:
            signal_fn(pid, signal.SIGTERM)
            sent.append(pid)
        except OSError:
            pass
    if not sent:
        return []
    try:
        sleep_fn(max(0.0, float(grace_s)))
    except (TypeError, ValueError):
        pass
    for pid, started in targets:
        if not _same_process(proc, pid, started):
            continue
        try:
            signal_fn(pid, signal.SIGKILL)
        except OSError:
            pass
    return sent


def log_kill(row: dict, directory: Path | None = None, now: float | None = None) -> None:
    """Append one JSONL audit record.  Logging errors never stop reaping."""
    try:
        when = time.time() if now is None else float(now)
        record = {"at": datetime.fromtimestamp(when, timezone.utc).isoformat(timespec="seconds"),
                  "pid": int(row["pid"]), "ppid": int(row["ppid"]),
                  "age_s": int(row["age_s"]), "why": str(row.get("why", "")),
                  "cmd": str(row["cmd"])[:160]}
        path = log_path(directory)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except (KeyError, OSError, TypeError, ValueError):
        pass


def reap(proc: str = "/proc", directory: Path | None = None, now: float | None = None,
         minimum_age_s: int | None = None, *, signal_fn=None, sleep_fn=None) -> list[dict]:
    """Kill and audit every currently stuck snapshot-shell job."""
    moment = time.time() if now is None else float(now)
    reaped = []
    for row in stuck(proc, moment, minimum_age_s):
        sent = kill(row, proc, signal_fn=signal_fn, sleep_fn=sleep_fn,
                    now=moment, minimum_age_s=minimum_age_s)
        if sent:
            log_kill(row, directory, moment)
            reaped.append(row)
    return reaped


class Watcher:
    """One daemon worker that reaps immediately and then at the configured rate."""
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def tick(self) -> list[dict]:
        return reap(directory=self.directory)

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            self.tick()
            interval = every_s()
            if interval <= 0:
                return
            self.stop_event.wait(interval)

    def start(self) -> None:
        if "pytest" in sys.modules or every_s() <= 0:
            return
        self.thread = threading.Thread(target=self._loop, name="job-reaper", daemon=True)
        self.thread.start()


def start(directory: Path | None = None) -> Watcher | None:
    """Start the configured worker outside tests, returning it when enabled."""
    if "pytest" in sys.modules or every_s() <= 0:
        return None
    watcher = Watcher(directory)
    watcher.start()
    return watcher


def main(argv: list[str] | None = None) -> int:
    """One pass from the command line, for a timer of your own instead of the
    server's worker. --dry-run lists what would go and signals nothing. Always
    exits 0, so the timer never reads as failed."""
    args = sys.argv[1:] if argv is None else list(argv)
    if any(arg in ("-h", "--help") for arg in args):
        print("usage: python3 -m agent_companion.jobreaper [--dry-run]")
        return 0
    if any(arg != "--dry-run" for arg in args):
        print("job reaper: unsupported arguments")
        return 0
    try:
        why = problem()
        if why:
            print(f"job reaper: {why}")
            return 0
        if "--dry-run" in args:
            rows = stuck()
            for row in rows:
                print(f"would kill pid={row['pid']} age={row['age_s'] / 3600:.1f}h "
                      f"why={row['why']} cmd={row['cmd']}")
            print(f"job reaper: {len(rows)} stuck job(s), dry run")
            return 0
        print(f"job reaper: killed {len(reap())} stuck job(s)")
    except Exception:
        print("job reaper: no jobs reaped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
