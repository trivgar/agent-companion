"""New session: start Claude Code in a configured project from the page.

The projects are the shared ``[projects]`` list (``agent_companion.projects``).
The browser sends a project's key, never a path, so a configured folder is the
only place a start can happen. A row's ``key`` and ``label`` default to its
name, and its tmux ``prefix`` to a tmux-safe form of the name.
``[new_session]`` adds ``claude_args``, words every start passes to claude
(none by default), ``default_model``, and ``launchers``: other ways to start a
session the page may offer, each ``{key, label, program, model}``. A launcher
runs ``program`` (a wrapper on this machine, one word, never shell) in place
of ``claude`` with its own ``--model``, for example Claude Code pointed at
another provider by a script that sets its environment and execs claude. The
browser sends a launcher's key, never a program; no key is plain claude.

The command is typed into the new pane's own shell, as a restore does: the
login shell's PATH and profile apply, and the pane outlives claude, so a start
that fails leaves its window to look at. A start counts only once a claude
process is seen in that folder on that pane's terminal.

Pure but for launch(), which drives tmux through an injectable runner and
refuses under pytest unless handed a fake one.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config
from . import projects as _projects
from . import restore

MAX_PROJECTS = 40
MAX_MESSAGE = 4000
VERIFY_S = 12.0          # how long a start may take before it is called failed
POLL_S = 0.5
PROC = Path("/proc")
# The process names a started Claude Code runs as: an npm install shows as node.
NAMES = {"claude", "node"}
_TMUX_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")   # C0, DEL and C1


def _table() -> dict:
    table = config.get("new_session", {})
    return table if isinstance(table, dict) else {}


def default_model() -> str:
    """``[new_session].default_model``, or "" for Claude Code's own default."""
    model = _table().get("default_model")
    return model.strip()[:120] if isinstance(model, str) else ""


MAX_LAUNCHERS = 8
_PROGRAM = re.compile(r"[A-Za-z0-9_~./+-]{1,200}")


def launchers() -> tuple[dict, ...]:
    """``[[new_session.launchers]]`` rows as ``{key, label, program, model}``,
    each key once. A row without a key or a one-word program is skipped:
    the program is typed into a shell, so nothing in it may be shell."""
    rows = _table().get("launchers")
    if not isinstance(rows, list):
        return ()
    out, keys = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = " ".join(str(row.get("key") or "").split())[:40]
        program = str(row.get("program") or "").strip()
        program = os.path.expanduser(program) if program.startswith("~/") else program
        model = row.get("model")
        model = model.strip()[:120] if isinstance(model, str) else ""
        if not key or key.casefold() in keys or not _PROGRAM.fullmatch(program) or program.startswith("-"):
            continue
        keys.add(key.casefold())
        label = " ".join(str(row.get("label") or "").split())[:60] or key
        out.append({"key": key, "label": label, "program": program, "model": model})
        if len(out) >= MAX_LAUNCHERS:
            break
    return tuple(out)


def launcher(key) -> dict | None:
    """The configured launcher for `key` (exact), else None."""
    if not isinstance(key, str):
        return None
    return next((l for l in launchers() if l["key"] == key), None)


def launcher_listing() -> list[dict]:
    """What the page may show: key and label only, never the program."""
    return [{"key": l["key"], "label": l["label"]} for l in launchers()]


def claude_args() -> list[str]:
    """``[new_session].claude_args``: words every start passes to claude, such
    as a permission mode. Anything but a list of text is none."""
    args = _table().get("claude_args")
    if not isinstance(args, list):
        return []
    return [a for a in args if isinstance(a, str) and a.strip()]


def safe_name(name: str) -> str:
    """A tmux-safe form of a name: tmux reads "." and ":" in a target."""
    return _TMUX_UNSAFE.sub("-", name.strip()).strip("-")[:60] or "claude"


def projects() -> tuple[dict, ...]:
    """Every configured project as ``{key, label, cwd, prefix}``, each key
    once. ``cwd`` never goes back to the browser."""
    out, keys = [], set()
    for row in _projects.rows()[:MAX_PROJECTS]:
        key = " ".join((row["key"] or "").split()) or row["name"]
        if key.casefold() in keys:
            continue
        try:
            cwd = str(Path(row["path"]).expanduser().resolve())
        except (OSError, RuntimeError):
            continue
        keys.add(key.casefold())
        out.append({"key": key, "label": " ".join((row["label"] or "").split()) or row["name"], "cwd": cwd,
                    "prefix": safe_name(row["prefix"] or row["name"])})
    return tuple(out)


def project(key, configured: tuple[dict, ...] | None = None) -> dict | None:
    """The configured row for `key`, else None. Only an exact key: a path, a
    label or anything not a string finds nothing."""
    if not isinstance(key, str):
        return None
    return next((p for p in (projects() if configured is None else configured) if p["key"] == key), None)


def clean_message(raw) -> tuple[str, str]:
    """(the first message as one line, an error in words or ""). Absent is
    fine. Over MAX_MESSAGE is measured on what was sent. Newlines become
    spaces, as a chat send does: the command is one typed line. So does every
    other control character, which typed into a shell acts on the line instead
    of landing in it (a ^C drops it, and what follows runs as its own command).
    A message starting with "-" gets a leading space so claude never reads it
    as one of its own flags."""
    if raw is None:
        return "", ""
    if not isinstance(raw, str):
        return "", "the first message must be text"
    if len(raw) > MAX_MESSAGE:
        return "", "the first message is over 4,000 characters"
    words = " ".join(_CONTROL.sub(" ", raw).split())
    if words.startswith("-"):
        words = " " + words
    return words, ""


def tmux_name(prefix: str, taken: set[str]) -> str:
    """The prefix if no tmux session has it, else <prefix>-2, -3, ... A restore
    keys on the tmux name, so it is unique."""
    if prefix not in taken:
        return prefix
    n = 2
    while f"{prefix}-{n}" in taken:
        n += 1
    return f"{prefix}-{n}"


def command(name: str, message: str, args: list[str] | None = None, model: str = "",
            program: str = "claude") -> str:
    """The line typed into the new pane. Every argument quoted, so the message
    reaches claude as one argument and nothing in it is shell. The session is
    named after its tmux session. `program` is a launcher's, else claude."""
    parts = [shlex.quote(program), *(shlex.quote(a) for a in args or ())]
    if model:
        parts += ["--model", shlex.quote(model)]
    parts += ["--name", shlex.quote(name)]
    if message:
        parts.append(shlex.quote(message))
    return " ".join(parts)


def busy_note(names: list[str], label: str) -> str:
    """"Ada is in Site", "Ada and Lin are in Site", or "" for nobody."""
    if not names:
        return ""
    who = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    who = who[:1].upper() + who[1:]
    return f"{who} {'is' if len(names) == 1 else 'are'} in {label}"


def _inside(cwd: str, root: str) -> bool:
    cwd = (cwd or "").rstrip("/")
    return cwd == root or cwd.startswith(root + "/")


def availability(cwd: str) -> tuple[bool, str]:
    """Whether a configured folder is usable, without saying where it is."""
    try:
        if not os.path.exists(cwd):
            return False, "Folder does not exist"
        if not os.path.isdir(cwd):
            return False, "Path is not a folder"
    except OSError:
        return False, "Folder cannot be read"
    return True, ""


def listing(rows, configured: tuple[dict, ...] | None = None) -> list[dict]:
    """Each project's key, label, live sessions ({speaker, pid}), the warning
    line, and whether its folder is there. A helper (child) is not a session.
    No path leaves here."""
    out = []
    for p in projects() if configured is None else configured:
        here = [r for r in rows or [] if isinstance(r, dict) and r.get("pid") and not r.get("child")
                and _inside(str(r.get("cwd") or ""), p["cwd"])]
        sessions = [{"speaker": str(r.get("speaker") or ""), "pid": int(r["pid"])} for r in here]
        names = [s["speaker"] or "an unnamed session" for s in sessions]
        available, reason = availability(p["cwd"])
        out.append({"key": p["key"], "label": p["label"], "sessions": sessions,
                    "note": busy_note(names, p["label"]), "available": available, "reason": reason})
    return out


def find_claude(cwd: str, tty: str, proc: Path | None = None) -> int | None:
    """The pid of a claude process (NAMES) whose cwd is `cwd` and whose stdin
    is `tty` (the new pane's), else None. Processes it may not read are
    skipped."""
    if not tty:
        return None
    root = Path(proc or PROC)
    want = os.path.realpath(cwd)
    try:
        entries = list(root.iterdir())
    except OSError:
        return None
    for d in entries:
        if not d.name.isdigit():
            continue
        try:
            if (d / "comm").read_text().strip() not in NAMES:
                continue
            if os.readlink(d / "cwd") != want or os.readlink(d / "fd" / "0") != tty:
                continue
        except OSError:
            continue
        return int(d.name)
    return None


# ---- the launcher -------------------------------------------------------------
# Module attributes, so tests swap them: RUN is the runner launch() uses when
# none is passed, SLEEP and CLOCK pace the 12 s check.
_REAL_RUN = subprocess.run
RUN = subprocess.run
SLEEP = time.sleep
CLOCK = time.monotonic
_LOCK = threading.Lock()      # two taps never pick the same tmux name


def _result(ok: bool, status: int, name: str, detail: str, log: str, pid: int | None = None) -> dict:
    return {"ok": ok, "status": status, "tmux": name, "pid": pid, "detail": detail, "log": log}


def _why(exc: Exception) -> str:
    err = getattr(exc, "stderr", None) or getattr(exc, "output", None) or str(exc)
    if isinstance(err, bytes):
        err = err.decode("utf-8", "replace")
    return " ".join(str(err).split())[:160] or type(exc).__name__


def pane_tail(run, tmux: str, target: str, lines: int = 3) -> list[str]:
    """The pane's last non-blank lines, for saying why a start failed."""
    try:
        out = run([tmux, "capture-pane", "-p", "-t", target], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    got = [l.strip() for l in (out or "").splitlines() if l.strip()]
    return got[-lines:]


def no_claude_words(name: str, tail: list[str]) -> str:
    seen = f'Its window last showed: "{" | ".join(tail)[:300]}".' if tail else "Its window was blank."
    return (f"No Claude started in {name} within {int(VERIFY_S)} seconds. {seen} "
            f"The tmux session {name} is left open for a look.")


def _new_session(run, tmux: str, name: str, cwd: str) -> None:
    """Create the detached tmux session, in a user scope of its own when one
    can be made: a tmux server born inside the dashboard service's cgroup dies
    with its next restart. Without one, plain tmux."""
    plain = [tmux, "new-session", "-d", "-s", name, "-c", cwd]
    scope = shutil.which("systemd-run")
    if scope:
        try:
            run([scope, "--user", "--scope", "--quiet", "--collect",
                 f"--unit=agent-companion-newsession-{name}-{int(time.time())}"] + plain,
                check=True, capture_output=True, text=True, timeout=15)
            return
        except (OSError, subprocess.SubprocessError):
            pass
    run(plain, check=True, capture_output=True, text=True, timeout=15)


def launch(proj: dict, message: str, run=None, find=None, launcher: dict | None = None) -> dict:
    """Start claude in a new tmux session in proj's folder and say so only
    once its process is seen in that pane. Returns {ok, status, tmux, pid,
    detail, log}: `detail` is for the page and may quote the pane; `log` is
    safe to record. A `launcher` row runs its program and model instead; its
    program must exec claude, since the start counts only once claude runs."""
    run = run or RUN
    if run is _REAL_RUN and "pytest" in sys.modules:
        return _result(False, 503, "", "sessions are not started under tests", "refused under pytest")
    find = find or find_claude
    cwd = proj["cwd"]
    if not os.path.isdir(cwd):
        return _result(False, 500, "", f"{proj['label']}'s folder is not there, so nothing was started",
                       "folder missing")
    tmux = shutil.which("tmux")
    if not tmux:
        return _result(False, 500, "", "tmux is not installed, so nothing was started", "no tmux")
    with _LOCK:
        taken = restore.running_tmux_sessions(run)
        if taken is None:
            return _result(False, 500, "", "tmux did not answer, so nothing was started", "tmux did not answer")
        name = tmux_name(proj["prefix"], taken)
        target = f"={name}:"
        try:
            _new_session(run, tmux, name, cwd)
            tty = run([tmux, "display-message", "-p", "-t", target, "#{pane_tty}"],
                      check=True, capture_output=True, text=True, timeout=5).stdout.strip()
            if launcher:
                line = command(name, message, claude_args(), launcher.get("model") or "",
                               launcher.get("program") or "claude")
            else:
                line = command(name, message, claude_args(), default_model())
            run([tmux, "send-keys", "-t", target, "-l", "--", line], check=True, timeout=5)
            run([tmux, "send-keys", "-t", target, "Enter"], check=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:
            return _result(False, 500, name, f"tmux refused to start {name}: {_why(exc)}", "tmux refused")
    deadline = CLOCK() + VERIFY_S
    while True:
        pid = find(cwd, tty)
        if pid:
            return _result(True, 200, name, "", "started", pid)
        if CLOCK() >= deadline:
            break
        SLEEP(POLL_S)
    return _result(False, 500, name, no_claude_words(name, pane_tail(run, tmux, target)),
                   f"no claude process within {int(VERIFY_S)} s")


def log_entry(who: str, key: str, name: str, ok: bool, detail: str, message_len: int,
              now: float | None = None, launcher: str = "") -> dict:
    """One line for the chat log (``server.chat.record``): the message's
    length, never its words. `to` and `text` are what the chat thread reads
    on every line. A launcher start names its key."""
    e = {"at": time.time() if now is None else now, "to": name or "new session",
         "text": f"[new session: {key}" + (f", {launcher}]" if launcher else "]"), "ok": ok, "detail": detail,
         "who": who, "project": key, "tmux": name, "message_len": message_len}
    if launcher:
        e["launcher"] = launcher
    return e
