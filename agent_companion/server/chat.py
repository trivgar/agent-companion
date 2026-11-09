"""Chat: the owner talks to any running session from the page, with full
authority over it.

The words are typed into the session's tmux pane, as if from the keyboard. What
keeps that the owner's and only the owner's:

  - the routes need an owner key, JSON, and this page's own origin
    (auth.require_write);
  - text goes only into the pane of the exact session named, and only while that
    session's own `claude` process is the pane's foreground job, checked right
    before the keys go in, so a child process or a shell sharing the pane never
    receives it;
  - never into a session stopped at a permission prompt or a question (keys
    there would answer the prompt, not ask the question);
  - every send is written down (chat.jsonl, mode 600) and the page shows it.

A message goes to the session picked on the page, or to the one it names:
"Ada, push it", "@Ada status". Replies are what the session wrote back, read
from the tail of its own transcript (never the whole file). Speaker names come
from agent_companion.registry and the reader, never a fixed cast.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .. import activity, addons, changes, config, ring, tasks
from ..messages import _folder
from . import auth, asks

TITLE = "Chat"

TAIL_BYTES = 600_000          # of each transcript: a reply is near the end
MAX_TAIL_BYTES = 4_800_000    # how far back a window reads when the tail is short
KEEP = 400                    # sends remembered
MAX_TEXT = 4000
SUBMIT_GAP_S = 0.3
SUBMIT_CHECK_S = 0.5
SUBMIT_RETRIES = 2
SUBMIT_WAIT_S = 5.0
MAX_UPLOAD = 20 * 1024 * 1024
MAX_UPLOAD_ENCODED = (MAX_UPLOAD * 4 + 2) // 3 + 4 * 1024
KEEP_UPLOADS_S = 30 * 86400
MAX_SENT = 20 * 1024 * 1024
SENT_IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}
TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
         "application/pdf": ".pdf", "text/plain": ".txt", "text/csv": ".csv", "application/json": ".json",
         "text/markdown": ".md"}
FILE_ID = re.compile(r"^[0-9]{8}-[0-9a-f]{12}(\.[a-z0-9]{1,5})$")
EFFORT_SENT = re.compile(r"/effort (low|medium|high|xhigh|max)")
_SUGGESTION_CACHE: dict[str, tuple[float, str | None]] = {}
_SUGGESTION_CACHE_SECONDS = 5
_CLEARED: dict[str, tuple] = {}
_CLEAR_CMD = re.compile(r"<command-name>\s*/clear\s*</command-name>")
CLEAR_TAIL_BYTES = 200_000


def claude_home() -> Path:
    return Path(str(config.get("claude_home") or Path.home() / ".claude")).expanduser()


def log_path() -> Path:
    return Path(config.env("CHAT_LOG")
                or Path.home() / ".local" / "share" / "agent-companion" / "chat.jsonl")


def seen_path() -> Path:
    return Path(str(config.get("chat_seen_file")
                    or Path.home() / ".local" / "share" / "agent-companion" / "chat-seen.json")).expanduser()


def uploads_path() -> Path:
    return Path(str(config.get("chat_uploads_dir")
                    or Path.home() / ".local" / "share" / "agent-companion" / "uploads")).expanduser()


def host_name() -> str:
    """The optional default direct-chat session, or no default."""
    return str(config.get("chat_host", "") or "").strip()[:40]


def picker_values(key: str) -> list[str]:
    """Configured picker choices only. No installation gets no defaults."""
    values = config.get(key, ()) or ()
    if isinstance(values, str):
        values = values.split(",")
    return [str(value).strip()[:40] for value in values if str(value).strip()][:12]


def targets(rows: list[dict], sessions_dir: Path) -> dict[str, dict]:
    """Every live session with a name, by lower-case name: its pid, pane,
    transcript and state. The name is the speaker the reader found, else the
    session's own. A session with no tmux pane cannot be typed into and is
    left out."""
    out = {}
    for r in rows or []:
        name, pid = r.get("speaker") or r.get("name"), r.get("pid")
        if not name or not pid or r.get("child"):
            continue
        try:
            reg = json.loads((Path(sessions_dir) / f"{int(pid)}.json").read_text())
        except (OSError, ValueError):
            continue
        tm = str(reg.get("tmux") or "")
        pane = tm.rsplit(".", 1)[-1] if "." in tm else ""
        if not re.fullmatch(r"%\d+", pane):
            continue
        out[name.lower()] = {"name": name, "pid": int(pid), "pane": pane, "cwd": r.get("cwd") or "",
                             "session_id": r.get("session_id") or reg.get("sessionId") or "",
                             "status": r.get("status"), "waiting": bool(r.get("waiting")),
                             "project": r.get("project") or ""}
    return out


def route(text: str, known: dict[str, dict], host: str = "") -> tuple[str, str]:
    """(the session it goes to or "", the words to type).

    A message addressed to a known session, "@Ada push it", "Ada, push it"
    or "Ada: push it", goes there without the name. An optional configured
    host receives unaddressed messages, but only if it remains a known target.
    """
    t = (text or "").strip()
    m = re.match(r"^(@)?([A-Za-z][A-Za-z0-9-]{1,30})(\s*[,:]\s*|\s+)(.+)$", t, re.S)
    if m and (m.group(1) or m.group(3).strip()):
        who = m.group(2).lower()
        if who in known:
            return known[who]["name"], m.group(4).strip()
    if host and host.lower() in known:
        return known[host.lower()]["name"], t
    return "", t


def _stat(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return raw[raw.rfind(")") + 2:].split()    # after comm, which may hold spaces


def in_front(pid: int, pane: str, run=subprocess.run) -> tuple[bool, str]:
    """Whether `pid` is the foreground job of tmux pane `pane`: its process
    group is the terminal's foreground group, and the pane's shell is its
    ancestor. Both, or no keys."""
    st = _stat(pid)
    if not st:
        return False, "that session is not running"
    pgrp, tpgid = st[2], st[5]                  # fields 5 and 8 of stat
    if pgrp != tpgid:
        return False, "something else is in front of that session's window"
    try:
        out = run(["tmux", "display-message", "-p", "-t", pane, "#{pane_pid}"],
                  capture_output=True, text=True, timeout=5)
        pane_pid = int(out.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return False, "its tmux window could not be found"
    p, hops = pid, 0
    while p > 1 and hops < 12:
        if p == pane_pid:
            return True, ""
        s = _stat(p)
        if not s:
            break
        p, hops = int(s[1]), hops + 1               # the parent
    return False, "that session is not in its own window"


_ANSI = re.compile(r"\x1b\[([0-?]*)([ -/]*)([@-~])")


def _screen_chars(line: str) -> list[tuple[str, bool]]:
    """The visible characters in a pane line and whether SGR 2 (dim) owns each."""
    out, at, dim = [], 0, False
    for m in _ANSI.finditer(line):
        out.extend((ch, dim) for ch in line[at:m.start()])
        if m.group(3) == "m":
            for word in m.group(1).split(";"):
                try:
                    code = int(word or "0")
                except ValueError:
                    continue
                if code in (0, 22):
                    dim = False
                elif code == 2:
                    dim = True
        at = m.end()
    out.extend((ch, dim) for ch in line[at:])
    return out


def parse_suggestion(screen: str) -> str | None:
    """Read Claude Code's dim next-message guess from one captured pane."""
    lines = str(screen or "").splitlines()
    for n in range(len(lines) - 2, 0, -1):
        chars = _screen_chars(lines[n])
        plain = "".join(ch for ch, _ in chars)
        above = "".join(ch for ch, _ in _screen_chars(lines[n - 1])).strip()
        below = "".join(ch for ch, _ in _screen_chars(lines[n + 1])).strip()
        if not (plain.startswith("❯") and above.startswith("──") and below and set(below) == {"─"}):
            continue
        if "".join(ch for ch, _ in chars[:2]) != "❯\u00a0":
            return None
        words = chars[2:]
        while words and words[0][0] == " ":
            words = words[1:]
        if not words or any(not dim for _, dim in words):
            return None
        value = " ".join("".join(ch for ch, _ in words).split())[:300]
        return value if value and not value.startswith('Try "') else None
    return None


def suggestion(target: dict, run=subprocess.run, now=time.time, fresh: bool = False) -> str | None:
    """Claude Code's dim on-screen guess, only while its own pane is idle."""
    if target.get("waiting") or target.get("status") in ("busy", "waiting"):
        return None
    pane = str(target.get("pane") or "")
    at = now()
    cached = _SUGGESTION_CACHE.get(pane)
    if not fresh and cached and 0 <= at - cached[0] < _SUGGESTION_CACHE_SECONDS:
        return cached[1]
    if not in_front(target["pid"], pane, run=run)[0]:
        value = None
    else:
        try:
            screen = run(["tmux", "capture-pane", "-e", "-p", "-t", pane],
                         capture_output=True, text=True, timeout=5).stdout or ""
        except (OSError, subprocess.SubprocessError):
            value = None
        else:
            value = parse_suggestion(screen)
    _SUGGESTION_CACHE[pane] = (at, value)
    return value


def parse_input(screen: str) -> str | None:
    """What sits typed in Claude Code's input box (dim guesses left out), or
    None when no box shows. The box is the lines from the prompt line to the
    rule under it."""
    lines = str(screen or "").splitlines()
    plain = ["".join(ch for ch, _ in _screen_chars(x)) for x in lines]
    for n in range(len(lines) - 1, 0, -1):
        if not (plain[n].startswith("❯") and plain[n - 1].strip().startswith("──")):
            continue
        out = []
        for m in range(n, len(lines)):
            if m > n and plain[m].strip() and set(plain[m].strip()) == {"─"}:
                return "".join(out)
            chars = _screen_chars(lines[m])[2:] if m == n else _screen_chars(lines[m])
            out.append("".join(ch for ch, dim in chars if not dim))
        return None
    return None


def _still_in_box(pane: str, line: str, run) -> bool:
    try:
        screen = run(["tmux", "capture-pane", "-e", "-p", "-t", pane],
                     capture_output=True, text=True, timeout=5).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return False
    box = parse_input(screen)
    tail = "".join(line.split())[-24:]
    return bool(box and tail and tail in "".join(box.split()))


def target_waiting(target: dict) -> bool:
    """Whether this session is waiting, as the latest reader row says."""
    if target.get("waiting") or target.get("status") == "waiting":
        return True
    for row in _rows():
        if row.get("pid") != target.get("pid"):
            continue
        session_id = str(target.get("session_id") or "")
        if session_id and str(row.get("session_id") or "") not in ("", session_id):
            continue
        return bool(row.get("waiting")) or row.get("status") == "waiting"
    return False


def confirm_effort(target: dict, text: str, run=subprocess.run, sleep=time.sleep,
                   tries: int = 12, every: float = .25) -> bool:
    """Confirm only the effort dialog raised by this exact direct-chat send."""
    match = EFFORT_SENT.fullmatch(str(text).strip())
    if not match:
        return False
    yes = re.compile(r"❯\s*1\.\s*Yes, switch to " + match.group(1) + r"\s*$", re.M)
    for _ in range(tries):
        sleep(every)
        try:
            screen = run(["tmux", "capture-pane", "-p", "-t", target["pane"]],
                         capture_output=True, text=True, timeout=5).stdout or ""
        except (OSError, subprocess.SubprocessError):
            return False
        if "Change effort level?" not in screen or not yes.search(screen):
            continue
        if not in_front(target["pid"], target["pane"], run=run)[0]:
            return False
        try:
            # Deliberate: this exact effort dialog and its selected Yes line
            # are the only prompt this worker is allowed to confirm.
            run(["tmux", "send-keys", "-t", target["pane"], "Enter"], check=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False
        return True
    return False


def send(target: dict, text: str, run=subprocess.run, sleep=time.sleep) -> tuple[bool, str]:
    """Type `text` into the target's pane and press Enter. One line: a newline
    would submit early, so newlines become spaces."""
    # Claude Code's own word first: "waiting" from the moment a prompt or a
    # question shows in its window. Keys typed into a dialog answer it.
    if target_waiting(target):
        return False, f"{target['name']} is waiting on your answer to a question or a permission; answer that first"
    ok, why = in_front(target["pid"], target["pane"], run=run)
    if not ok:
        return False, why
    line = " ".join(str(text).split())[:MAX_TEXT]
    try:
        run(["tmux", "send-keys", "-t", target["pane"], "-l", "--", line], check=True, timeout=5)
        # Words and Enter in one burst read as a paste, and the Enter becomes a
        # newline in the box: a pause between them, then a look.
        sleep(SUBMIT_GAP_S)
        if target_waiting(target):
            return False, "it opened a dialog, not sent"
        run(["tmux", "send-keys", "-t", target["pane"], "Enter"], check=True, timeout=5)
        # A busy session once accepted a message 0.23 s after the old 1.3 s deadline.
        for attempt in range(int(SUBMIT_WAIT_S / SUBMIT_CHECK_S)):
            sleep(SUBMIT_CHECK_S)
            if not _still_in_box(target["pane"], line, run):
                return True, ""
            if target_waiting(target):
                return False, "it opened a dialog, not sent"
            if attempt < SUBMIT_RETRIES:
                run(["tmux", "send-keys", "-t", target["pane"], "Enter"], check=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"tmux refused: {str(exc)[:80]}"
    return False, f"typed, but {target['name']} did not take the Enter; the words are still in its box"


def stop(target: dict, run=subprocess.run) -> tuple[bool, str]:
    """Interrupt it, as Esc does in its window. Same guard as send: its own
    process in front, or no key."""
    ok, why = in_front(target["pid"], target["pane"], run=run)
    if not ok:
        return False, why
    try:
        # Deliberate: Escape interrupts a run. At a dialog it denies, which is
        # the safe side. The routes below record every use as [stop].
        run(["tmux", "send-keys", "-t", target["pane"], "Escape"], check=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"tmux refused: {str(exc)[:80]}"
    return True, ""


def record(entry: dict, path: Path | None = None) -> None:
    path = Path(path or log_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(entry) + "\n")


def sent(path: Path | None = None, limit: int = 60) -> list[dict]:
    try:
        lines = Path(path or log_path()).read_text().splitlines()[-KEEP:]
    except OSError:
        return []
    out = []
    for ln in lines[-limit:]:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def _ts(v) -> float | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _tail(transcript: Path, tail_bytes: int) -> str:
    try:
        size = transcript.stat().st_size
        with transcript.open("rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def replies(transcript: Path, since: float, limit: int = 20) -> list[dict]:
    """What the session wrote back after `since`: its assistant text, from the
    tail of its transcript."""
    out = []
    for line in _tail(transcript, TAIL_BYTES).splitlines():
        if '"assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") != "assistant" or d.get("isSidechain"):
            continue
        at = _ts(d.get("timestamp"))
        if at is None or at < since:
            continue
        text = "".join(c.get("text", "") for c in (d.get("message") or {}).get("content") or []
                       if isinstance(c, dict) and c.get("type") == "text").strip()
        if text:
            out.append({"at": at, "text": text, "id": d.get("uuid") or f"{at}"})
    return out[-limit:]


def transcript_path(t: dict, projects: Path) -> Path:
    """Where a session's transcript is. A row missing its cwd or session id
    (a reader that could not parse it) gets a path that is never there, so
    every reader of it degrades to empty rather than one card taking the
    chat window down."""
    if not t.get("cwd") or not t.get("session_id"):
        return Path(projects) / ".none" / "missing.jsonl"
    return Path(projects) / _folder(t["cwd"]) / f"{t['session_id']}.jsonl"


def thread(known: dict[str, dict], projects: Path, path: Path | None = None) -> list[dict]:
    """The owner's sends and each addressed session's replies since, oldest first."""
    msgs = sent(path)
    out = [{"role": "me", "at": m["at"], "to": m["to"], "text": m["text"], "ok": m.get("ok", True),
            "detail": m.get("detail", ""), "id": f"s{m['at']}"} for m in msgs]
    first: dict[str, float] = {}
    for m in msgs:
        if m.get("ok", True):
            first.setdefault(m["to"].lower(), m["at"])
    for who, since in first.items():
        t = known.get(who)
        if not t or not t["session_id"]:
            continue
        for r in replies(transcript_path(t, projects), since - 1):
            out.append({"role": "agent", "at": r["at"], "from": t["name"], "text": r["text"], "id": r["id"]})
    out.sort(key=lambda m: m["at"])
    return out[-120:]


# ONE SESSION'S WINDOW: its conversation from the tail of its transcript: what
# was asked of it, what it wrote back, a line for each run of tool work between,
# and its questions with the pick.
_WRAPPERS = re.compile(r"<(system-reminder|command-[a-z-]+|local-command-[a-z-]+|task-notification)>.*?</\1>", re.S)


def _prompt_text(content) -> str:
    if isinstance(content, str):
        t = content
    elif isinstance(content, list):
        t = "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    else:
        return ""
    return _WRAPPERS.sub("", t).strip()


def _sent_id(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8", "replace")).hexdigest()[:12]


def _sent_paths(call: dict, cwd: str) -> list[str]:
    files = (call.get("input") or {}).get("files") if isinstance(call.get("input"), dict) else None
    out = []
    for path in files if isinstance(files, list) else []:
        if isinstance(path, str) and path.strip():
            path = path.strip()
            out.append(path if os.path.isabs(path) else os.path.join(cwd or "/", path))
    return out


def sent_file_ok(path: Path) -> Path | None:
    """A regular, non-escaping delivered file within the size cap, or None."""
    try:
        real = path.resolve(strict=True)
        if real.parent != path.parent.resolve(strict=True) or not real.is_file() or real.stat().st_size > MAX_SENT:
            return None
    except (OSError, RuntimeError):
        return None
    return real


def _sent_entry(path: str) -> dict:
    entry = {"name": os.path.basename(path) or "file",
             "kind": "image" if Path(path).suffix.lower() in SENT_IMAGES else "file", "id": _sent_id(path)}
    if sent_file_ok(Path(path)) is None:
        entry["gone"] = True
    return entry


def sent_file(transcript: Path, file_id: str) -> Path | None:
    """The path of a delivered file in this session's transcript, if any."""
    if not re.fullmatch(r"[0-9a-f]{12}", file_id or ""):
        return None
    text = _tail(transcript, MAX_TAIL_BYTES)
    for line in text.splitlines():
        if "SendUserFile" not in line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("type") != "assistant" or data.get("isSidechain") or data.get("isMeta"):
            continue
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        for call in message.get("content") if isinstance(message.get("content"), list) else []:
            if isinstance(call, dict) and call.get("type") == "tool_use" and call.get("name") == "SendUserFile":
                for path in _sent_paths(call, str(data.get("cwd") or "")):
                    if secrets.compare_digest(_sent_id(path), file_id):
                        return Path(path)
    return None


def conversation(transcript: Path, limit: int = 80, tail_bytes: int = TAIL_BYTES) -> list[dict]:
    """[{role: me|agent|work|asked, at, text, tools}] oldest first, the last
    `limit`. A tail short of `limit` reads further back, doubling to
    MAX_TAIL_BYTES. `asked` is one of its questions, with `options`, the
    `answer` ("" while it waits) and `closed` once the session has its result."""
    got = _conversation(transcript, limit, tail_bytes)
    while len(got) < limit and tail_bytes < MAX_TAIL_BYTES:
        try:
            if transcript.stat().st_size <= tail_bytes:
                break
        except OSError:
            break
        tail_bytes = min(tail_bytes * 2, MAX_TAIL_BYTES)
        got = _conversation(transcript, limit, tail_bytes)
    return got


def _conversation(transcript: Path, limit: int, tail_bytes: int) -> list[dict]:
    out: list[dict] = []
    work: list[str] = []
    last = [""]                                  # what the latest tool call was about
    asked: dict[str, list[dict]] = {}            # tool_use id -> its question entries

    def flush_work(at):
        if work:
            out.append({"role": "work", "at": at, "text": "", "tools": list(dict.fromkeys(work)),
                        "last": last[0], "id": f"w{at}"})
            work.clear()

    for line in _tail(transcript, tail_bytes).splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        typ = d.get("type")
        if typ == "attachment" and not d.get("isSidechain"):
            # What the owner sends while it works is absorbed mid-turn and
            # recorded as a queued_command, never a user line.
            a = d.get("attachment") if isinstance(d.get("attachment"), dict) else {}
            origin = a.get("origin") if isinstance(a.get("origin"), dict) else {}
            at = _ts(d.get("timestamp"))
            if (a.get("type") == "queued_command" and a.get("commandMode") == "prompt"
                    and origin.get("kind") == "human" and at is not None):
                t = _prompt_text(a.get("prompt"))
                if t:
                    flush_work(at)
                    out.append({"role": "me", "at": at, "text": t, "id": d.get("uuid") or f"q{at}"})
            continue
        if typ not in ("user", "assistant") or d.get("isSidechain") or d.get("isMeta"):
            continue
        at = _ts(d.get("timestamp"))
        if at is None:
            continue
        content = (d.get("message") or {}).get("content")
        if typ == "user":
            if d.get("toolUseResult") is not None or (isinstance(content, list) and any(
                    isinstance(c, dict) and c.get("type") == "tool_result" for c in content)):
                # The answers, given here or in the terminal, onto the question;
                # a result with none closes it unanswered.
                got = d.get("toolUseResult") if isinstance(d.get("toolUseResult"), dict) else {}
                picks = got.get("answers") if isinstance(got.get("answers"), dict) else {}
                picks = {str(k).strip()[:500]: v for k, v in picks.items()}
                for c in content if isinstance(content, list) else []:
                    for q in asked.get(c.get("tool_use_id") if isinstance(c, dict) else "", []):
                        q["answer"] = str(picks.get(q["text"][:500]) or "")
                        q["closed"] = True
                continue
            t = _prompt_text(content)
            if t:
                flush_work(at)
                out.append({"role": "me", "at": at, "text": t, "id": d.get("uuid") or f"u{at}"})
            continue
        for c in content if isinstance(content, list) else []:
            if not isinstance(c, dict):
                continue
            if (c.get("type") == "tool_use" and str(c.get("name") or "").endswith("text_to_speech")
                    and isinstance(c.get("input"), dict) and str(c["input"].get("text") or "").strip()):
                flush_work(at)
                out.append({"role": "spoke", "at": at, "text": str(c["input"]["text"]).strip(),
                            "id": (d.get("uuid") or f"s{at}") + str(len(out))})
            elif c.get("type") == "tool_use" and c.get("name") == "AskUserQuestion":
                flush_work(at)
                qs = (c.get("input") or {}).get("questions") if isinstance(c.get("input"), dict) else None
                mine = asked.setdefault(str(c.get("id") or ""), [])
                for i, q in enumerate(qs if isinstance(qs, list) else []):
                    if not isinstance(q, dict) or not str(q.get("question") or "").strip():
                        continue
                    e = {"role": "asked", "at": at, "text": str(q["question"]).strip(),
                         "options": [str(o.get("label") or "") for o in q.get("options") or [] if isinstance(o, dict)],
                         "answer": "", "closed": False, "id": f"q{c.get('id') or at}.{i}"}
                    mine.append(e)
                    out.append(e)
            elif c.get("type") == "tool_use" and c.get("name") == "SendUserFile":
                paths = _sent_paths(c, str(d.get("cwd") or ""))
                if paths:
                    flush_work(at)
                    inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                    out.append({"role": "agent", "at": at, "text": str(inp.get("caption") or "").strip(),
                                "files": [_sent_entry(path) for path in paths],
                                "id": (d.get("uuid") or f"f{at}") + str(len(out))})
            elif c.get("type") == "tool_use":
                work.append(str(c.get("name") or "a tool"))
                last[0] = f"{c.get('name') or 'a tool'}: {asks.summary(str(c.get('name') or ''), c.get('input'))}"[:200]
            elif c.get("type") == "text" and c.get("text", "").strip():
                flush_work(at)
                out.append({"role": "agent", "at": at, "text": c["text"].strip(),
                            "id": (d.get("uuid") or f"a{at}") + str(len(out)),
                            "model": str((d.get("message") or {}).get("model") or "")})
    if work:
        flush_work(out[-1]["at"] if out else time.time())
    return out[-limit:]


def seen(path: Path | None = None) -> dict[str, float]:
    try:
        data = json.loads(Path(path or seen_path()).read_text())
        return {str(key): float(value) for key, value in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def mark_seen(session_id: str, at: float | None = None, path: Path | None = None) -> None:
    """Remember that the owner has opened this session's direct chat."""
    if not session_id:
        return
    target = Path(path or seen_path())
    data = seen(target)
    data[session_id] = time.time() if at is None else at
    if len(data) > 500:
        data = dict(sorted(data.items(), key=lambda item: item[1])[-300:])
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(data, file)
    os.replace(temporary, target)


def unread(row: dict, seen_at: dict[str, float]) -> bool:
    reply = row.get("last_reply_at") or 0
    if not reply or row.get("status") == "busy" or row.get("waiting"):
        return False
    return reply > max(seen_at.get(str(row.get("session_id") or ""), 0), row.get("last_prompt_at") or 0)


def cleared_unprompted(transcript: Path) -> bool:
    """Whether the tail ends at a clear command with no later human prompt."""
    try:
        stat = transcript.stat()
    except OSError:
        return False
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _CLEARED.get(str(transcript))
    if cached and cached[0] == key:
        return cached[1]
    tail = _tail(transcript, CLEAR_TAIL_BYTES)
    answer = False
    for line in reversed(tail.splitlines()):
        if '"user"' not in line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if data.get("type") != "user" or data.get("isSidechain") or data.get("isMeta"):
            continue
        content = (data.get("message") or {}).get("content")
        raw = content if isinstance(content, str) else "\n".join(
            item.get("text", "") for item in content or [] if isinstance(item, dict) and item.get("type") == "text")
        if _CLEAR_CMD.search(raw):
            answer = True
            break
        if _prompt_text(content):
            break
    if len(_CLEARED) > 200:
        _CLEARED.clear()
    _CLEARED[str(transcript)] = (key, answer)
    return answer


def save_upload(data: bytes, content_type: str, root: Path | None = None,
                now: float | None = None) -> Path:
    """Keep one supported attachment privately."""
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    extension = TYPES.get(ctype)
    if not extension:
        raise ValueError(f"{ctype or 'that kind of file'} is not something an agent can open")
    if not data:
        raise ValueError("the file was empty")
    if len(data) > MAX_UPLOAD:
        raise ValueError("over 20 MB")
    now = time.time() if now is None else now
    directory = Path(root or uploads_path())
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    for old in directory.iterdir():
        try:
            if now - old.stat().st_mtime > KEEP_UPLOADS_S:
                old.unlink()
        except OSError:
            pass
    name = f"{time.strftime('%Y%m%d', time.localtime(now))}-{secrets.token_hex(6)}{extension}"
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(data)
    return path


def upload_content_too_large(content_length: str | None) -> bool:
    """Whether a request header exceeds the encoded attachment allowance."""
    try:
        return int(content_length or 0) > MAX_UPLOAD_ENCODED
    except (TypeError, ValueError):
        return False


def upload_data_too_large(data: str) -> bool:
    """Whether base64 text exceeds the encoded attachment allowance."""
    return len(data) > MAX_UPLOAD_ENCODED


def upload_path(file_id: str, root: Path | None = None) -> Path | None:
    if not FILE_ID.match(file_id or ""):
        return None
    try:
        directory = Path(root or uploads_path()).resolve(strict=True)
        path = (directory / file_id).resolve(strict=True)
        return path if path.parent == directory and path.is_file() else None
    except (OSError, RuntimeError):
        return None


def effort(transcript: Path, tail_bytes: int = 262_144) -> str | None:
    """The effort value from the newest non-sidechain assistant entry."""
    for line in reversed(_tail(transcript, tail_bytes).splitlines()):
        if '"effort"' not in line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        value = data.get("effort")
        if data.get("type") == "assistant" and not data.get("isSidechain") and isinstance(value, str):
            return value
    return None


_ASKS_PUSH = re.compile(r"\b(push it\?|shall i push|should i push|want me to push|ready to push|say push|"
                        r"push (?:it )?(?:now|live)\?|ok to push|okay to push|go to push)", re.I)
_ASKS_YES = re.compile(r"\b(want me to|should i|shall i|ok if i|okay if i)\b[^?]{0,160}\?\s*$", re.I)


def quick_replies(text: str) -> list[dict]:
    """Safe draft actions for a short yes-or-push question."""
    normalized = " ".join(str(text or "").split())
    if _ASKS_PUSH.search(normalized):
        return [{"action": "say-push", "title": "Push", "words": "push"}]
    if _ASKS_YES.search(normalized):
        return [{"action": "say-yes", "title": "Yes", "words": "yes"}]
    return []


# ROUTES

router = APIRouter()
_reader = None
_NOSTORE = {"Cache-Control": "no-store"}


def known() -> dict[str, dict]:
    """Every session the page can type into, now."""
    global _reader
    try:
        if _reader is None:
            from agent_companion.reader import SessionReader
            _reader = SessionReader(claude_home())
        rows = _reader.snapshot()
    except Exception:
        rows = []
    return {name: target for name, target in targets(rows, claude_home() / "sessions").items()
            if addons.chat_target(dict(target)) is True}


def _rows() -> list[dict]:
    try:
        if _reader is None:
            return []
        return _reader.snapshot()
    except Exception:
        return []


def _agents(k: dict[str, dict]) -> list[dict]:
    return sorted(({"name": t["name"], "project": t["project"], "status": t["status"], "waiting": t["waiting"],
                    "pid": t["pid"], "session_id": t.get("session_id") or ""}
                   for t in k.values()), key=lambda a: a["name"].lower())


def _target(name: str) -> dict | None:
    return known().get(name.lower())


def _chat_facts(target: dict, rows: list[dict]) -> dict:
    """Optional session facts from the reader, never guessed."""
    facts: dict = {"pid": target["pid"], "session_id": target.get("session_id") or ""}
    row = next((item for item in rows if item.get("pid") == target["pid"]), {})
    for key in ("model", "context_pct", "cost_usd", "permission_mode", "last_prompt_at"):
        if row.get(key) is not None:
            facts[key] = row[key]
    helpers = [item for item in rows if asks.is_helper(item) and item.get("root_pid") == target["pid"]]
    facts["helpers"] = len(row.get("subagents") or []) + len(helpers)
    if helpers and facts.get("cost_usd") is not None:
        facts["cost_usd"] = round(float(facts["cost_usd"]) + sum(float(item.get("cost_usd") or 0) for item in helpers), 4)
    value = effort(transcript_path(target, claude_home() / "projects"))
    if value:
        facts["effort"] = value
    return facts


def _chat_payload(name: str) -> dict:
    target = _target(name)
    if not target:
        return {"ok": False, "detail": f"{name} is not running"}
    rows = _rows()
    pending = asks.pending()
    mine = asks.for_agent(pending, rows, target["pid"]) if rows else [
        ask for ask in pending if ask.get("speaker", "").lower() == target["name"].lower()]
    transcript = transcript_path(target, claude_home() / "projects")
    try:
        suggested = suggestion(target)
    except Exception:
        suggested = None
    try:
        job_cards = tasks.tasks(transcript.with_suffix(""))
    except Exception:
        job_cards = []
    return {"ok": True, "name": target["name"], "project": target["project"], "status": target["status"],
            "waiting": target["waiting"] or target["status"] == "waiting", "messages": conversation(transcript),
            "facts": _chat_facts(target, rows), "tasks": job_cards, "asks": mine,
            "suggestion": suggested, "models": picker_values("chat_models"), "efforts": picker_values("chat_efforts")}


def _send(body: dict) -> dict:
    text = str(body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "nothing to send")
    k = known()
    to, words = route(text, k, host_name())
    pick = str(body.get("to") or "").strip()
    if not to and pick:
        to = k[pick.lower()]["name"] if pick.lower() in k else pick
    if not to:
        raise HTTPException(400, "say who to send to: pick a session, or start with its name")
    t = k.get(to.lower())
    if not t:
        ok, why = False, f"{to} is not running in a window this page can reach"
    else:
        ok, why = send(t, words)
    record({"at": time.time(), "to": to, "text": words, "ok": ok, "detail": why})
    if ok and EFFORT_SENT.fullmatch(words) and "pytest" not in __import__("sys").modules:
        def confirm() -> None:
            if confirm_effort(t, words):
                record({"at": time.time(), "to": to, "text": "(effort confirmed)", "ok": True, "detail": ""})
        threading.Thread(target=confirm, daemon=True, name="chat-effort-confirm").start()
    return {"ok": ok, "to": to, "detail": why}


def _matches(target: dict, body: dict) -> bool:
    return body.get("pid") == target["pid"] and (
        not target.get("session_id") or body.get("session_id") == target["session_id"])


def _addon_result(action: str, target: dict, body: dict) -> dict:
    result = addons.chat_action(action, target, body)
    return result if result is not None else {"ok": False, "detail": f"{action} is not available in this install"}


@router.get("/api/cave", dependencies=[Depends(auth.require_token)])
def cave_thread():
    targets_now = known()
    host = host_name()
    return JSONResponse({"ok": True, "host": host, "host_here": host.lower() in targets_now if host else False,
                         "agents": _agents(targets_now), "messages": thread(targets_now, claude_home() / "projects"),
                         "models": picker_values("chat_models"), "efforts": picker_values("chat_efforts")}, headers=_NOSTORE)


@router.get("/api/cave/chat/{name}", dependencies=[Depends(auth.require_token)])
def cave_chat(name: str):
    return JSONResponse(_chat_payload(name), headers=_NOSTORE)


@router.get("/api/cave/changes/{name}", dependencies=[Depends(auth.require_token)])
def cave_changes(name: str):
    target = _target(name)
    if not target:
        return JSONResponse({"ok": False, "detail": f"{name} is not running"}, headers=_NOSTORE)
    got = changes.changes(target["cwd"])
    return JSONResponse({"ok": True, **got} if got is not None else {"ok": False, "detail": "not in a git repository"}, headers=_NOSTORE)


@router.get("/api/cave/diff/{name}", dependencies=[Depends(auth.require_token)])
def cave_diff(name: str, path: str = ""):
    target = _target(name)
    return JSONResponse(changes.diff(target["cwd"], path) if target else {"error": f"{name} is not running"}, headers=_NOSTORE)


@router.get("/api/cave/specs/{name}", dependencies=[Depends(auth.require_token)])
def cave_specs(name: str):
    target = _target(name)
    return JSONResponse(_addon_result("specs", target, {}) if target else {"ok": False, "detail": f"{name} is not running"}, headers=_NOSTORE)


@router.get("/api/cave/spec/{name}", dependencies=[Depends(auth.require_token)])
def cave_spec(name: str, kind: str = "", file: str = "", project: str = ""):
    target = _target(name)
    body = {"kind": kind, "file": file, "project": project}
    return JSONResponse(_addon_result("spec", target, body) if target else {"ok": False, "detail": f"{name} is not running"}, headers=_NOSTORE)


@router.get("/api/cave/files/{name}", dependencies=[Depends(auth.require_token)])
def cave_files(name: str, path: str = ""):
    target = _target(name)
    return JSONResponse(_addon_result("files", target, {"path": path}) if target else {"ok": False, "detail": f"{name} is not running"}, headers=_NOSTORE)


@router.get("/api/cave/read/{name}", dependencies=[Depends(auth.require_token)])
def cave_read(name: str, path: str = ""):
    target = _target(name)
    return JSONResponse(_addon_result("read", target, {"path": path}) if target else {"ok": False, "detail": f"{name} is not running"}, headers=_NOSTORE)


@router.get("/api/cave/activity/{name}", dependencies=[Depends(auth.require_token)])
def cave_activity(name: str):
    target = _target(name)
    if not target:
        return JSONResponse({"ok": False, "detail": f"{name} is not running"}, headers=_NOSTORE)
    return JSONResponse({"ok": True, **activity.activity(transcript_path(target, claude_home() / "projects"))}, headers=_NOSTORE)


@router.post("/api/cave/seen/{name}", dependencies=[Depends(auth.require_write)])
def cave_seen(name: str):
    target = _target(name)
    if not target:
        return {"ok": False, "detail": f"{name} is not running"}
    mark_seen(target.get("session_id") or "")
    return {"ok": True}


@router.post("/api/cave/seen-all", dependencies=[Depends(auth.require_write)])
def cave_seen_all():
    targets_now = known()
    for target in targets_now.values():
        mark_seen(target.get("session_id") or "")
    return {"ok": True, "marked": len(targets_now)}


@router.post("/api/cave/stop/{name}", dependencies=[Depends(auth.require_write)])
def cave_stop(name: str):
    target = _target(name)
    if not target:
        return {"ok": False, "detail": f"{name} is not running"}
    ok, detail = stop(target)
    record({"at": time.time(), "to": target["name"], "text": "[stop]", "ok": ok, "detail": detail})
    return {"ok": ok, "detail": detail}


@router.get("/api/cave/characters", dependencies=[Depends(auth.require_token)])
def cave_characters():
    characters: list[dict] = []
    for target in known().values():
        for item in addons.chat_characters(target):
            name = str(item.get("name") or "").strip()[:40]
            if name and all(str(old.get("name") or "").lower() != name.lower() for old in characters):
                characters.append({"name": name, "custom": bool(item.get("custom")), "held_by": item.get("held_by")})
    return {"characters": characters}


@router.post("/api/cave/character/{name}", dependencies=[Depends(auth.require_write)])
def cave_character(name: str, body: dict = Body(default={})):
    target = _target(name)
    if not target or not _matches(target, body):
        return JSONResponse({"ok": False, "detail": f"{name} is not the session that card showed; nothing was done"}, status_code=409)
    result = _addon_result("character", target, body)
    record({"at": time.time(), "to": target["name"], "text": "[character]", "ok": bool(result.get("ok")),
            "detail": str(result.get("detail") or "")})
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


@router.post("/api/cave/end/{name}", dependencies=[Depends(auth.require_write)])
def cave_end(name: str, body: dict = Body(default={})):
    target = _target(name)
    if not target or not _matches(target, body):
        return JSONResponse({"ok": False, "detail": f"{name} is not the session that card showed; nothing was done"}, status_code=409)
    result = _addon_result("end", target, body)
    record({"at": time.time(), "to": target["name"], "text": "[end]", "ok": bool(result.get("ok")),
            "detail": str(result.get("detail") or "")})
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


@router.post("/api/cave/resume/{name}", dependencies=[Depends(auth.require_write)])
def cave_resume(name: str, body: dict = Body(default={})):
    target = _target(name)
    if not target or not _matches(target, body):
        return JSONResponse({"ok": False, "detail": f"{name} is not the session that card showed; nothing was sent"}, status_code=409)
    text = str(config.get("chat_resume_text") or "Pick up where you left off and say what you are resuming first.").strip()
    transcript = transcript_path(target, claude_home() / "projects")
    if target.get("waiting") or target.get("status") == "waiting":
        ok, detail = False, f"{target['name']} is waiting on your answer to a question or a permission; answer that first"
    elif target.get("status") != "idle":
        ok, detail = False, f"{target['name']} is {target.get('status') or 'in an unknown state'}, not idle"
    elif not cleared_unprompted(transcript):
        ok, detail = False, f"{target['name']} has not just been cleared"
    else:
        ok, detail = send(target, text)
    record({"at": time.time(), "to": target["name"], "text": "[resume]" + (" " + text if ok else ""), "ok": ok, "detail": detail})
    return JSONResponse({"ok": ok, "detail": detail}, status_code=200 if ok else 409)


@router.post("/api/cave/suggest/{name}", dependencies=[Depends(auth.require_write)])
def cave_suggest(name: str, body: dict = Body(default={})):
    target = _target(name)
    if not target:
        return {"ok": False, "detail": f"{name} is not running"}
    if not _matches(target, body):
        return JSONResponse({"ok": False, "detail": f"{name} is not the session that card showed; nothing was sent"}, status_code=409)
    if target.get("waiting") or target.get("status") == "waiting":
        ok, detail, words = False, f"{target['name']} is waiting on your answer to a question or a permission; answer that first", None
    elif target.get("status") != "idle":
        ok, detail, words = False, f"{target['name']} is {target.get('status') or 'in an unknown state'}, not idle", None
    else:
        words = suggestion(target, fresh=True)
        if not isinstance(words, str) or " ".join(words.split()) != " ".join(str(body.get("words") or "").split()):
            ok, detail = False, "the suggestion changed; nothing was sent"
        else:
            ok, detail = send(target, words)
    record({"at": time.time(), "to": target["name"], "text": "[suggested]" + (" " + words if ok else ""),
            "ok": ok, "detail": detail})
    return JSONResponse({"ok": ok, "detail": detail}, status_code=200 if ok else 409)


@router.post("/api/cave/upload", dependencies=[Depends(auth.require_write)])
async def cave_upload(request: Request):
    if upload_content_too_large(request.headers.get("content-length")):
        raise HTTPException(413, "the file is over 20 MB")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "the file did not arrive whole")
    if not isinstance(body, dict):
        raise HTTPException(400, "the file did not arrive whole")
    encoded = str(body.get("data") or "")
    if upload_data_too_large(encoded):
        raise HTTPException(413, "the file is over 20 MB")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "the file did not arrive whole")
    try:
        path = save_upload(data, str(body.get("type") or ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "id": path.name, "path": str(path)}


@router.get("/api/cave/file/{file_id}", dependencies=[Depends(auth.require_token)])
def cave_file(file_id: str):
    path = upload_path(file_id)
    if not path:
        raise HTTPException(404, "no such attachment")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


@router.get("/api/cave/sent/{name}/{file_id}", dependencies=[Depends(auth.require_token)])
def cave_sent(name: str, file_id: str):
    target = _target(name)
    if not target:
        raise HTTPException(404, f"{name} is not running")
    path = sent_file(transcript_path(target, claude_home() / "projects"), file_id)
    if not path:
        raise HTTPException(404, "that file is not one this agent sent")
    real = sent_file_ok(path)
    if not real:
        raise HTTPException(404, "no longer on this computer")
    headers = {"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"}
    media_type = SENT_IMAGES.get(path.suffix.lower())
    if media_type:
        return FileResponse(real, media_type=media_type, headers=headers)
    return FileResponse(real, media_type="application/octet-stream", filename=path.name,
                        content_disposition_type="attachment", headers=headers)


@router.post("/api/cave/send", dependencies=[Depends(auth.require_write)])
def cave_send(body: dict = Body(default={})):
    return _send(body)


@router.post("/api/cave/ask/{ask_id}/answer", dependencies=[Depends(auth.require_write)])
def cave_ask_answer(ask_id: str, body: dict = Body(default={})):
    return asks.asks_answer(ask_id, body)


@router.post("/api/cave/ring/decide", dependencies=[Depends(auth.require_write)])
def cave_ring_decide(body: dict = Body(default={})):
    ask_id, decision = str(body.get("ask") or ""), str(body.get("decision") or "")
    if decision not in asks.DECISIONS:
        raise HTTPException(400, "allow or deny")
    if not ring.consume(ask_id, str(body.get("n") or "")):
        raise HTTPException(403, "that button was already used, or is not from an alert")
    return asks.asks_decide(ask_id, {"decision": decision})


@router.post("/api/cave/ask/{ask_id}", dependencies=[Depends(auth.require_write)])
def cave_ask_decide(ask_id: str, body: dict = Body(default={})):
    return asks.asks_decide(ask_id, body)


@router.post("/api/cave/mark", dependencies=[Depends(auth.require_write)])
def cave_mark(body: dict = Body(default={})):
    target = _target(str(body.get("speaker") or ""))
    if not target:
        return JSONResponse({"ok": False, "detail": "that session is not running"}, status_code=409)
    result = _addon_result("mark", target, body)
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


# Compatibility addresses. The core page uses /api/cave now; existing clients
# can move independently.
@router.get("/api/chat", dependencies=[Depends(auth.require_token)])
def chat_thread():
    return cave_thread()


@router.get("/api/chat/window/{name}", dependencies=[Depends(auth.require_token)])
def chat_window(name: str):
    payload = _chat_payload(name)
    if not payload.get("ok"):
        raise HTTPException(404, f"{name} is not running in a window this page can reach")
    return JSONResponse(payload, headers=_NOSTORE)


@router.post("/api/chat/send", dependencies=[Depends(auth.require_write)])
def chat_send(body: dict = Body(default={})):
    return _send(body)


@router.post("/api/chat/stop/{name}", dependencies=[Depends(auth.require_write)])
def chat_stop(name: str):
    target = _target(name)
    if not target:
        raise HTTPException(404, f"{name} is not running in a window this page can reach")
    ok, detail = stop(target)
    record({"at": time.time(), "to": target["name"], "text": "[stop]", "ok": ok, "detail": detail})
    return {"ok": ok, "detail": detail}
