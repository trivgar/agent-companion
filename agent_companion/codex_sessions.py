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
import shutil
import subprocess
import time
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


TAIL_BYTES = 600_000          # of a rollout: a reply is near the end
MAX_TAIL_BYTES = 4_800_000


def name_of(thread_id: str) -> str:
    """Unique and typeable: "Codex-1041" from a thread id ending ...1041."""
    tail = re.sub(r"[^0-9A-Za-z]", "", str(thread_id))[-4:]
    return f"Codex-{tail or 'session'}"


def _text(payload: dict) -> str:
    parts = payload.get("content") if isinstance(payload.get("content"), list) else []
    return "\n".join(str(c.get("text") or "") for c in parts
                     if isinstance(c, dict) and c.get("type") in ("input_text", "output_text")).strip()


def _his(payload: dict) -> str:
    """His words, or "" for the system blocks Codex sends as the user."""
    if payload.get("type") != "message" or payload.get("role") != "user":
        return ""
    text = _text(payload)
    return "" if text.startswith("<") else text


class CodexReader:
    """Rows for the live sessions, each rollout read once by byte offset."""

    def __init__(self, live=live) -> None:
        self._live = live
        self._state: dict[str, dict] = {}

    def _read(self, path: Path) -> dict:
        key = str(path)
        st = self._state.setdefault(key, {"offset": 0, "status": "idle", "model": "", "last_prompt": "",
                                          "last_prompt_at": None, "last_reply": "", "last_reply_at": None,
                                          "at": None})
        try:
            with path.open("rb") as fh:
                fh.seek(st["offset"])
                chunk = fh.read()
        except OSError:
            return st
        end = chunk.rfind(b"\n")
        if end < 0:
            return st
        st["offset"] += end + 1
        for line in chunk[:end].decode("utf-8", "replace").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            p = d.get("payload") if isinstance(d, dict) and isinstance(d.get("payload"), dict) else {}
            at = _ts(d.get("timestamp"))
            st["at"] = at or st["at"]
            kind = d.get("type")
            if kind == "event_msg" and p.get("type") == "task_started":
                st["status"] = "busy"
            elif kind == "event_msg" and p.get("type") == "task_complete":
                st["status"] = "idle"
                if str(p.get("last_agent_message") or "").strip():
                    st["last_reply"], st["last_reply_at"] = str(p["last_agent_message"]).strip(), at
            elif kind == "turn_context" and p.get("model"):
                st["model"] = str(p["model"])
            elif kind == "response_item" and _his(p):
                st["last_prompt"], st["last_prompt_at"] = _his(p), at
            elif (kind == "response_item" and p.get("type") == "message" and p.get("role") == "assistant"
                  and p.get("phase") == "final_answer" and _text(p)):
                st["last_reply"], st["last_reply_at"] = _text(p), at
        return st

    def rows(self) -> list[dict]:
        now, out, seen = time.time(), [], set()
        for s in self._live():
            path = Path(s["rollout"])
            seen.add(str(path))
            st = self._read(path)
            cwd = s.get("cwd") or ""
            out.append({
                "pid": s["pid"], "session_id": s["thread_id"], "app_url": "", "name": name_of(s["thread_id"]),
                "speaker": "", "child": False, "parent_pid": None, "root_pid": None, "job": "", "job_about": "",
                "label": "", "project": os.path.basename(cwd.rstrip("/")) if cwd else "",
                "started_at": s.get("started_at"), "cwd": cwd, "title": "", "status": st["status"],
                "waiting": False, "model": st["model"], "context_used": 0, "context_limit": 0,
                "context_pct": 0.0, "cost_usd": None, "priced": False, "tokens": {}, "subagent_cost_usd": 0.0,
                "subagent_tokens": 0, "subagents": [], "permission_mode": "", "last_prompt": st["last_prompt"],
                "last_prompt_at": st["last_prompt_at"], "last_reply_at": st["last_reply_at"],
                "last_reply": st["last_reply"], "cache": {}, "entrypoint": "codex",
                "last_activity": round(now - st["at"], 1) if st["at"] else None,
                "backend": "codex", "pane": s.get("pane") or "", "rollout": str(path),
                "branch": s.get("branch") or ""})
        for key in [k for k in self._state if k not in seen]:
            del self._state[key]
        return out


def _tail(path: Path, tail_bytes: int) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _conversation(path: Path, limit: int, tail_bytes: int) -> list[dict]:
    out: list[dict] = []
    work: list[str] = []
    last = [""]

    def flush(at):
        if work:
            out.append({"role": "work", "at": at, "text": "", "tools": list(dict.fromkeys(work)),
                        "last": last[0], "id": f"w{at}"})
            work.clear()

    for line in _tail(path, tail_bytes).splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("type") != "response_item":
            continue
        p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        at = _ts(d.get("timestamp"))
        if at is None:
            continue
        mine = _his(p)
        if mine:
            flush(at)
            out.append({"role": "me", "at": at, "text": mine, "id": str(p.get("id") or f"u{at}")})
        elif p.get("type") in ("custom_tool_call", "function_call"):
            name = str(p.get("name") or "a tool")
            work.append(name)
            detail = p.get("input") if p.get("type") == "custom_tool_call" else p.get("arguments")
            last[0] = f"{name}: {' '.join(str(detail or '').split())}"[:200]
        elif p.get("type") == "message" and p.get("role") == "assistant" and _text(p):
            if p.get("phase") == "final_answer":
                flush(at)
                out.append({"role": "agent", "at": at, "text": _text(p),
                            "id": str(p.get("id") or f"a{at}") + str(len(out))})
            else:
                # Commentary between tool calls folds into the work line; it
                # names the work only when no tool has yet.
                if not work:
                    work.append("note")
                    last[0] = " ".join(_text(p).split())[:200]
    if work:
        flush(out[-1]["at"] if out else time.time())
    return out[-limit:]


def conversation(rollout: Path, limit: int = 80, tail_bytes: int = TAIL_BYTES) -> list[dict]:
    """[{role: me|agent|work, at, text, ...}] oldest first, the last `limit`,
    in the shape the chat window draws for a Claude session."""
    got = _conversation(Path(rollout), limit, tail_bytes)
    while len(got) < limit and tail_bytes < MAX_TAIL_BYTES:
        try:
            if Path(rollout).stat().st_size <= tail_bytes:
                break
        except OSError:
            break
        tail_bytes = min(tail_bytes * 2, MAX_TAIL_BYTES)
        got = _conversation(Path(rollout), limit, tail_bytes)
    return got


MAX_TEXT = 4000


def queue(target: dict, text: str, run=subprocess.run, which=shutil.which) -> tuple[bool, str]:
    """Put `text` on the session's queue with Codex's own command. A busy
    session runs it when its turn ends, as Claude takes a sent line."""
    binary = which("codex")
    if not binary:
        return False, "codex is not installed"
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    line = " ".join(str(text).split())[:MAX_TEXT]
    try:
        done = run([binary, "queue", "--thread", str(target["thread_id"]), "--message", line],
                   capture_output=True, text=True, timeout=20, env=env)
    except subprocess.TimeoutExpired:
        return False, "codex queue timed out"
    except OSError:
        return False, "codex is not installed"
    if done.returncode == 0 and "Queued message" in (done.stdout or ""):
        return True, ""
    first = next((l.strip() for l in (done.stderr or "").splitlines() if l.strip()), "")
    return False, first or f"codex queue failed (exit {done.returncode})"
