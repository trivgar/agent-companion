"""Token usage over time, for the 5-hour and 7-day readouts.

WHAT THIS IS, AND IS NOT. Claude Code does not record your rate-limit state
anywhere on disk -- not in the transcripts, and `stats-cache.json` is computed
on its own schedule and was months stale here. So this measures what you have
SPENT in a window, not how much of a limit remains. It answers "how heavy has
the last five hours been", which is the question the number was being used to
answer anyway.

Scanning every transcript on every poll would be the mistake this project
forbids for a single transcript, multiplied. So the same watermark applies:
each file is read from its last offset, records are bucketed by hour, and the
buckets are persisted. A restart re-reads nothing it has already seen; a poll
costs a stat() per file plus whatever was appended.

Buckets older than the widest window are dropped, so the cache stays small
regardless of how long this runs.

Buckets are held PER FILE rather than in one shared pile. That is not tidiness:
a truncated or rotated transcript has to be re-read from zero, and with shared
buckets its earlier contribution would still be sitting there, so the re-read
would add to it instead of replacing it. Owning the buckets lets a reset drop
exactly that file's history.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from agent_companion import config
from agent_companion.pricing import cost_usd

HOUR = 3600
WINDOW_5H = 5 * HOUR
WINDOW_7D = 7 * 24 * HOUR
KEEP = WINDOW_7D + HOUR          # one bucket of slack

# Old transcript trees no longer written to (a previous install, another OS).
# Scanned once by the lifetime seed, never by a poll. os.pathsep-separated.
ARCHIVE_ROOTS = config.paths("archive_roots")
CACHE_NAME = "session-companion-usage.json"

# HOW A REPLY IS COUNTED. Claude Code writes one transcript line per content
# block of a reply -- thinking, text, each tool call -- and every one of those
# lines carries the reply's full `usage`. Counting lines counted one API call
# two or three times: measured 2026-09-23, $17,420 of the $31,411 in the live
# transcripts was the same message again in the same file, 622 of 918 replies
# in one session. Now a reply counts once, by its message id.
#
# Bumping this discards the cache and re-seeds, so a total built under the old
# rule never mixes with one built under the new.
COUNTING = 2
SEED_READERS = 8         # parallel reads for ensure_lifetime's one full pass
SEEN_KEEP = 64           # message ids remembered per file; the repeats are adjacent

TOKEN_KEYS = ("input", "output", "cache_read", "cache_write", "cache_write_1h")


def _zero() -> dict:
    return {k: 0 for k in TOKEN_KEYS}


def _parse_ts(value) -> float:
    """Transcript timestamps are ISO-8601 with a Z. Returns 0 when unusable."""
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def tokens_from(usage: dict) -> dict:
    out = _zero()
    out["input"] = usage.get("input_tokens") or 0
    out["output"] = usage.get("output_tokens") or 0
    out["cache_read"] = usage.get("cache_read_input_tokens") or 0
    out["cache_write"] = usage.get("cache_creation_input_tokens") or 0
    creation = usage.get("cache_creation")
    if isinstance(creation, dict):
        out["cache_write_1h"] = creation.get("ephemeral_1h_input_tokens") or 0
    return out


def merge(into: dict, more: dict) -> None:
    for k in TOKEN_KEYS:
        into[k] = into.get(k, 0) + more.get(k, 0)


def _read_one(job: tuple) -> bytes | None:
    path, _entry, offset, size = job
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            return fh.read(size - offset)
    except OSError:
        return None


def _read_tails(jobs: list, workers: int):
    """(entry, bytes or None) for each job, in order. With workers > 1 the
    reads run ahead on threads (the read releases the GIL), at most
    workers * 2 files held at once, so 11 GB never sits in memory."""
    if workers <= 1 or len(jobs) < 2:
        for job in jobs:
            yield job[1], _read_one(job)
        return
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as pool:
        ahead: deque = deque()
        it = iter(jobs)
        for job in it:
            ahead.append((job[1], pool.submit(_read_one, job)))
            if len(ahead) >= workers * 2:
                break
        for job in it:
            entry, fut = ahead.popleft()
            yield entry, fut.result()
            ahead.append((job[1], pool.submit(_read_one, job)))
        while ahead:
            entry, fut = ahead.popleft()
            yield entry, fut.result()


def _usage_lines(chunk: bytes):
    """The lines of `chunk` that mention "usage", decoded, found by searching
    the bytes instead of decoding and splitting the whole chunk. The seed pass
    reads every transcript (11 GB, 19,188 files on 2026-11-07) and only about
    one line in six carries usage, so decoding and splitting the rest was half
    of its 52 s. Lines split as str.splitlines() splits them, so the count is
    the same as before."""
    find = chunk.find
    i = find(b'"usage"')
    while i != -1:
        start = chunk.rfind(b"\n", 0, i) + 1
        end = find(b"\n", i)
        if end == -1:
            end = len(chunk)
        # splitlines() also breaks on \r, \x85, \u2028 and the rest: keep
        # only the piece holding the match, as it did.
        for piece in chunk[start:end].decode("utf-8", "replace").splitlines():
            if '"usage"' in piece:
                yield piece
        i = find(b'"usage"', end)


class UsageIndex:
    """Hourly token buckets across every transcript. Create once, reuse."""

    def __init__(self, home: Path, cache_path: Path | None = None,
                 archive_roots: tuple | None = None) -> None:
        self.home = Path(home)
        # Absolute paths, so only for the real ~/.claude: never a fixture home.
        if archive_roots is None:
            real = Path.home() / ".claude"
            try:
                archive_roots = ARCHIVE_ROOTS if self.home.resolve() == real.resolve() else ()
            except OSError:
                archive_roots = ()
        self.archive_roots = tuple(archive_roots)
        self.cache_path = Path(cache_path) if cache_path else self.home / CACHE_NAME
        # path -> {"offset", "inode", "buckets": {hour epoch: {model: tokens}}}
        self._files: dict[str, dict] = {}
        # Everything ever, by model. NEVER pruned -- that is the whole point of
        # it, and it is why it cannot be derived from the buckets, which keep
        # seven days. Seeded once by a full pass and accumulated incrementally
        # from then on, so the watermark discipline still holds on every poll.
        self._lifetime: dict[str, dict] = {}
        self._lifetime_seeded = False
        self._lifetime_since = 0.0
        self._load()

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        if data.get("counting") != COUNTING:
            return          # built under the old counting: start again, re-seed
        for path, entry in (data.get("files") or {}).items():
            if not isinstance(entry, dict):
                continue
            try:
                buckets = {int(h): {m: dict(t) for m, t in models.items()}
                           for h, models in (entry.get("buckets") or {}).items()}
            except (ValueError, TypeError, AttributeError):
                continue
            self._files[path] = {"offset": int(entry.get("offset") or 0),
                                 "inode": int(entry.get("inode") or 0),
                                 "buckets": buckets,
                                 "seen": [str(x) for x in (entry.get("seen") or [])][-SEEN_KEEP:],
                                 "life": {m: dict(t) for m, t in (entry.get("life") or {}).items()
                                          if isinstance(t, dict)}}
        life = data.get("lifetime")
        if isinstance(life, dict):
            self._lifetime = {m: dict(t) for m, t in life.items()
                              if isinstance(t, dict)}
        self._lifetime_seeded = bool(data.get("lifetime_seeded"))
        try:
            self._lifetime_since = float(data.get("lifetime_since") or 0)
        except (TypeError, ValueError):
            self._lifetime_since = 0.0

    def _save(self) -> None:
        payload = {"counting": COUNTING, "files": {
            p: {"offset": e["offset"], "inode": e["inode"],
                "buckets": {str(h): m for h, m in e["buckets"].items()},
                "seen": e.get("seen", []), "life": e.get("life", {})}
            for p, e in self._files.items()},
            "lifetime": self._lifetime,
            "lifetime_seeded": self._lifetime_seeded,
            "lifetime_since": self._lifetime_since}
        tmp = self.cache_path.with_suffix(".json.sc-tmp")
        try:
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, self.cache_path)
        except OSError:
            pass          # a cache we cannot write is a slow poll, not a failure

    # -- scanning ---------------------------------------------------------
    def _prune(self, now: float) -> None:
        cutoff = now - KEEP
        for entry in self._files.values():
            for hour in [h for h in entry["buckets"] if h < cutoff]:
                del entry["buckets"][hour]

    def _consume(self, entry: dict, chunk: bytes) -> None:
        for raw in _usage_lines(chunk):
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            ts = _parse_ts(rec.get("timestamp"))
            if not ts:
                continue
            # ONE REPLY, ONE COUNT (see COUNTING). A line whose message id was
            # just counted is the same API call's next content block.
            mid = msg.get("id") or rec.get("requestId")
            if mid:
                seen = entry.setdefault("seen", [])
                if mid in seen:
                    continue
                seen.append(mid)
                del seen[:-SEEN_KEEP]
            bucket = entry["buckets"].setdefault(int(ts // HOUR) * HOUR, {})
            model = str(msg.get("model") or "") or "unknown"
            counted = tokens_from(usage)
            merge(bucket.setdefault(model, _zero()), counted)
            # The same records, into a total nothing prunes -- and into this
            # file's own share of it, so a re-read can take that share back out.
            merge(self._lifetime.setdefault(model, _zero()), counted)
            merge(entry.setdefault("life", {}).setdefault(model, _zero()), counted)
            if not self._lifetime_since or ts < self._lifetime_since:
                self._lifetime_since = ts

    def _unlife(self, life: dict) -> None:
        for model, counted in life.items():
            total = self._lifetime.get(model)
            if total is None:
                continue
            for k in TOKEN_KEYS:
                total[k] = max(0, total.get(k, 0) - counted.get(k, 0))

    def ensure_lifetime(self, now: float | None = None) -> None:
        """Seed the all-time total once, by reading every transcript in full.

        THIS IS THE ONE PLACE THAT RE-READS WHOLE TRANSCRIPTS, and it is not a
        violation of the rule -- the rule is about the polling path, which stays
        on its byte offsets. This runs once in the life of the cache, is marked
        done in the cache file, and never runs again. Measured on one real
        machine: 1,560 files, 869 MB, 269,407 lines, **2.4 seconds** -- the
        `"usage" not in line` fast path means almost nothing is parsed.

        It cannot be built from refresh() instead. That skips any file untouched
        for longer than the window, which is nearly all of the history, and it
        resumes from stored offsets that have already passed the records a total
        needs. Hence a full pass, with the offsets reset so the incremental
        reader continues cleanly from where this finished.
        """
        if self._lifetime_seeded:
            return
        self._lifetime = {}
        self._lifetime_since = 0.0
        for entry in self._files.values():
            entry.update(offset=0, buckets={}, seen=[], life={})
        self.refresh(now=now, full=True)
        self._scan_archives()
        self._lifetime_seeded = True
        self._save()

    def _scan_archives(self) -> None:
        """Fold in transcripts from installs that are finished writing.

        Lifetime only. They contribute nothing to the seven-day windows -- they
        are months old -- and they are never read again, so they get no offset
        tracking and no place in self._files.
        """
        live = self.home / "projects"
        seen: set = set()
        for root in self.archive_roots:
            try:
                if not root.is_dir() or root.resolve() == live.resolve():
                    continue
                real = root.resolve()
            except OSError:
                continue
            if real in seen:
                continue
            seen.add(real)
            for path in root.rglob("*.jsonl"):
                try:
                    chunk = path.read_bytes()
                except OSError:
                    continue
                # A throwaway entry: _consume writes buckets into it, and those
                # are for the window, which this history is far outside of.
                self._consume({"buckets": {}}, chunk)

    def lifetime(self) -> dict:
        """Every token ever recorded, and what it would have cost at list price.

        `unpriced_tokens` is the honest asterisk. cost_usd() returns 0.00 for a
        model RATES has never heard of, which reads exactly like a model that
        cost nothing -- and billions of real tokens sat that way until
        the Opus 4.5 and Sonnet 4.5 rows went in. Reporting the count means the
        next missing model shows up as a number rather than as silence.
        """
        from agent_companion.pricing import cost_usd, is_priced
        total = _zero()
        unpriced = 0
        missing: list[str] = []
        for model, counted in self._lifetime.items():
            merge(total, counted)
            if not is_priced(model) and sum(counted.values()):
                unpriced += sum(counted.values())
                missing.append(model)
        return {"tokens": total,
                "total_tokens": sum(total.values()),
                "cost_usd": round(sum(cost_usd(m, t)
                                      for m, t in self._lifetime.items()), 2),
                "unpriced_tokens": unpriced,
                "unpriced_models": sorted(missing)[:6],
                "since": self._lifetime_since,
                "seeded": self._lifetime_seeded}

    def refresh(self, now: float | None = None, full: bool = False) -> None:
        """Read whatever is new. Cheap enough to call on every poll.

        `full` is for ensure_lifetime() only: it stops the age skip below from
        excluding the history, and assumes the caller has reset the offsets.
        """
        now = now or time.time()
        projects = self.home / "projects"
        if not projects.is_dir():
            return

        touched = False
        todo: list[tuple] = []
        # rglob, NOT a list of hand-written globs. This has now been wrong
        # twice for the same reason. First "*/*.jsonl" missed ordinary
        # subagents at <project>/<session>/subagents/agent-<id>.jsonl -- 280
        # files, 1.05 billion tokens, $481 excluded in silence. Adding that one
        # glob fixed those and missed WORKFLOW subagents, which nest deeper
        # still at .../subagents/workflows/<wf-id>/agent-<id>.jsonl: another
        # 153 files, 32 MB, ~400 million tokens and ~$618, measured
        # 2026-09-16.
        #
        # A third glob would be wrong again the next time Claude Code nests
        # something. Walking the tree costs a directory scan of ~1,500 files and
        # cannot be defeated by a new layout.
        for path in projects.rglob("*.jsonl"):
            key = str(path)
            try:
                st = path.stat()
            except OSError:
                continue
            # Files untouched for longer than the window cannot contribute --
            # to the WINDOW. They are most of the lifetime total, so the seed
            # pass must not skip them.
            if not full and st.st_mtime < now - KEEP and key not in self._files:
                continue

            entry = self._files.setdefault(
                key, {"offset": 0, "inode": st.st_ino, "buckets": {}, "seen": [], "life": {}})
            if st.st_ino != entry["inode"] or st.st_size < entry["offset"]:
                # Rotated or truncated: drop what this file contributed, or the
                # re-read would add to its own history instead of replacing it.
                # The LIFETIME share too: this cleared the buckets and left the
                # total, so every re-read added the whole file to it again.
                self._unlife(entry.get("life") or {})
                entry.update(offset=0, inode=st.st_ino, buckets={}, seen=[], life={})
            if st.st_size <= entry["offset"]:
                continue
            todo.append((path, entry, entry["offset"], st.st_size))

        # Read in parallel only for the seed pass: the poll reads a few small
        # tails, and the seed reads every transcript (11 GB on 2026-11-07, half
        # its time spent waiting on the disk). Consumed in walk order, so the
        # result is the same as one at a time.
        for entry, chunk in _read_tails(todo, SEED_READERS if full else 1):
            if chunk is None:
                continue
            cut = chunk.rfind(b"\n")
            if cut == -1:
                continue
            self._consume(entry, chunk[: cut + 1])
            entry["offset"] += cut + 1
            touched = True

        self._prune(now)
        if touched:
            self._save()

    # -- reading ----------------------------------------------------------
    def window(self, seconds: int, now: float | None = None,
               start: float | None = None) -> dict:
        """Totals for the last `seconds`, or since `start` when given.

        `start` exists because Anthropic's 7-day limit is a FIXED window with a
        reset time, not a rolling week. Measuring a rolling week and printing it
        beside their percentage put two different periods next to each other as
        though they were one figure -- 2.6x apart when it was caught.
        """
        now = now or time.time()
        cutoff = now - seconds if start is None else start
        tokens, cost = _zero(), 0.0
        for entry in self._files.values():
            for hour, models in entry["buckets"].items():
                if hour + HOUR <= cutoff:
                    continue
                for model, t in models.items():
                    merge(tokens, t)
                    cost += cost_usd(model, t)
        return {"tokens": tokens, "cost_usd": round(cost, 4),
                "total_tokens": sum(tokens.values()),
                # What period this actually covers, so nothing downstream has to
                # assume it matches the label next to it.
                "since": cutoff}

    def snapshot(self, now: float | None = None,
                 seven_day_start: float | None = None,
                 five_hour_start: float | None = None) -> dict:
        """Both windows. Pass the real reset times where they are known, so the
        spend figures cover the same period as the utilisation percentages
        shown beside them."""
        now = now or time.time()
        return {"five_hour": self.window(WINDOW_5H, now, five_hour_start),
                "seven_day": self.window(WINDOW_7D, now, seven_day_start),
                "note": "usage in the window, not a rate-limit percentage"}
