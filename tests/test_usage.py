import json
import time

import pytest

from agent_companion.usage import HOUR, WINDOW_5H, WINDOW_7D, UsageIndex, tokens_from


def _rec(ts_iso, out=0, model="claude-opus-5", **usage_extra):
    usage = {"input_tokens": 0, "output_tokens": out,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    usage.update(usage_extra)
    return json.dumps({"type": "assistant", "timestamp": ts_iso,
                       "message": {"model": model, "usage": usage}}) + "\n"


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _home(tmp_path, lines):
    home = tmp_path / ".claude"
    d = home / "projects" / "-proj"
    d.mkdir(parents=True)
    (d / "s1.jsonl").write_text("".join(lines))
    return home


def test_tokens_include_the_1h_cache_split():
    t = tokens_from({"cache_creation_input_tokens": 100,
                     "cache_creation": {"ephemeral_1h_input_tokens": 60}})
    assert t["cache_write"] == 100 and t["cache_write_1h"] == 60


def test_counts_only_the_window(tmp_path):
    now = time.time()
    home = _home(tmp_path, [_rec(_iso(now - 60), out=10),
                            _rec(_iso(now - 30 * HOUR), out=999)])
    idx = UsageIndex(home, tmp_path / "cache.json")
    idx.refresh(now)
    assert idx.window(WINDOW_5H, now)["tokens"]["output"] == 10


def test_seven_day_includes_what_five_hour_excludes(tmp_path):
    now = time.time()
    home = _home(tmp_path, [_rec(_iso(now - 60), out=10),
                            _rec(_iso(now - 30 * HOUR), out=5)])
    idx = UsageIndex(home, tmp_path / "cache.json")
    idx.refresh(now)
    snap = idx.snapshot(now)
    assert snap["five_hour"]["tokens"]["output"] == 10
    assert snap["seven_day"]["tokens"]["output"] == 15


def test_cost_is_summed_per_model(tmp_path):
    now = time.time()
    home = _home(tmp_path, [_rec(_iso(now - 60), out=1_000_000, model="claude-opus-5"),
                            _rec(_iso(now - 60), out=1_000_000, model="claude-haiku-4-5")])
    idx = UsageIndex(home, tmp_path / "cache.json")
    idx.refresh(now)
    # 25.00 for opus output + 5.00 for haiku output
    assert round(idx.window(WINDOW_5H, now)["cost_usd"], 2) == 30.00


def test_second_refresh_reads_only_new_bytes(tmp_path):
    now = time.time()
    home = _home(tmp_path, [_rec(_iso(now - 60), out=10)])
    idx = UsageIndex(home, tmp_path / "cache.json")
    idx.refresh(now)
    f = home / "projects" / "-proj" / "s1.jsonl"
    with f.open("a") as fh:
        fh.write(_rec(_iso(now - 30), out=7))
    idx.refresh(now)
    assert idx.window(WINDOW_5H, now)["tokens"]["output"] == 17


def test_totals_are_not_double_counted_on_repeat_refresh(tmp_path):
    """The watermark's whole job."""
    now = time.time()
    home = _home(tmp_path, [_rec(_iso(now - 60), out=10)])
    idx = UsageIndex(home, tmp_path / "cache.json")
    for _ in range(4):
        idx.refresh(now)
    assert idx.window(WINDOW_5H, now)["tokens"]["output"] == 10


def test_cache_survives_a_restart(tmp_path):
    now = time.time()
    home = _home(tmp_path, [_rec(_iso(now - 60), out=10)])
    cache = tmp_path / "cache.json"
    UsageIndex(home, cache).refresh(now)
    fresh = UsageIndex(home, cache)              # nothing re-read
    assert fresh.window(WINDOW_5H, now)["tokens"]["output"] == 10


def test_truncated_file_is_re_read_from_zero(tmp_path):
    now = time.time()
    home = _home(tmp_path, [_rec(_iso(now - 60), out=10)])
    idx = UsageIndex(home, tmp_path / "cache.json")
    idx.refresh(now)
    (home / "projects" / "-proj" / "s1.jsonl").write_text(_rec(_iso(now - 60), out=3))
    idx.refresh(now)
    assert idx.window(WINDOW_5H, now)["tokens"]["output"] == 3


def test_records_without_a_timestamp_are_skipped(tmp_path):
    home = _home(tmp_path, ['{"type":"assistant","message":{"model":"claude-opus-5",'
                            '"usage":{"output_tokens":5}}}\n'])
    idx = UsageIndex(home, tmp_path / "cache.json")
    idx.refresh()
    assert idx.window(WINDOW_5H)["total_tokens"] == 0


def test_missing_home_is_not_fatal(tmp_path):
    idx = UsageIndex(tmp_path / "nope", tmp_path / "cache.json")
    idx.refresh()
    assert idx.snapshot()["five_hour"]["total_tokens"] == 0


def test_snapshot_says_what_it_is_not():
    idx = UsageIndex("/nonexistent", "/tmp/sc-usage-test.json")
    assert "rate-limit" in idx.snapshot()["note"]


def test_a_window_can_start_at_an_explicit_time(tmp_path):
    """Anthropic's 7-day limit is a FIXED window with a reset time, not a
    rolling seven days. Measuring spend over a different period and printing it
    beside their percentage made the two read as one figure when they were
    2.6x apart."""
    now = time.time()
    home = _home(tmp_path, [_rec(_iso(now - 30 * HOUR), out=500),   # before the reset
                            _rec(_iso(now - 2 * HOUR), out=7)])     # after it
    idx = UsageIndex(home, tmp_path / "cache.json")
    idx.refresh(now)
    rolling = idx.window(WINDOW_7D, now)["tokens"]["output"]
    since = idx.window(WINDOW_7D, now, start=now - 6 * HOUR)["tokens"]["output"]
    assert rolling == 507, rolling
    assert since == 7, since


def test_snapshot_uses_the_reset_when_it_is_given_one(tmp_path):
    now = time.time()
    home = _home(tmp_path, [_rec(_iso(now - 30 * HOUR), out=500),
                            _rec(_iso(now - 2 * HOUR), out=7)])
    idx = UsageIndex(home, tmp_path / "cache.json")
    idx.refresh(now)
    snap = idx.snapshot(now, seven_day_start=now - 6 * HOUR)
    assert snap["seven_day"]["tokens"]["output"] == 7
    assert snap["seven_day"]["since"] == pytest.approx(now - 6 * HOUR)
    # ...and falls back to a rolling week when it is not given one.
    assert idx.snapshot(now)["seven_day"]["tokens"]["output"] == 507


# --- lifetime -----------------------------------------------------------------
# The header used to sum the cost of LIVE sessions. That figure fell whenever
# one exited, mixed lifetimes of wildly different ages, and sat above a 7-day
# figure that was larger. Lifetime only goes up and means one thing.

def test_lifetime_counts_records_far_outside_the_window(tmp_path):
    """The point of it. The buckets keep seven days, so a total cannot be
    derived from them -- it needs its own accumulator that nothing prunes."""
    now = time.time()
    home = _home(tmp_path, [_rec(_iso(now - 60), out=10),
                            _rec(_iso(now - 40 * 24 * HOUR), out=1000)])
    idx = UsageIndex(home, tmp_path / "cache.json")
    idx.ensure_lifetime(now=now)
    life = idx.lifetime()
    assert life["tokens"]["output"] == 1010, life
    assert life["seeded"] is True
    # ...and the window still sees only the recent one.
    snap = idx.snapshot()
    assert snap["seven_day"]["tokens"]["output"] == 10


def test_pruning_the_window_does_not_touch_the_total(tmp_path):
    now = time.time()
    home = _home(tmp_path, [_rec(_iso(now - 40 * 24 * HOUR), out=500)])
    idx = UsageIndex(home, tmp_path / "cache.json")
    idx.ensure_lifetime(now=now)
    idx.refresh(now=now)                     # prunes the old bucket
    assert idx.lifetime()["tokens"]["output"] == 500
    assert idx.snapshot()["seven_day"]["tokens"]["output"] == 0


def test_the_seed_runs_once_and_survives_a_restart(tmp_path):
    """It re-reads every transcript in full -- the one place that does. Doing
    that on every poll would be the thing this project forbids."""
    now = time.time()
    home = _home(tmp_path, [_rec(_iso(now - 60), out=7)])
    cache = tmp_path / "cache.json"
    idx = UsageIndex(home, cache)
    idx.ensure_lifetime(now=now)
    idx.ensure_lifetime(now=now)             # no-op
    assert idx.lifetime()["tokens"]["output"] == 7, "seeded twice, counted twice"
    again = UsageIndex(home, cache)           # restart
    assert again.lifetime()["seeded"] is True
    again.ensure_lifetime(now=now)
    assert again.lifetime()["tokens"]["output"] == 7, "re-seeded after a restart"


def test_new_records_add_to_the_total_after_seeding(tmp_path):
    now = time.time()
    home = _home(tmp_path, [_rec(_iso(now - 60), out=5)])
    idx = UsageIndex(home, tmp_path / "cache.json")
    idx.ensure_lifetime(now=now)
    f = home / "projects" / "-proj" / "s1.jsonl"
    f.write_text(f.read_text() + _rec(_iso(now - 30), out=3))
    idx.refresh(now=now)
    assert idx.lifetime()["tokens"]["output"] == 8, "the incremental read was lost"


def test_workflow_subagents_are_counted(tmp_path):
    """Nested at <session>/subagents/workflows/<wf>/agent-<id>.jsonl. Two
    hand-written globs missed them: 153 files, ~400M tokens and ~$618 on one real
    machine. The same class of miss had already cost
    1.05 billion tokens once, which is why this walks the tree instead."""
    now = time.time()
    home = _home(tmp_path, [_rec(_iso(now - 60), out=1)])
    deep = (home / "projects" / "-proj" / "sess" / "subagents"
            / "workflows" / "wf_abc")
    deep.mkdir(parents=True)
    (deep / "agent-1.jsonl").write_text(_rec(_iso(now - 60), out=400))
    plain = home / "projects" / "-proj" / "sess" / "subagents"
    (plain / "agent-2.jsonl").write_text(_rec(_iso(now - 60), out=20))
    idx = UsageIndex(home, tmp_path / "cache.json")
    idx.ensure_lifetime(now=now)
    assert idx.lifetime()["tokens"]["output"] == 421, \
        "a nesting level was missed; add no globs, the walk should find it"


def test_lifetime_since_is_the_oldest_record(tmp_path):
    """"Lifetime" that silently means "since the cache was built" would be a
    lie the number cannot tell you, so the header shows the date."""
    now = time.time()
    old = now - 50 * 24 * HOUR
    home = _home(tmp_path, [_rec(_iso(old), out=1), _rec(_iso(now - 60), out=1)])
    idx = UsageIndex(home, tmp_path / "cache.json")
    idx.ensure_lifetime(now=now)
    assert abs(idx.lifetime()["since"] - old) < 2, idx.lifetime()["since"]


def test_lifetime_is_priced_and_never_negative(tmp_path):
    now = time.time()
    home = _home(tmp_path, [_rec(_iso(now - 60), out=1_000_000)])
    idx = UsageIndex(home, tmp_path / "cache.json")
    idx.ensure_lifetime(now=now)
    life = idx.lifetime()
    assert life["cost_usd"] > 0
    assert life["total_tokens"] == 1_000_000


def test_an_unknown_model_is_counted_as_unpriced_not_as_free(tmp_path):
    """cost_usd() returns 0.00 for a model RATES has never heard of, which is
    indistinguishable from a model that cost nothing. Billions of real
    tokens sat that way -- Opus 4.5 and Sonnet 4.5 had no rows -- and the
    figure said nothing. The next missing model shows up as a number."""
    now = time.time()
    home = _home(tmp_path, [_rec(_iso(now - 60), out=1000,
                                 model="claude-from-the-future-9")])
    idx = UsageIndex(home, tmp_path / "cache.json")
    idx.ensure_lifetime(now=now)
    life = idx.lifetime()
    assert life["cost_usd"] == 0.0
    assert life["unpriced_tokens"] == 1000, life
    assert "claude-from-the-future-9" in life["unpriced_models"]


def test_a_known_model_is_not_flagged_unpriced(tmp_path):
    now = time.time()
    home = _home(tmp_path, [_rec(_iso(now - 60), out=1000, model="claude-opus-4-5-20251101")])
    idx = UsageIndex(home, tmp_path / "cache.json")
    idx.ensure_lifetime(now=now)
    life = idx.lifetime()
    assert life["unpriced_tokens"] == 0, life
    assert life["cost_usd"] > 0, "the Opus 4.5 row is missing again"


def test_archive_roots_are_folded_in_once(tmp_path):
    """Transcripts from an older install (another OS, a previous machine) are
    frozen -- nothing appends to them -- and may be the only record of a
    project's start."""
    now = time.time()
    home = _home(tmp_path, [_rec(_iso(now - 60), out=5)])
    archive = tmp_path / "old" / "projects" / "-proj"
    archive.mkdir(parents=True)
    (archive / "ancient.jsonl").write_text(
        _rec(_iso(now - 200 * 24 * HOUR), out=700))
    idx = UsageIndex(home, tmp_path / "cache.json",
                     archive_roots=(tmp_path / "old" / "projects",))
    idx.ensure_lifetime(now=now)
    assert idx.lifetime()["tokens"]["output"] == 705, "the archive was not counted"
    # ...and the archive is months old, so it must not reach the 7-day window.
    assert idx.snapshot()["seven_day"]["tokens"]["output"] == 5
    # A later poll must not count it a second time.
    idx.refresh(now=now)
    assert idx.lifetime()["tokens"]["output"] == 705, "the archive was double-counted"


def test_an_archive_root_that_is_the_live_tree_is_skipped(tmp_path):
    """Otherwise every live transcript is counted twice -- once by the walk and
    once by the archive pass."""
    now = time.time()
    home = _home(tmp_path, [_rec(_iso(now - 60), out=5)])
    idx = UsageIndex(home, tmp_path / "cache.json",
                     archive_roots=(home / "projects",))
    idx.ensure_lifetime(now=now)
    assert idx.lifetime()["tokens"]["output"] == 5, "the live tree was double-counted"


def test_a_missing_archive_root_is_not_an_error(tmp_path):
    now = time.time()
    home = _home(tmp_path, [_rec(_iso(now - 60), out=5)])
    idx = UsageIndex(home, tmp_path / "cache.json",
                     archive_roots=(tmp_path / "nope",))
    idx.ensure_lifetime(now=now)
    assert idx.lifetime()["tokens"]["output"] == 5


def test_a_fixture_home_never_reaches_the_real_archive(tmp_path):
    """The guard itself. ARCHIVE_ROOTS are absolute paths into a real history;
    a bare module constant meant every test with a temp home scanned 1,021
    real transcripts. Eight failed and the suite took 58 seconds."""
    idx = UsageIndex(tmp_path / ".claude", tmp_path / "cache.json")
    assert idx.archive_roots == (), idx.archive_roots


def _msg(ts_iso, mid, out=0, block="text", model="claude-opus-5"):
    """One content block of a reply, as Claude Code writes it: the reply's
    whole usage on every block's line."""
    return json.dumps({"type": "assistant", "timestamp": ts_iso, "requestId": "req_" + mid,
                       "message": {"id": mid, "model": model, "content": [{"type": block}],
                                   "usage": {"input_tokens": 0, "output_tokens": out,
                                             "cache_read_input_tokens": 0,
                                             "cache_creation_input_tokens": 0}}}) + "\n"


def test_a_reply_written_as_three_lines_counts_once(tmp_path):
    """Measured 2026-09-23: 622 of 918 replies in one session were written on
    more than one line with identical usage, and each line was counted."""
    now = time.time()
    t = _iso(now - 60)
    home = _home(tmp_path, [_msg(t, "msg_A", 100, "thinking"), _msg(t, "msg_A", 100, "text"),
                            _msg(t, "msg_A", 100, "tool_use"), _msg(t, "msg_B", 7)])
    idx = UsageIndex(home, tmp_path / "cache.json", archive_roots=())
    idx.ensure_lifetime(now=now)
    assert idx.window(WINDOW_5H, now)["tokens"]["output"] == 107
    assert idx.lifetime()["tokens"]["output"] == 107


def test_a_reply_split_across_two_polls_still_counts_once(tmp_path):
    now = time.time()
    t = _iso(now - 60)
    home = _home(tmp_path, [_msg(t, "msg_A", 100, "thinking")])
    idx = UsageIndex(home, tmp_path / "cache.json", archive_roots=())
    idx.ensure_lifetime(now=now)
    with (home / "projects" / "-proj" / "s1.jsonl").open("a") as fh:
        fh.write(_msg(t, "msg_A", 100, "tool_use"))
    again = UsageIndex(home, tmp_path / "cache.json", archive_roots=())    # across a restart too
    again.refresh(now)
    assert again.lifetime()["tokens"]["output"] == 100


def test_a_rewritten_file_replaces_its_share_of_the_lifetime(tmp_path):
    """The re-read after a truncation cleared the window and left the total,
    so the whole file was added to lifetime a second time."""
    now = time.time()
    home = _home(tmp_path, [_rec(_iso(now - 60), out=10), _rec(_iso(now - 30), out=5)])
    idx = UsageIndex(home, tmp_path / "cache.json", archive_roots=())
    idx.ensure_lifetime(now=now)
    assert idx.lifetime()["tokens"]["output"] == 15
    (home / "projects" / "-proj" / "s1.jsonl").write_text(_rec(_iso(now - 60), out=10))
    idx.refresh(now)
    assert idx.lifetime()["tokens"]["output"] == 10, "replaced, not added to"


def test_a_cache_from_the_old_counting_is_rebuilt(tmp_path):
    now = time.time()
    t = _iso(now - 60)
    home = _home(tmp_path, [_msg(t, "msg_A", 100), _msg(t, "msg_A", 100)])
    (tmp_path / "cache.json").write_text(json.dumps(
        {"files": {}, "lifetime": {"claude-opus-5": {"output": 99999}}, "lifetime_seeded": True}))
    idx = UsageIndex(home, tmp_path / "cache.json", archive_roots=())
    idx.ensure_lifetime(now=now)
    assert idx.lifetime()["tokens"]["output"] == 100


def test_seed_reads_come_back_in_walk_order_and_whole(tmp_path):
    """The seed pass reads on threads; the parse must see every file, whole,
    in the order the walk found them, and a vanished file as None."""
    from agent_companion import usage
    jobs = []
    for i in range(40):
        p = tmp_path / f"{i}.jsonl"
        p.write_bytes(b"x" * (i * 97 + 1))
        jobs.append((p, {"i": i}, 0, p.stat().st_size))
    jobs.append((tmp_path / "gone.jsonl", {"i": "gone"}, 0, 10))
    for workers in (1, 8):
        got = list(usage._read_tails(jobs, workers))
        assert [e["i"] for e, _ in got] == list(range(40)) + ["gone"]
        assert [len(c) for _, c in got[:40]] == [i * 97 + 1 for i in range(40)]
        assert got[-1][1] is None


def test_usage_lines_split_as_splitlines_did():
    """Only the lines naming "usage", cut at the same line breaks as before."""
    from agent_companion import usage
    chunk = ('{"a":1}\n{"usage":1}\r\n{"b":" "}{"usage":2}\n'
             '{"c":"\xe9"}\n{"usage":3}').encode() + b"\xff\n"
    old = [l for l in chunk.decode("utf-8", "replace").splitlines() if '"usage"' in l]
    assert list(usage._usage_lines(chunk)) == old
