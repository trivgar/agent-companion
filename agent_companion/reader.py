"""Turn Claude Code's files into a list of session views.

Knows nothing about AgentVibes: the speaker overlay activates only when
~/.claude/tts/agents.tsv happens to exist, and is absent otherwise. Everything
else here reads Claude Code's own files.
"""
from __future__ import annotations

import datetime
import json
import re
import threading
import time
from pathlib import Path

from agent_companion.notify import WAITING_DIR
from agent_companion.transcript import _zero_tokens
from agent_companion.pricing import context_limit, cost_usd, rewarm_usd, warm_usd
from agent_companion.liveness import proc_environ, proc_ppid
from agent_companion.registry import live_sessions
from agent_companion.transcript import TranscriptReader


# A subagent never registers as a process -- `~/.claude/sessions` holds only
# `kind: interactive` -- so the only evidence one exists is its transcript.
# Neither does it speak: announcements go out under the parent's name. So it
# cannot be a speaker in the room, and it should not pretend to be one. It gets a label instead, derived from its own opening
# prompt, which is the one thing the file is guaranteed to start with.
SUBAGENT_RECENT_SECS = 90.0

# A listed subagent that wrote in the last 30 s reads "busy", else "idle". Its
# file cannot tell finished from mid-run (see the loop in _snapshot), so the
# word only ever means "wrote lately".
SUBAGENT_BUSY_SECS = 30.0

# How far up the process tree to look for a spawning session. A child sits a
# few levels under its parent (claude, a shell, maybe a script); notify.py's
# walk from a hook uses the same bound for the same shape.
SPAWN_DEPTH = 12


def spawning_session(pid: int, session_pids: set[int], ppid_of=proc_ppid) -> int | None:
    """The pid of the nearest live session above this one's process.

    0 when there is none: the walk reached init, went round a loop in a
    malformed /proc, or ran out of SPAWN_DEPTH. None when /proc cannot say at
    all, so the caller falls back rather than guessing "no".
    """
    current = ppid_of(pid)
    if current is None:
        return None
    seen = {pid}
    for _ in range(SPAWN_DEPTH):
        if current is None or current <= 1 or current in seen:
            return 0
        if current in session_pids:
            return current
        seen.add(current)
        current = ppid_of(current)
    return 0


def spawned_by_session(pid: int, session_pids: set[int], ppid_of=proc_ppid) -> bool | None:
    """Is another live session an ancestor of this one's process? None when
    /proc cannot say. spawning_session() says which one."""
    parent = spawning_session(pid, session_pids, ppid_of)
    return None if parent is None else bool(parent)


# A session started for a job rather than by a person: a background run some
# host launches detached (setsid), so no session is its ancestor and the
# process tree cannot tell it from a person's. The host says so in the run's
# environment instead. JOB_ENV names the job ("summary", "digest", any word
# the host picks) and ABOUT_ENV, optionally, whose work it is about. A host
# that only silences speech on its headless runs (SILENT_ENV, the same
# variable the Stop hook that speaks reads) still marks the run as not a
# person's, and it reads as the job "headless".
JOB_ENV = "AGENT_COMPANION_JOB"
ABOUT_ENV = "AGENT_COMPANION_ABOUT"
SILENT_ENV = "CLAUDE_TTS_SILENT"
_JOB_KEYS = (JOB_ENV, ABOUT_ENV, SILENT_ENV)
_JOB_MAX = 64                # a word for a chip, not a paragraph


def job_of(env: dict) -> tuple[str, str]:
    """(job, about) from a session's environment: ("", "") for a person's."""
    def clean(v) -> str:
        return " ".join(str(v or "").split())[:_JOB_MAX]
    job = clean(env.get(JOB_ENV))
    if not job and str(env.get(SILENT_ENV) or ""):
        job = "headless"
    return job, (clean(env.get(ABOUT_ENV)) if job else "")


def root_of(pid: int, rows_by_pid: dict[int, dict]) -> int | None:
    """Up through parent_pid to the first row that is not a child: the session
    a chain of helpers was ultimately started from. None when the chain
    reaches nothing (no parent, a parent that is itself a parentless child,
    or a loop)."""
    seen = {pid}
    current = (rows_by_pid.get(pid) or {}).get("parent_pid")
    while current and current not in seen:
        row = rows_by_pid.get(current)
        if row is None:
            return None
        if not row.get("child"):
            return current
        seen.add(current)
        current = row.get("parent_pid")
    return None


def _iso_epoch(value) -> float | None:
    """An ISO timestamp ("2026-10-11T19:39:19.000Z") as epoch seconds; None
    for anything else."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _started(ms) -> float | None:
    """The registry's startedAt (milliseconds) as epoch seconds; None when it
    is absent."""
    if isinstance(ms, bool) or not isinstance(ms, (int, float)) or ms <= 0:
        return None
    return ms / 1000.0


def is_child_session(s: dict, panes: dict[str, int], spawned: bool | None = None) -> bool:
    """Did Claude Code spawn this, rather than you starting it?

    They register as ordinary `kind: interactive` sessions and appear on the
    dashboard beside the real ones -- `my-project-8f` sitting between two
    agents, with a cost and a context bar of its own. They should be
    labelled, and the labelling needs a signal rather than a guess at the
    name shape: a real session could perfectly well be called `api-1`.

    THE PROCESS TREE is that signal: a child runs under another session's
    process, which is what "Claude Code spawned it" means. `spawned` carries
    that answer (spawned_by_session(), or bool(spawning_session())), and when
    it has one it decides. A job (job_of()) is a child whatever this says:
    the snapshot settles that before asking.

    `nameSource` WAS the signal, and it answers a different question: who
    chose the NAME. An SDK host drives Claude Code (`entrypoint: sdk-cli`)
    and passes no name, so its session is `agent-a6`, `nameSource: derived`
    -- and that host's agent, nobody's subagent, was labelled SUBAGENT. Its
    parent process is the host, not a session. The same would have caught a
    plain `claude` started with no --name.

    When /proc cannot answer, the old rules stand: nameSource present and not
    "user", or, with no nameSource at all (an older Claude Code), what was
    measured twice on 2026-09-16 -- no bridge of its own AND a tmux pane
    shared with another session. Both at once has only ever meant a child.
    """
    if spawned is not None:
        return spawned
    src = s.get("name_source") or ""
    if src:
        return src != "user"
    if s.get("bridge_session_id"):
        return False
    pane = s.get("tmux") or ""
    return bool(pane) and panes.get(pane, 0) > 1


# Cut where the useful part of "You are fixing findings in `/mnt/...`" ends.
_LABEL_CUT = re.compile(r"\s+in\s+`|`|\(|\. |, |:\s")
_LABEL_MAX = 24
# Words not worth ending a truncated label on.
_TRAILING = {"a", "an", "the", "and", "or", "of", "for", "from", "to", "in",
             "on", "with", "that", "this", "its", "one", "at", "by", "as"}


def subagent_label(first_line: str) -> str:
    """A chip-sized label from a subagent's opening line.

    Prompts are written at the agent ("You are reviewing Task 1: ...") so the
    lead-in is noise and the first clause is the job. Truncation is on word
    boundaries: a chip reading "making one approved b" is worse than a shorter
    one that ends on a real word. Anything unusable becomes the plain word,
    because an unlabelled chip still says a subagent is there.
    """
    text = " ".join((first_line or "").split())
    text = re.sub(r"^(?:you are|you're|your task is to)\s+", "", text, flags=re.I)
    cut = _LABEL_CUT.search(text)
    if cut:
        text = text[:cut.start()]
    text = text.strip(" .:-\u2014")
    if not text:
        return "subagent"
    if len(text) <= _LABEL_MAX:
        return text

    words, kept = text.split(), []
    for w in words:
        if len(" ".join(kept + [w])) > _LABEL_MAX:
            break
        kept.append(w)
    while kept and kept[-1].lower().strip(",.;") in _TRAILING:
        kept.pop()
    return (" ".join(kept) + "\u2026") if kept else "subagent"


class SessionReader:
    def __init__(self, claude_home: Path, ppid_of=proc_ppid, hide=None,
                 environ_of=proc_environ) -> None:
        self.home = Path(claude_home)
        # () -> folders whose sessions are someone else's (app/people.py
        # others_private). Asked per snapshot, so a new person takes effect at once.
        self._hide = hide
        # Injectable so a test can draw a process tree without spawning one.
        self._ppid_of = ppid_of
        # (pid, keys) -> only those keys of its environment. Injectable for
        # the same reason. A process's environment is fixed when it starts, so
        # each answer is kept per (pid, start time) and read once.
        self._environ_of = environ_of
        self._job_env: dict[tuple[int, str], dict] = {}
        self._transcripts = TranscriptReader()
        # A subagent's label and start come from its opening record, which
        # never changes, so the head of each file is read once and kept.
        self._sub_heads: dict[str, tuple[str, float | None]] = {}
        self._snap_lock = threading.Lock()

    def _project_dir(self, cwd: str) -> Path:
        return self.home / "projects" / cwd.replace("/", "-")

    def _transcript_path(self, cwd: str, session_id: str) -> Path:
        """~/.claude/projects/<cwd with / as ->/<sessionId>.jsonl"""
        return self._project_dir(cwd) / f"{session_id}.jsonl"

    def _subagent_paths(self, cwd: str, session_id: str) -> list[Path]:
        """A session's subagents write their own transcripts, one directory
        deeper. Their tokens are billed to this session and were being missed
        entirely -- the Agent tool_result records an agentId and an outputFile
        but no usage at all, so the parent transcript cannot tell you."""
        try:
            return sorted((self._project_dir(cwd) / session_id / "subagents").glob("*.jsonl"))
        except OSError:
            return []

    def _subagent_head(self, path: Path) -> tuple[str, float | None]:
        """(label, started_at) from the first record only, never the whole
        file. Cached."""
        key = str(path)
        if key in self._sub_heads:
            return self._sub_heads[key]
        label, started = "subagent", None
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                rec = json.loads(fh.readline() or "{}")
            if not isinstance(rec, dict):
                rec = {}
            started = _iso_epoch(rec.get("timestamp"))
            content = (rec.get("message") or {}).get("content")
            if isinstance(content, list):
                content = " ".join(b.get("text", "") for b in content
                                   if isinstance(b, dict))
            if isinstance(content, str) and content.strip():
                label = subagent_label(content.strip().splitlines()[0])
        except (OSError, ValueError, AttributeError):
            pass                      # a label is not worth failing a request
        self._sub_heads[key] = (label, started)
        return label, started

    def _speakers(self) -> tuple[dict[str, str], dict[str, str]]:
        """(by session key, by tmux pane). Both empty without AgentVibes.

        agents.tsv is 8 tab-separated fields:
            p<pid>.<procStart>  name  project  branch  first  last  count  pane

        Field 0 is the same pid+start-time identity liveness verifies, so it is
        the primary join; the pane is a fallback for rows written by an older
        version. Nothing here is required -- a machine with no agents.tsv gets
        an empty overlay, which is the point.
        """
        by_key: dict[str, str] = {}
        by_pane: dict[str, str] = {}
        try:
            text = (self.home / "tts" / "agents.tsv").read_text()
        except OSError:
            return by_key, by_pane
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) < 2 or not parts[1]:
                continue
            name = parts[1]
            if parts[0]:
                by_key[parts[0]] = name
            if len(parts) >= 8 and parts[7] not in ("", "-"):
                by_pane[parts[7]] = name
        return by_key, by_pane

    def _job(self, s: dict) -> tuple[str, str]:
        """(job, about) for a session, from its environment, read once.

        An empty answer is kept too. The one window where a live process's
        environ reads empty is during its exec, and a session is in the
        registry only after Claude Code itself is up and wrote it there."""
        key = (s["pid"], s.get("proc_start") or "")
        env = self._job_env.get(key)
        if env is None:
            try:
                env = dict(self._environ_of(s["pid"], _JOB_KEYS) or {})
            except Exception:
                env = {}              # a marker is not worth failing a request
            # Only the job keys are kept, whatever an injected reader returned.
            env = {k: v for k, v in env.items() if k in _JOB_KEYS}
            self._job_env[key] = env
        return job_of(env)

    def _waiting_since(self, pid: int) -> float:
        """mtime of this session's waiting marker, 0 if none."""
        try:
            return (self.home / WAITING_DIR / str(pid)).stat().st_mtime
        except OSError:
            return 0.0

    def snapshot(self) -> list[dict]:
        """One read at a time. The endpoints run on a thread pool (sessions,
        messages, the note relay, and anything asking whether you are at the
        keyboard), and two concurrent reads of one transcript would both apply
        the same new chunk: its tokens counted twice, and the offset pushed
        past the end of the file."""
        with self._snap_lock:
            return self._snapshot()

    def private_presence(self) -> list[dict]:
        """Sessions in someone else's private workspace, as presence only:
        whose, and whether it is working. Read from the registry's small JSON
        alone; never a title, a prompt or a transcript."""
        labels = self._hide() if self._hide else {}
        if not isinstance(labels, dict):
            labels = {str(h): "" for h in labels}
        out = []
        for s in live_sessions(self.home / "sessions"):
            cwd = str(s["cwd"])
            for h, label in labels.items():
                h = str(h).rstrip("/")
                if cwd == h or cwd.startswith(h + "/"):
                    out.append({"private": True, "label": label or "Someone's agent",
                                "status": s["status"], "pid": s["pid"]})
                    break
        return out

    def _snapshot(self) -> list[dict]:
        by_key, by_pane = self._speakers()
        sessions = live_sessions(self.home / "sessions")
        # Dropped before anything reads them: no title, no prompt, no transcript.
        hidden = [str(h).rstrip("/") for h in (self._hide() if self._hide else [])]
        sessions = [s for s in sessions
                    if not any(str(s["cwd"]) == h or str(s["cwd"]).startswith(h + "/") for h in hidden)]

        # Resolve the exact matches first, then let the pane fallback fill in
        # the rest -- but never hand out a name another session already owns.
        # Two live sessions can share one tmux pane, and the second must not
        # inherit the first's speaker name.
        speakers: dict[int, str] = {}
        for s in sessions:
            name = by_key.get(f"p{s['pid']}.{s['proc_start']}")
            if name:
                speakers[s["pid"]] = name
        claimed = set(speakers.values())
        for s in sessions:
            if s["pid"] in speakers:
                continue
            name = by_pane.get(s["tmux"], "")
            if name and name not in claimed:
                speakers[s["pid"]] = name
                claimed.add(name)

        # How many live sessions sit in each tmux pane. Only used as the
        # fallback in is_child_session(), when /proc cannot draw the tree.
        panes: dict[str, int] = {}
        session_pids = {s["pid"] for s in sessions}
        for s in sessions:
            if s.get("tmux"):
                panes[s["tmux"]] = panes.get(s["tmux"], 0) + 1

        now = time.time()
        rows: list[dict] = []
        for s in sessions:
            t = self._transcripts.read(
                self._transcript_path(s["cwd"], s["session_id"]))
            # Subagent turns cost real money but belong to their own context
            # window, so they add to the bill and never move the context bar.
            sub_tokens = _zero_tokens()
            sub_cost = 0.0
            # "Recently active", not "running" -- and the difference is not
            # hair-splitting. There is no end marker to read: a finished
            # subagent's last assistant record says stop_reason end_turn, and
            # so does one that is mid-run between tool calls, so the file
            # cannot tell you it is done. Only the parent's tool_result can,
            # and finding it means re-reading a 3.5 MB transcript every poll,
            # which is the one thing this project forbids. So: wrote lately.
            active: list[dict] = []
            for path in self._subagent_paths(s["cwd"], s["session_id"]):
                st = self._transcripts.read(path)
                for k, v in st["tokens"].items():
                    sub_tokens[k] = sub_tokens.get(k, 0) + v
                one = cost_usd(st["model"] or t["model"], st["tokens"])
                sub_cost += one
                age = now - st["mtime"] if st["mtime"] else None
                if age is not None and age <= SUBAGENT_RECENT_SECS:
                    label, started = self._subagent_head(path)
                    active.append({"label": label, "ago": round(age, 1),
                                   "status": "busy" if age <= SUBAGENT_BUSY_SECS else "idle",
                                   # Its own share of subagent_cost_usd.
                                   "cost_usd": round(one, 4),
                                   "started_at": started})
            active.sort(key=lambda a: a["ago"])
            limit = context_limit(t["model"])
            used = t["context_used"]
            last_reply_at = t.get("last_reply_at")
            last_call_at = t.get("last_call_at")
            cache = None
            cache_at = last_call_at if last_call_at is not None else last_reply_at
            if cache_at is not None and used is not None:
                ttl_s = t.get("cache_ttl_s", 3600)
                expires_in_s = round(ttl_s - (now - cache_at), 1)
                rewarm = rewarm_usd(t["model"], used, ttl_s)
                warm = warm_usd(t["model"], used)
                cache = {
                    "ttl_s": ttl_s,
                    "expires_in_s": expires_in_s,
                    "cold": expires_in_s <= 0,
                    "rewarm_usd": round(rewarm, 2) if rewarm is not None else None,
                    "warm_usd": round(warm, 2) if warm is not None else None,
                }
            # Blocked only while the permission marker is newer than the last
            # transcript write; answering the prompt clears it implicitly.
            waiting = self._waiting_since(s["pid"]) > t["mtime"] > 0
            # Who started it. The process tree first: the nearest live
            # session above it. Only when there is none is the environment
            # asked whether it is a job, since a detached job has no session
            # above it and a tree that already names a parent needs no more.
            # A shared tmux pane is never used to name a parent: a job runs in
            # the pane of whoever set it off, not whoever it is about, and two
            # real sessions can share a pane.
            parent = spawning_session(s["pid"], session_pids, self._ppid_of)
            job, about = self._job(s) if not parent else ("", "")
            child = bool(job) or is_child_session(
                s, panes, None if parent is None else bool(parent))
            rows.append({
                # Identity: the page keys cards on pid so expanding one
                # survives the next poll.
                "pid": s["pid"],
                "session_id": s["session_id"],
                "app_url": (f"https://claude.ai/code/{s['bridge_session_id']}"
                            if s.get("bridge_session_id") else ""),
                "name": s["name"],
                "speaker": speakers.get(s["pid"], ""),
                # Spawned by Claude Code, not started by you. It still gets a
                # card -- it is burning real tokens and that should show --
                # but it says so rather than passing as one of your agents.
                "child": child,
                # The nearest live session above it in the process tree, else
                # None; root_pid, filled in below, is the top of that chain.
                "parent_pid": parent or None,
                "root_pid": None,
                # A job's name and whose work it is about, from its
                # environment; "" for anything that is not a job.
                "job": job,
                "job_about": about,
                # A chip-sized "what it is doing" for a child; "" otherwise.
                "label": _child_label(t) if child else "",
                "project": s["project"],
                # When it started, epoch seconds; None when the registry does
                # not say.
                "started_at": _started(s.get("started_at")),
                # The repo this session is working in. reader stays pure -- the
                # git state itself is layered on in server.py, which owns the
                # caches -- but only the registry knows where the session is.
                "cwd": s["cwd"],
                "title": t["title"],
                "status": s["status"],
                "waiting": waiting,
                "model": t["model"],
                "context_used": used,
                "context_limit": limit,
                "context_pct": round(100.0 * used / limit, 1) if limit else 0.0,
                "cost_usd": round(cost_usd(t["model"], t["tokens"]) + sub_cost, 4),
                "tokens": t["tokens"],
                "subagent_cost_usd": round(sub_cost, 4),
                "subagent_tokens": sum(sub_tokens.values()),
                "subagents": active,
                "permission_mode": t["permission_mode"],
                "last_prompt": t["last_prompt"],
                "last_prompt_at": t.get("last_prompt_at"),
                "last_reply_at": last_reply_at,
                "last_reply": t.get("last_reply") or "",
                "cache": cache,
                # "cli" when you started it; an SDK host says "sdk-cli".
                "entrypoint": s.get("entrypoint") or "",
                "last_activity": round(now - t["mtime"], 1) if t["mtime"] else None,
            })
        by_pid = {r["pid"]: r for r in rows}
        for r in rows:
            r["root_pid"] = root_of(r["pid"], by_pid)
        # Forget the environments of sessions that are gone.
        live = {(s["pid"], s.get("proc_start") or "") for s in sessions}
        for key in [k for k in self._job_env if k not in live]:
            del self._job_env[key]
        order(rows)
        return rows


def _child_label(t: dict) -> str:
    """A child's label: its title, else its prompt, as subagent_label() cuts
    an opening line."""
    text = (t.get("title") or t.get("last_prompt") or "").strip()
    return subagent_label(text.splitlines()[0] if text else "")


def order(rows: list[dict]) -> list[dict]:
    """The most recent message first, not alphabetical by project folder,
    which left nobody able to say why a session was on top. Its last reply, else its prompt, else the transcript's time; a
    session with none of those goes last and the project breaks ties. The page
    groups needs-you and new-for-you above the rest and keeps this order
    inside each group."""
    def last(r):
        at = max(r.get("last_reply_at") or 0, r.get("last_prompt_at") or 0)
        if at:
            return -at
        return -(time.time() - r["last_activity"]) if r.get("last_activity") is not None else float("inf")
    rows.sort(key=lambda r: (last(r), r.get("project") or ""))
    return rows
