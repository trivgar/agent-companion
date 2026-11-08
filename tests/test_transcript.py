import datetime
import json

from agent_companion.transcript import TranscriptReader


def _rec(**kw):
    return json.dumps(kw) + "\n"


def _usage(i=0, o=0, cr=0, cw=0, cw_1h=None, model="claude-opus-5", **extra):
    usage = {"input_tokens": i, "output_tokens": o,
             "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}
    if cw_1h is not None:
        usage["cache_creation"] = {"ephemeral_1h_input_tokens": cw_1h,
                                   "ephemeral_5m_input_tokens": cw - cw_1h}
    return _rec(type="assistant", message={"model": model, "usage": usage}, **extra)


def _prompt(text, **extra):
    return _rec(type="user", message={"role": "user", "content": text}, **extra)


# --- totals, context, model -----------------------------------------------

def test_totals_and_context(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_usage(i=10, o=20, cr=100, cw=5) + _usage(i=1, o=2, cr=500, cw=3))
    r = TranscriptReader().read(p)
    assert r["tokens"]["output"] == 22
    assert r["tokens"]["cache_read"] == 600
    assert r["context_used"] == 504      # LAST record only, not the sum
    assert r["model"] == "claude-opus-5"


def test_one_hour_cache_writes_are_tracked_separately(tmp_path):
    """usage.cache_creation records the TTL; 1h writes bill at 2x."""
    p = tmp_path / "t.jsonl"
    p.write_text(_usage(cw=300, cw_1h=300) + _usage(cw=100, cw_1h=40))
    t = TranscriptReader().read(p)["tokens"]
    assert t["cache_write"] == 400        # the total
    assert t["cache_write_1h"] == 340     # the subset billed at 2x


def test_last_assistant_cache_usage_sets_the_one_hour_ttl(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_usage(cw=300, cw_1h=0) + _usage(cw=300, cw_1h=300))
    assert TranscriptReader().read(p)["cache_ttl_s"] == 3600


def test_last_assistant_cache_usage_sets_the_five_minute_ttl(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_usage(cw=300, cw_1h=300) + _usage(cw=300, cw_1h=0)
                 + _usage(cw=0))
    assert TranscriptReader().read(p)["cache_ttl_s"] == 300


def test_sidechain_cache_write_does_not_change_ttl(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_usage(cw=300, cw_1h=300) + _usage(cw=300, cw_1h=0,
                                                       isSidechain=True))
    assert TranscriptReader().read(p)["cache_ttl_s"] == 3600


def test_cache_write_without_breakdown_leaves_1h_zero(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_usage(cw=500))
    t = TranscriptReader().read(p)["tokens"]
    assert t["cache_write"] == 500
    assert t["cache_write_1h"] == 0


def test_sidechain_tokens_count_but_do_not_move_context(tmp_path):
    """A subagent's turn is billed, but the context bar tracks the main thread."""
    p = tmp_path / "t.jsonl"
    p.write_text(_usage(o=10, cr=900_000) + _usage(o=5, cr=20, isSidechain=True))
    r = TranscriptReader().read(p)
    assert r["tokens"]["output"] == 15
    assert r["context_used"] == 900_000


def test_compact_boundary_resets_context_to_post_compaction_size(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        _usage(i=10, o=20, cr=489_684, cw=5)
        + _rec(type="system", subtype="compact_boundary",
               compactMetadata={"postTokens": 24_643})
    )
    r = TranscriptReader().read(p)
    assert r["context_used"] == 24_643
    assert r["tokens"] == {
        "input": 10, "output": 20, "cache_read": 489_684,
        "cache_write": 5, "cache_write_1h": 0,
    }


def test_assistant_usage_after_compact_boundary_sets_context(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        _usage(cr=489_699)
        + _rec(type="system", subtype="compact_boundary",
               compactMetadata={"postTokens": 24_643})
        + _usage(i=1, cr=24, cw=3)
    )
    assert TranscriptReader().read(p)["context_used"] == 28


def test_compact_boundary_without_post_tokens_clears_context(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_usage(cr=489_699) + _rec(
        type="system", subtype="compact_boundary", compactMetadata={}
    ))
    assert TranscriptReader().read(p)["context_used"] == 0


def test_sidechain_compact_boundary_does_not_move_context(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_usage(cr=489_699) + _rec(
        type="system", subtype="compact_boundary", isSidechain=True,
        compactMetadata={"postTokens": 24_643}
    ))
    assert TranscriptReader().read(p)["context_used"] == 489_699


# --- typed records ---------------------------------------------------------

def test_typed_records(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        _rec(type="ai-title", aiTitle="Fixing the thermals")
        + _rec(type="permission-mode", permissionMode="bypassPermissions")
    )
    r = TranscriptReader().read(p)
    assert r["title"] == "Fixing the thermals"
    assert r["permission_mode"] == "bypassPermissions"


def test_custom_title_beats_ai_title(tmp_path):
    """A title the user set by hand outranks the generated one."""
    p = tmp_path / "t.jsonl"
    p.write_text(_rec(type="ai-title", aiTitle="Generated")
                 + _rec(type="custom-title", customTitle="Mine"))
    assert TranscriptReader().read(p)["title"] == "Mine"


def test_last_prompt_is_the_last_human_message(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_prompt("first thing") + _usage(o=1) + _prompt("second thing"))
    assert TranscriptReader().read(p)["last_prompt"] == "second thing"


def test_the_last_prompt_carries_when_you_sent_it(tmp_path):
    """A dashboard reads this as you at the keyboard; injected text must not move it."""
    p = tmp_path / "t.jsonl"
    p.write_text(_prompt("first", timestamp="2026-09-25T17:40:00.000Z")
                 + _prompt("mine", timestamp="2026-09-25T17:50:00.000Z")
                 + _prompt("Another Claude session sent a message:", isMeta=True,
                           timestamp="2026-09-25T18:30:00.000Z"))
    sent = datetime.datetime(2026, 9, 25, 17, 50, tzinfo=datetime.timezone.utc).timestamp()
    assert TranscriptReader().read(p)["last_prompt_at"] == sent


def test_a_prompt_without_a_stamp_has_no_time(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_prompt("no stamp"))
    assert TranscriptReader().read(p)["last_prompt_at"] is None


def test_tool_results_are_not_prompts(tmp_path):
    """A user record whose content is a tool_result list is not a prompt."""
    p = tmp_path / "t.jsonl"
    p.write_text(_prompt("the real prompt") + _rec(
        type="user", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "x", "content": "output"}]}))
    assert TranscriptReader().read(p)["last_prompt"] == "the real prompt"


def test_meta_messages_are_not_prompts(tmp_path):
    """isMeta marks injected text (hooks, cross-session messages), not the user."""
    p = tmp_path / "t.jsonl"
    p.write_text(_prompt("the real prompt")
                 + _prompt("Another Claude session sent a message:", isMeta=True))
    assert TranscriptReader().read(p)["last_prompt"] == "the real prompt"


def test_injected_wrappers_are_not_prompts(tmp_path):
    """<task-notification> arrives as a plain-string user message, unflagged."""
    p = tmp_path / "t.jsonl"
    p.write_text(_prompt("the real prompt")
                 + _prompt("<task-notification>\n<task-id>abc</task-id>"))
    assert TranscriptReader().read(p)["last_prompt"] == "the real prompt"


def test_pasted_markup_is_still_a_prompt(tmp_path):
    """The filter is a denylist, so real markup the user pasted survives."""
    p = tmp_path / "t.jsonl"
    p.write_text(_prompt("<div>fix this markup</div>"))
    assert TranscriptReader().read(p)["last_prompt"] == "<div>fix this markup</div>"


def test_long_prompt_is_truncated(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_prompt("x" * 5000))
    assert len(TranscriptReader().read(p)["last_prompt"]) <= 500


# --- the watermark ---------------------------------------------------------

def test_second_read_only_consumes_new_bytes(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_usage(o=5))
    reader = TranscriptReader()
    reader.read(p)
    first_offset = reader._state[str(p)].offset
    with p.open("a") as fh:
        fh.write(_usage(o=7))
    r = reader.read(p)
    assert r["tokens"]["output"] == 12
    assert reader._state[str(p)].offset > first_offset


def test_incremental_matches_full_reread(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_usage(o=5))
    incremental = TranscriptReader()
    incremental.read(p)
    with p.open("a") as fh:
        fh.write(_usage(o=7))
    assert incremental.read(p)["tokens"] == TranscriptReader().read(p)["tokens"]


def test_truncated_file_resets_watermark(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_usage(o=100))
    reader = TranscriptReader()
    reader.read(p)
    p.write_text(_usage(o=3))
    assert reader.read(p)["tokens"]["output"] == 3


def test_partial_last_line_is_not_consumed(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_usage(o=5) + '{"type":"assist')
    reader = TranscriptReader()
    assert reader.read(p)["tokens"]["output"] == 5
    with p.open("a") as fh:
        fh.write('ant","message":{"model":"claude-opus-5",'
                 '"usage":{"output_tokens":9}}}\n')
    assert reader.read(p)["tokens"]["output"] == 14


def test_missing_file_returns_empty(tmp_path):
    r = TranscriptReader().read(tmp_path / "nope.jsonl")
    assert r["tokens"]["output"] == 0
    assert r["title"] == ""


def test_the_last_reply_is_a_text_block_not_a_tool_call(tmp_path):
    """The sessions tab orders by it: when it last wrote to you."""
    import json
    from agent_companion.transcript import TranscriptReader
    f = tmp_path / "t.jsonl"
    use = {"input_tokens": 1, "output_tokens": 1}
    f.write_text("\n".join(json.dumps(r) for r in [
        {"type": "assistant", "timestamp": "2026-09-29T10:00:00Z",
         "message": {"content": [{"type": "text", "text": "Done."}], "usage": use}},
        {"type": "assistant", "timestamp": "2026-09-29T10:05:00Z",
         "message": {"content": [{"type": "tool_use", "name": "Bash"}], "usage": use}},
        {"type": "assistant", "timestamp": "2026-09-29T10:06:00Z", "isSidechain": True,
         "message": {"content": [{"type": "text", "text": "helper"}], "usage": use}},
    ]) + "\n")
    import datetime
    want = datetime.datetime(2026, 9, 29, 10, 0, tzinfo=datetime.timezone.utc).timestamp()
    assert TranscriptReader().read(f)["last_reply_at"] == want


def test_last_call_is_a_main_thread_assistant_turn_with_usage(tmp_path):
    f = tmp_path / "t.jsonl"
    use = {"input_tokens": 1, "output_tokens": 1}
    f.write_text("\n".join(json.dumps(r) for r in [
        {"type": "assistant", "timestamp": "2026-09-29T10:00:00Z",
         "message": {"content": [{"type": "text", "text": "Done."}], "usage": use}},
        {"type": "assistant", "timestamp": "2026-09-29T10:10:00Z",
         "message": {"content": [{"type": "tool_use", "name": "Bash"}], "usage": use}},
        {"type": "assistant", "timestamp": "2026-09-29T10:11:00Z", "isSidechain": True,
         "message": {"content": [{"type": "tool_use", "name": "Bash"}], "usage": use}},
    ]) + "\n")
    want = datetime.datetime(2026, 9, 29, 10, 10, tzinfo=datetime.timezone.utc).timestamp()
    assert TranscriptReader().read(f)["last_call_at"] == want
