"""Make room before a full suite and report what was in the way.

``preflight()`` first checks whether enough memory is available.  When memory
is short, it stops idle Gradle and Kotlin daemons when no Gradle client is
running, reaps configured orphaned test workers, and names large idle
processes in interactive or delegate work.  Named processes are never killed;
the preflight waits up to ``WAIT_S`` for memory to become available.

The module uses only the standard library.  Procfs, signals, and clocks are
injectable so callers can test it without touching live processes.
"""
from __future__ import annotations

import os
import re
import signal
import time
from typing import Callable, Optional

GB = 1024 ** 3
HOG_MIN = 1 * GB          # a process this big and idle is worth naming
SAMPLE_S = 5.0            # CPU sampled over this long; idle is under IDLE_TICKS of it
IDLE_TICKS = 5            # clock ticks (100/s): under 1% of one core over the sample
MIN_AGE_S = 15 * 60       # younger than this is still starting or between steps
WAIT_S = 10 * 60
POLL_S = 30.0
# Interactive sessions and delegate jobs, by cgroup. Long-lived services are excluded.
AGENT_CG = re.compile(r"app-tmux\.slice|delegate|tmux-spawn")
DAEMON = re.compile(r"\b(org\.gradle\.launcher\.daemon\.bootstrap\.GradleDaemon|"
                    r"org\.jetbrains\.kotlin\.daemon\.KotlinCompileDaemon)\b")
GRADLE_CLIENT = re.compile(r"\b(org\.gradle\.wrapper\.GradleWrapperMain|org\.gradle\.launcher\.GradleMain)\b")


def need_bytes(workers: int = 2) -> int:
    """Memory wanted for a full suite: reserve, one job, and 1.5 GB per worker."""
    reserve, per_job = 4 * GB, 2 * GB
    return int(reserve + per_job + workers * 1.5 * GB)


def available(proc: str = "/proc") -> Optional[int]:
    try:
        with open(f"{proc}/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _read(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _info(proc: str, pid: int) -> Optional[dict]:
    """{pid, cmd, rss, cpu (utime+stime ticks), start (ticks), cgroup}."""
    stat = _read(f"{proc}/{pid}/stat")
    if not stat or ")" not in stat:
        return None
    try:
        fields = stat.rsplit(")", 1)[1].split()
        cpu, start, rss_pages = int(fields[11]) + int(fields[12]), int(fields[19]), int(fields[21])
    except (IndexError, ValueError):
        return None
    cmd = _read(f"{proc}/{pid}/cmdline").replace("\0", " ").strip()
    return {"pid": pid, "cmd": cmd, "rss": rss_pages * 4096, "cpu": cpu, "start": start,
            "cgroup": _read(f"{proc}/{pid}/cgroup").strip()}


def _all(proc: str) -> dict[int, dict]:
    out = {}
    try:
        names = os.listdir(proc)
    except OSError:
        return out
    for name in names:
        if name.isdigit():
            info = _info(proc, int(name))
            if info:
                out[info["pid"]] = info
    return out


def _uptime(proc: str) -> Optional[float]:
    try:
        return float(_read(f"{proc}/uptime").split()[0])
    except (IndexError, ValueError):
        return None


def idle(proc: str = "/proc", sample_s: float = SAMPLE_S, sleep: Callable = time.sleep,
         ticks: int = 100) -> list[dict]:
    """Processes old enough and below ``IDLE_TICKS`` CPU over the sample."""
    first = _all(proc)
    sleep(sample_s)
    second = _all(proc)
    up = _uptime(proc)
    out = []
    for pid, later in sorted(second.items()):
        earlier = first.get(pid)
        if not earlier or earlier["start"] != later["start"] or later["cpu"] - earlier["cpu"] >= IDLE_TICKS:
            continue
        age = up - later["start"] / ticks if up is not None else None
        if age is None or age < MIN_AGE_S:
            continue
        out.append({**later, "age_s": int(age)})
    return out


def stop_daemons(quiet: list[dict], everyone: Optional[dict] = None, kill: Callable = os.kill) -> list[dict]:
    """Stop idle Gradle or Kotlin daemons unless a Gradle client is running."""
    procs = everyone if everyone is not None else {}
    if any(GRADLE_CLIENT.search(process["cmd"]) for process in procs.values()):
        return []
    stopped = []
    for process in quiet:
        if DAEMON.search(process["cmd"]):
            try:
                kill(process["pid"], signal.SIGTERM)
                stopped.append(process)
            except OSError:
                pass
    return stopped


def hogs(quiet: list[dict]) -> list[dict]:
    """The idle big processes in interactive or delegate work, biggest first."""
    return sorted((process for process in quiet if process["rss"] >= HOG_MIN and AGENT_CG.search(process["cgroup"])),
                  key=lambda process: -process["rss"])


def _label(process: dict) -> str:
    cmd = process["cmd"]
    match = DAEMON.search(cmd)
    name = match.group(1).rsplit(".", 1)[-1] if match else (cmd.split()[0].rsplit("/", 1)[-1] if cmd else "?")
    return f"{name} {process['rss'] / GB:.1f} GB, idle, {process['age_s'] // 60} min old (pid {process['pid']})"


def _reap() -> int:
    try:
        from . import jobreaper
        return len(jobreaper.reap())
    except Exception:  # noqa: BLE001 - preflight must never stop a suite
        return 0


def preflight(need: Optional[int] = None, proc: str = "/proc", *, sleep: Callable = time.sleep,
              clock: Callable = time.monotonic, kill: Callable = os.kill, reap: Callable = _reap,
              wait_s: float = WAIT_S, poll_s: float = POLL_S, sample_s: float = SAMPLE_S) -> dict:
    """Make room for a full run and return a note. Never raises."""
    need = need_bytes() if need is None else need
    note: dict = {"need_gb": round(need / GB, 1)}
    try:
        free = available(proc)
        if free is None or free >= need:
            note.update(free_gb=round((free or 0) / GB, 1), ok=True)
            return note
        quiet = idle(proc, sample_s, sleep)
        stopped = stop_daemons(quiet, _all(proc), kill)
        reaped = reap()
        note.update(stopped=[_label(process) for process in stopped], reaped=reaped)
        if stopped or reaped:
            sleep(5)
        began = clock()
        free = available(proc) or 0
        named: list[str] = []
        while free < need and clock() - began < wait_s:
            if not named:
                gone = {process["pid"] for process in stopped}
                named = [_label(process) for process in hogs(quiet) if process["pid"] not in gone][:5]
            sleep(poll_s)
            free = available(proc) or 0
        note.update(free_gb=round(free / GB, 1), hogs=named, waited_s=int(clock() - began), ok=free >= need)
    except Exception as exc:  # noqa: BLE001 - preflight must never stop a suite
        note.update(ok=False, error=repr(exc)[:160])
    return note
