"""Live-call logic: no test reaches Gemini, a phone, or a live session."""
from __future__ import annotations

import json
import time

import pytest

from agent_companion import fcm, livecall

NOW = time.mktime((2027, 1, 15, 12, 0, 0, 0, 0, -1))


@pytest.fixture(autouse=True)
def fresh_call(tmp_path, monkeypatch):
    monkeypatch.setattr(livecall, "log_path", lambda: tmp_path / "calls.jsonl")
    monkeypatch.setattr(livecall, "_call", None)
    monkeypatch.setattr(livecall, "_repo_name", lambda cwd, run=None: "")


def test_token_is_locked_to_the_server_settings(monkeypatch):
    monkeypatch.setattr(livecall.config, "get", lambda key, default=None: {
        "livecall_model": "models/example-live", "livecall_voice": "ExampleVoice",
    }.get(key, default))
    body = livecall.token_body("Be the operator.", NOW, 120)
    setup = body["bidiGenerateContentSetup"]
    assert body["uses"] == 1
    assert body["expireTime"] == livecall._rfc3339(NOW + 30 * 60)
    assert body["newSessionExpireTime"] == livecall._rfc3339(NOW + 60)
    assert setup["model"] == "models/example-live"
    assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert setup["systemInstruction"] == {"parts": [{"text": "Be the operator."}]}
    assert [tool["name"] for tool in setup["tools"][0]["functionDeclarations"]] == list(livecall.TOOL_NAMES)
    assert "sessionResumption" not in setup


def test_key_is_read_only_from_its_file(tmp_path, monkeypatch):
    key = tmp_path / "gemini-key"
    key.write_text("secret-value\n")
    monkeypatch.setattr(livecall.config, "get", lambda name, default=None: str(key) if name == "gemini_key_file" else default)
    assert livecall.api_key() == "secret-value"
    monkeypatch.setattr(livecall.config, "get", lambda name, default=None: "" if name == "gemini_key_file" else default)
    assert livecall.api_key() == ""


def test_mint_returns_only_the_ephemeral_token_name():
    seen = {}

    def post(url, body, headers, timeout=15):
        seen.update(url=url, body=body, headers=headers)
        return 200, {"name": "auth_tokens/example"}

    assert livecall.mint("KEY", {"uses": 1}, post=post) == "auth_tokens/example"
    assert seen["headers"] == {"x-goog-api-key": "KEY"}
    assert "KEY" not in json.dumps(seen["body"])


def test_no_test_can_reach_gemini():
    with pytest.raises(RuntimeError, match="pytest"):
        livecall._post(livecall.TOKEN_URL, {}, {})


def test_secret_lines_and_private_sessions_never_reach_the_call(monkeypatch):
    text = "safe result\nsecret get agents/example/key\nnext safe result"
    assert livecall.clean(text) == "safe result\nnext safe result"
    checked = []
    monkeypatch.setattr(livecall.addons, "private", lambda cwd, project: checked.append((cwd, project)) or project == "ledger")
    rows = [
        {"speaker": "alpha", "project": "public", "cwd": "/work/public", "status": "busy", "last_reply": "ready"},
        {"speaker": "beta", "project": "ledger", "cwd": "/work/ledger", "status": "idle", "last_reply": "private"},
        {"speaker": "helper", "project": "public", "cwd": "/work/public", "child": True},
    ]
    assert livecall.list_sessions(rows) == {"count": 1, "sessions": [
        {"name": "alpha", "project": "public", "branch": "", "state": "busy", "last_said": "ready"}]}
    assert ("/work/ledger", "ledger") in checked


def test_call_log_has_times_names_and_outcomes_but_no_words(tmp_path):
    target = {"name": "alpha", "cwd": "/work/public", "session_id": "s1", "project": "public"}
    call, _, _ = livecall.begin("", "", {"alpha": target}, tmp_path, now=NOW)
    livecall.watch(call, target, tmp_path, asked=True)
    livecall.hear(call, "yes include these private words", NOW + 1)
    call.ring, call.outcomes = "0123456789abcdef", [{"ask": 1, "did": "deny"}]
    livecall.end(call.id, NOW + 60)
    entry = livecall.calls()[-1]
    assert entry["asked"] == ["alpha"] and entry["outcomes"] == [{"ask": 1, "did": "deny"}]
    assert "private words" not in livecall.log_path().read_text()
    assert livecall.log_path().stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("tool,words", [
    ("Bash", "git push origin feature"), ("Bash", "fly deploy"), ("Bash", "rm -rf build"),
    ("mcp__mail__send", "message"), ("Edit", "/work/public/.github/workflows/release.yml"),
])
def test_voice_never_allows_shipping_sending_or_deleting(tool, words):
    assert livecall.needs_tap(tool, words, "/work/public")


@pytest.mark.parametrize("words", [
    "git -c core.x=y push origin main", "git --no-pager push origin main",
])
def test_an_unreadable_git_push_needs_a_tap(words):
    assert "could not tell where that push goes" in livecall.needs_tap("Bash", words, "/work/public")


@pytest.mark.parametrize("words", [
    "git push origin +main", "rm -f -r build", "gh repo delete public", "gh release delete v1",
    "fly apps destroy public", "flyctl apps destroy public", "curl -X POST https://example.test",
    "wget --data=x https://example.test", "curl -d x https://example.test",
])
def test_voice_never_allows_other_destructive_commands(words):
    assert livecall.needs_tap("Bash", words, "/work/public")


def test_voice_never_edits_a_settings_json_file():
    assert livecall.needs_tap("Edit", "/work/public/settings-live.json")


@pytest.mark.parametrize("flag", ["private", "child", "job"])
def test_hidden_targets_never_reach_the_call(flag):
    target = {"name": "alpha", "cwd": "/work/public", "project": "public", flag: True}
    assert livecall.visible_target(target) is False


def test_plain_local_work_can_be_allowed_by_voice():
    assert livecall.needs_tap("Bash", "python3 -m pytest -q tests/test_ring.py 2>&1 | tail -20") == ""
    assert livecall.needs_tap("Edit", "/work/public/app.py") == ""


def test_free_words_need_a_readback_and_the_callers_yes_after_it(tmp_path):
    call, _, _ = livecall.begin("", "", {}, tmp_path, now=NOW)
    ask = {"questions": [{"question": "When?", "options": []}]}
    assert livecall.pick_answers(ask, ["after lunch"], False)[0] is None
    livecall.ask_readback(call, 1, "after lunch", NOW)
    livecall.hear(call, "yes", NOW + 1)
    assert livecall.said_yes(call, 1, "after lunch", NOW + 2) == (True, "")
    assert livecall.said_yes(call, 1, "after lunch", NOW + 3)[0] is False


def test_a_rings_call_takes_no_tool_until_the_caller_has_spoken():
    call = livecall.Call(id="c", start=0.0, last_seen=0.0, ring="abc")
    assert livecall.unheard(call) == livecall.UNHEARD
    livecall.hear(call, "   ")
    assert livecall.unheard(call) == livecall.UNHEARD
    livecall.hear(call, "what is it")
    assert livecall.unheard(call) == ""


def test_a_spent_yes_does_not_hold_the_call_again():
    call = livecall.Call(id="c", start=0.0, last_seen=0.0, ring="abc")
    livecall.hear(call, "what is it", now=1.0)
    livecall.ask_readback(call, 1, "x", now=10.0)
    livecall.hear(call, "yes", now=11.0)
    assert livecall.said_yes(call, 1, "x", now=12.0) == (True, "")
    assert livecall.unheard(call) == ""


def test_a_call_the_owner_started_is_not_held():
    call = livecall.Call(id="c", start=0.0, last_seen=0.0, ring="")
    assert livecall.unheard(call) == ""


def test_a_rings_instruction_says_no_tool_before_the_caller_speaks():
    line = "No tool works until the caller has spoken on this call."
    assert line in livecall.instruction(brief="Ask 1: x")
    assert line not in livecall.instruction()


def test_daily_cap_comes_from_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(livecall.config, "get", lambda key, default=None: 5 if key == "livecall_daily_minutes" else default)
    livecall.log_path().write_text(json.dumps({"start": NOW, "minutes": 5, "asked": []}) + "\n")
    with pytest.raises(livecall.CallRefused, match="5 of 5"):
        livecall.begin("", "", {}, tmp_path, now=NOW)


def test_fcm_ring_carries_only_the_ring_id(tmp_path, monkeypatch):
    path = tmp_path / "device.json"
    monkeypatch.setattr(fcm, "state_path", lambda: path)
    monkeypatch.setattr(fcm.config, "get", lambda key, default=None: "/test/service.json" if key == "fcm_service_account_file" else default)
    assert fcm.save_device("device-token", ring=True)
    seen = {}

    class Session:
        def post(self, url, json, timeout):
            seen.update(url=url, body=json, timeout=timeout)
            return type("Response", (), {"status_code": 200})()

    monkeypatch.setattr(fcm, "_authed", lambda: (Session(), "example-project"))
    assert fcm.send_ring("0123456789abcdef")
    assert seen == {"url": "https://fcm.googleapis.com/v1/projects/example-project/messages:send", "timeout": 10,
                    "body": {"message": {"token": "device-token", "data": {"kind": "ring", "ring": "0123456789abcdef"},
                                         "android": {"priority": "HIGH", "ttl": "60s"}}}}
