"""Incremental reader for Claude Code transcripts.

Transcripts reach 75 MB and grow continuously. Re-reading every file on every
poll is tens of MB/s of pointless I/O, so each file gets a watermark: byte
offset plus running totals and last-seen typed records. Each poll reads only
the bytes appended since last time.

A torn final line (the writer was mid-append) is NOT consumed: the offset stops
at the last newline, so the line is read whole on a later poll.

Record shapes, verified against real transcripts on 2026-09-13:

    {"type":"ai-title","aiTitle":"..."}         generated title
    {"type":"custom-title","customTitle":"..."} title the user set by hand
    {"type":"permission-mode","permissionMode":"bypassPermissions"}
    {"type":"user","message":{"role":"user","content":"..."}}
    {"type":"assistant","message":{"model":...,"usage":{...}}}

Two traps found by reading the files rather than guessing:

* `last-prompt` does NOT contain the prompt. It is
  `{"type":"last-prompt","leafUuid":...}` -- a pointer to the conversation's
  current leaf, which is usually a tool result. The last human prompt is the
  last `user` record whose `message.content` is a plain STRING and which is not
  flagged `isMeta` (that flag marks injected text: hook output, cross-session
  messages, skill preambles) or wrapped in an injected tag such as
  <task-notification>.
* `cost-state` records exist but carry `totalCostUSD: 0` and an empty
  `modelUsage`, so cost still has to be estimated from `usage`.
"""
from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path

MAX_PROMPT_CHARS = 500

# Wrappers the harness injects as plain-string user messages. Observed in real
# transcripts: <task-notification> appeared 34 times against 88 typed prompts.
# A denylist, not a general "starts with a tag" rule, so a pasted <div> still
# counts as something the user actually said.
INJECTED_PREFIXES = (
    "<task-notification>",
    "<system-reminder>",
    "<cross-session-message",
    "<local-command-stdout>",
    "<command-name>",
    "<user-prompt-submit-hook>",
)


def _is_typed_by_a_human(content: str) -> bool:
    return not content.lstrip().startswith(INJECTED_PREFIXES)


def _epoch(stamp) -> float:
    """A record's ISO `timestamp` as epoch seconds; 0.0 when it has none."""
    try:
        return datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _zero_tokens() -> dict:
    return {"input": 0, "output": 0, "cache_read": 0,
            "cache_write": 0, "cache_write_1h": 0}


@dataclass
class _State:
    offset: int = 0
    inode: int = 0
    tokens: dict = field(default_factory=_zero_tokens)
    model: str = ""
    context_used: int = 0
    ai_title: str = ""
    custom_title: str = ""
    last_prompt: str = ""
    # When you sent it: a dashboard can count a prompt in the last ten
    # minutes as you being at the keyboard. 0.0 until a prompt with a stamp is seen.
    last_prompt_at: float = 0.0
    # When it last wrote to you (a text block, not a tool call): the sessions
    # tab orders by it and marks a reply you have not seen.
    last_reply_at: float = 0.0
    # Every main-thread assistant API call refreshes the prompt cache, even
    # when it only contains tool_use blocks and did not write text to us.
    last_call_at: float = 0.0
    last_reply: str = ""
    # The last cache creation tells us how long the current prompt cache lasts.
    # Claude Code normally uses one hour; keep that conservative default when
    # a transcript does not expose a cache_creation breakdown.
    cache_ttl_s: int = 3600
    permission_mode: str = ""


def _empty() -> dict:
    return {
        "tokens": _zero_tokens(), "model": "", "context_used": 0, "title": "",
        "last_prompt": "", "last_prompt_at": None, "last_reply_at": None, "last_call_at": None, "last_reply": "", "cache_ttl_s": 3600, "permission_mode": "", "mtime": 0.0,
    }


class TranscriptReader:
    """Holds watermarks across polls. Create once, reuse."""

    def __init__(self) -> None:
        self._state: dict[str, _State] = {}

    def read(self, path: Path) -> dict:
        path = Path(path)
        key = str(path)
        try:
            st = path.stat()
        except OSError:
            return _empty()

        s = self._state.get(key)
        if s is None or st.st_ino != s.inode or st.st_size < s.offset:
            s = _State(inode=st.st_ino)
            self._state[key] = s

        if st.st_size > s.offset:
            try:
                with path.open("rb") as fh:
                    fh.seek(s.offset)
                    chunk = fh.read(st.st_size - s.offset)
            except OSError:
                chunk = b""
            cut = chunk.rfind(b"\n")
            if cut != -1:
                whole = chunk[: cut + 1]
                s.offset += len(whole)
                self._consume(s, whole)

        return {
            "tokens": dict(s.tokens),
            "model": s.model,
            "context_used": s.context_used,
            "title": s.custom_title or s.ai_title,
            "last_prompt": s.last_prompt,
            "last_prompt_at": s.last_prompt_at or None,
            "last_reply_at": s.last_reply_at or None,
            "last_call_at": s.last_call_at or None,
            "last_reply": s.last_reply,
            "cache_ttl_s": s.cache_ttl_s,
            "permission_mode": s.permission_mode,
            "mtime": st.st_mtime,
        }

    @staticmethod
    def _consume(s: _State, chunk: bytes) -> None:
        for raw in chunk.decode("utf-8", "replace").splitlines():
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue

            kind = rec.get("type")
            if kind == "ai-title" and rec.get("aiTitle"):
                s.ai_title = str(rec["aiTitle"])
                continue
            if kind == "custom-title" and rec.get("customTitle"):
                s.custom_title = str(rec["customTitle"])
                continue
            if kind == "permission-mode" and rec.get("permissionMode"):
                s.permission_mode = str(rec["permissionMode"])
                continue
            if (kind == "system" and rec.get("subtype") == "compact_boundary"
                    and not rec.get("isSidechain")):
                # A compaction shrinks the window; until the next reply, the
                # last usage line is pre-compaction (2026-11-08: ~25k read as 489k, told to clear).
                metadata = rec.get("compactMetadata")
                post_tokens = metadata.get("postTokens") if isinstance(metadata, dict) else None
                s.context_used = post_tokens if isinstance(post_tokens, int) and post_tokens >= 0 else 0
                continue

            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue

            if kind == "user":
                # A plain string is something the human typed. A list is tool
                # results or injected blocks; isMeta marks injected text.
                content = msg.get("content")
                if (isinstance(content, str) and content.strip()
                        and not rec.get("isMeta")
                        and _is_typed_by_a_human(content)):
                    s.last_prompt = content.strip()[:MAX_PROMPT_CHARS]
                    s.last_prompt_at = _epoch(rec.get("timestamp")) or s.last_prompt_at
                continue

            if kind == "assistant" and not rec.get("isSidechain") and isinstance(msg.get("content"), list):
                said = " ".join(str(c.get("text") or "") for c in msg["content"]
                                if isinstance(c, dict) and c.get("type") == "text").strip()
                if said:
                    s.last_reply_at = _epoch(rec.get("timestamp")) or s.last_reply_at
                    s.last_reply = " ".join(said.split())[:MAX_PROMPT_CHARS]
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            if kind == "assistant" and not rec.get("isSidechain"):
                s.last_call_at = _epoch(rec.get("timestamp")) or s.last_call_at
            if msg.get("model"):
                s.model = str(msg["model"])
            i = usage.get("input_tokens") or 0
            o = usage.get("output_tokens") or 0
            cr = usage.get("cache_read_input_tokens") or 0
            cw = usage.get("cache_creation_input_tokens") or 0
            s.tokens["input"] += i
            s.tokens["output"] += o
            s.tokens["cache_read"] += cr
            s.tokens["cache_write"] += cw

            # The TTL split decides the write rate: 1.25x for 5m, 2x for 1h.
            creation = usage.get("cache_creation")
            if isinstance(creation, dict):
                write_1h = creation.get("ephemeral_1h_input_tokens") or 0
                write_5m = creation.get("ephemeral_5m_input_tokens") or 0
                s.tokens["cache_write_1h"] += write_1h
                if not rec.get("isSidechain"):
                    if write_1h > 0:
                        s.cache_ttl_s = 3600
                    elif write_5m > 0:
                        s.cache_ttl_s = 300

            # A subagent's turn is billed to us, so its tokens count -- but the
            # context bar should track the main thread, not the subagent's small
            # window.
            #
            # VERIFIED 2026-09-14, and the earlier note here was wrong twice
            # over. It said no record carries isSidechain: every record in a
            # subagent transcript does, `isSidechain: true`. It also looked for
            # a `Task` tool_use; the tool is named `Agent`. Both errors came
            # from searching only the parent transcripts, which never contain a
            # subagent's turns -- those live one directory deeper, in
            # <project>/<session-id>/subagents/agent-<id>.jsonl.
            #
            # The guard is right and now reached for real: a subagent has its
            # own window, so its turns are a cost and never a context reading.
            if not rec.get("isSidechain"):
                s.context_used = cr + i + cw
