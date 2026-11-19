"""Shared CLI/MCP phone-job helpers."""
from agent_companion.phone import job


def test_targets_accept_only_a_look_number_or_a_point():
    assert job.point("120,640") == (120, 640)
    assert job.point("120,") is None
    assert job.target("12") == {"n": 12}
    assert job.target("120,640") == {"x": 120, "y": 640}


def test_result_words_include_a_safe_refusal_code():
    assert job.result_text({"ok": False, "code": "held", "text": "waiting"}) == "waiting [held]"
    assert job.result_text({"ok": True, "text": "look", "shot": "/safe/path"}) == "look\nscreenshot: /safe/path"
