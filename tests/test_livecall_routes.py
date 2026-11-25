"""Owner-only live-call and ring routes. These use TestClient in CI, not here."""
from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agent_companion import livecall, ring, server  # noqa: E402
from agent_companion.server import auth  # noqa: E402
from agent_companion.server import asks  # noqa: E402
from agent_companion.server import livecall as routes  # noqa: E402

TOKEN = "t" * 32
OWNER = "o" * 64
PAGE = {auth.OWNER_HEADER: OWNER, "origin": "http://testserver", "host": "testserver"}
TARGET = {"name": "alpha", "pid": 1, "pane": "%1", "cwd": "/work/public", "session_id": "s1",
          "status": "idle", "waiting": False, "project": "public"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "FEATURES", [])
    monkeypatch.setattr(server, "live_call_enabled", lambda: True)
    monkeypatch.setattr(livecall, "log_path", lambda: tmp_path / "calls.jsonl")
    monkeypatch.setattr(livecall, "_call", None)
    monkeypatch.setattr(livecall, "_repo_name", lambda cwd, run=None: "")
    monkeypatch.setattr(livecall, "api_key", lambda: "not-returned")
    monkeypatch.setattr(livecall, "_post", lambda *args, **kwargs: (200, {"name": "auth_tokens/example"}))
    monkeypatch.setattr(routes.chat, "known", lambda: {"alpha": TARGET})
    monkeypatch.setattr(routes, "_rows", lambda: [dict(TARGET, speaker="alpha")])
    monkeypatch.setattr(routes, "_projects", lambda: tmp_path / "projects")
    monkeypatch.setattr(routes.livecall, "visible_target", lambda target: True)
    monkeypatch.setattr(routes.fcm, "state_path", lambda: tmp_path / "fcm-device.json")
    monkeypatch.setattr(ring, "STATE", tmp_path / "ring.json")
    monkeypatch.setattr(asks, "store", lambda: tmp_path / "asks")
    return TestClient(server.create_app(token=TOKEN, owner_key=OWNER))


def post(client, path, body, headers=PAGE):
    return client.post(path, json=body, headers=headers)


def _spoke(client, call_id):
    assert post(client, "/api/call/heard", {"call_id": call_id, "text": "what is it"}).status_code == 200


def test_live_call_is_hidden_and_routes_are_absent_without_a_key_file(monkeypatch):
    monkeypatch.setattr(server, "FEATURES", [])
    monkeypatch.setenv("AGENT_COMPANION_GEMINI_KEY_FILE", "")
    client = TestClient(server.create_app(token=TOKEN, owner_key=OWNER))
    page = client.get("/")
    assert 'id="call"' not in page.text
    assert "/static/call.js" not in page.text
    assert post(client, "/api/call/token", {}).status_code == 404


def test_every_call_write_needs_an_owner_key_json_and_same_origin(client):
    for path in ("/api/cave/ring/decide", "/api/call/answer", "/api/call/token",
                 "/api/call/tool/list_sessions", "/api/call/updates", "/api/call/heard", "/api/call/end"):
        assert post(client, path, {}, {}).status_code == 401
        assert client.post(path, json={}, headers={auth.OWNER_HEADER: OWNER}).status_code == 403


def test_token_never_returns_the_api_key_and_private_target_is_omitted(client, monkeypatch):
    response = post(client, "/api/call/token", {"agent": "alpha"})
    assert response.status_code == 200
    assert response.json()["token"] == "auth_tokens/example"
    assert "not-returned" not in response.text
    monkeypatch.setattr(routes.livecall, "visible_target", lambda target: False)
    response = post(client, "/api/call/token", {"agent": "alpha"})
    assert response.json()["agent"] == ""


def test_ring_button_and_answer_keys_are_one_time(client):
    ask = asks.create("alpha", "public", "s1", "Bash", {"command": "ls"}, now=time.time())
    key = ring.issue(ask["id"], time.time())
    response = post(client, "/api/cave/ring/decide", {"ask": ask["id"], "decision": "allow", "n": key})
    assert response.status_code == 200
    assert post(client, "/api/cave/ring/decide", {"ask": ask["id"], "decision": "deny", "n": key}).status_code == 403
    ring_id, answer_key = ring.new_ring("alpha", [], time.time())
    assert client.get(f"/api/ring/{ring_id}", headers={auth.OWNER_HEADER: OWNER}).status_code == 200
    assert post(client, "/api/call/answer", {"ring": ring_id, "n": answer_key}).json() == {"ok": True, "agent": "alpha"}
    assert post(client, "/api/call/answer", {"ring": ring_id, "n": answer_key}).status_code == 403


def test_a_shortened_permission_is_never_voice_allowed(client):
    ask = asks.create("alpha", "public", "s1", "Bash", {"command": "ls", "unshown": "x"}, now=time.time())
    ring_id, key = ring.new_ring("alpha", [], time.time(), ask_ids=[ask["id"]])
    ring.answer(ring_id, key)
    opened = post(client, "/api/call/token", {"ring": ring_id}).json()
    _spoke(client, opened["call_id"])
    result = post(client, "/api/call/tool/answer_permission", {"call_id": opened["call_id"],
                  "args": {"ask": 1, "decision": "allow", "confirmed": True}}).json()["result"]
    assert result["done"] is False and "shortened" in result["detail"]


def test_a_confirmed_permission_allow_needs_a_heard_yes(client):
    ask = asks.create("alpha", "public", "s1", "Bash", {"command": "ls"}, now=time.time())
    ring_id, key = ring.new_ring("alpha", [], time.time(), ask_ids=[ask["id"]])
    ring.answer(ring_id, key)
    opened = post(client, "/api/call/token", {"ring": ring_id}).json()
    _spoke(client, opened["call_id"])
    result = post(client, "/api/call/tool/answer_permission", {"call_id": opened["call_id"],
                  "args": {"ask": 1, "decision": "allow", "confirmed": True}}).json()["result"]
    assert result["done"] is False and "Read it back first" in result["detail"]
    assert asks.get(ask["id"])["status"] == "pending"


def test_a_shortened_ring_allow_keeps_its_key_for_deny(client):
    ask = asks.create("alpha", "public", "s1", "Bash", {"command": "ls", "unshown": "x"}, now=time.time())
    key = ring.issue(ask["id"], time.time())
    refused = post(client, "/api/cave/ring/decide", {"ask": ask["id"], "decision": "allow", "n": key})
    assert refused.status_code == 409 and refused.json()["detail"] == "open it and show all, then allow"
    assert post(client, "/api/cave/ring/decide", {"ask": ask["id"], "decision": "deny", "n": key}).status_code == 200


@pytest.mark.parametrize("refusal", ["key", "cap"])
def test_a_refused_call_does_not_spend_its_ring_brief(client, monkeypatch, refusal):
    ask = asks.create("alpha", "public", "s1", "Bash", {"command": "ls"}, now=time.time())
    ring_id, key = ring.new_ring("alpha", [], time.time(), ask_ids=[ask["id"]])
    ring.answer(ring_id, key)
    real_get = livecall.config.get
    if refusal == "key":
        monkeypatch.setattr(livecall, "api_key", lambda: "")
    else:
        livecall.log_path().write_text(json.dumps({"start": time.time(), "minutes": 1}) + "\n")
        monkeypatch.setattr(livecall.config, "get", lambda name, default=None: 1 if name == "livecall_daily_minutes" else real_get(name, default))
    assert post(client, "/api/call/token", {"ring": ring_id}).status_code == 409
    monkeypatch.setattr(livecall, "api_key", lambda: "not-returned")
    if refusal == "cap":
        monkeypatch.setattr(livecall.config, "get", lambda name, default=None: 120 if name == "livecall_daily_minutes" else real_get(name, default))
    opened = post(client, "/api/call/token", {"ring": ring_id})
    assert opened.status_code == 200 and opened.json()["brief"] == 1




def test_a_call_holds_its_agents_clips_renews_the_hold_and_lets_it_go(client, tmp_path, monkeypatch):
    tts = tmp_path / "tts"
    tts.mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "audio-effects.cfg").write_text("alpha|gain 8||0.7\n")
    quiet = tts / "quiet.sh"
    quiet.write_text('#!/bin/sh\necho "$@" >> "$(dirname "$0")/quiet.log"\nrm -f "$(dirname "$0")/callhold-$2"\n')
    quiet.chmod(0o755)
    monkeypatch.setenv("AGENT_COMPANION_TTS_DIR", str(tts))
    call_id = post(client, "/api/call/token", {"agent": "alpha"}).json()["call_id"]
    assert (tts / "callhold-alpha").is_file()
    (tts / "callhold-alpha").unlink()
    assert post(client, "/api/call/updates", {"call_id": call_id}).status_code == 200
    assert (tts / "callhold-alpha").is_file(), "each poll renews the hold"
    assert post(client, "/api/call/end", {"call_id": call_id}).json() == {"ok": True}
    assert (tts / "quiet.log").read_text() == "callrelease alpha\n"
    assert not (tts / "callhold-alpha").exists()


def test_without_a_coordinator_a_call_holds_nothing(client, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_TTS_DIR", str(tmp_path / "none"))
    call_id = post(client, "/api/call/token", {"agent": "alpha"}).json()["call_id"]
    assert post(client, "/api/call/end", {"call_id": call_id}).json() == {"ok": True}
    assert not (tmp_path / "none").exists()


def test_fetching_a_rings_brief_marks_it_fetched(client):
    then = time.time() - ring.LAND_S - 5
    first, _ = ring.new_ring("alpha", [{"title": "t1", "body": "b", "url": "/u1", "tag": "x", "actions": []}], then)
    ring.new_ring("alpha", [{"title": "t2", "body": "b", "url": "/u2", "tag": "x", "actions": []}], then)
    assert client.get(f"/api/ring/{first}", headers={auth.OWNER_HEADER: OWNER}).status_code == 200
    sent = []
    assert ring.fall_back([], lambda *x: sent.append(x) or True) == 1
    assert sent[0][2] == "/u2", "the second ring's alert goes, the fetched one's does not"


def test_no_tool_runs_on_a_rings_call_before_the_caller_speaks(client, monkeypatch):
    ask = asks.create("alpha", "public", "s1", "Bash", {"command": "ls"}, now=time.time())
    ring_id, key = ring.new_ring("alpha", [], time.time(), ask_ids=[ask["id"]])
    ring.answer(ring_id, key)
    opened = post(client, "/api/call/token", {"ring": ring_id}).json()
    sent = []
    monkeypatch.setattr(routes.chat, "send", lambda *a, **k: sent.append(a) or (True, ""))

    def tool(name, args):
        return post(client, f"/api/call/tool/{name}", {"call_id": opened["call_id"], "args": args}).json()["result"]

    held = {"done": False, "sent": False, "detail": livecall.UNHEARD}
    assert tool("answer_permission", {"ask": 1, "decision": "deny", "confirmed": False}) == held
    assert tool("ask_session", {"name": "alpha", "text": "go"}) == held
    assert tool("list_sessions", {}) == held
    assert asks.get(ask["id"])["status"] == "pending" and sent == []
    assert post(client, "/api/call/heard", {"call_id": opened["call_id"], "text": "deny it"}).status_code == 200
    result = tool("answer_permission", {"ask": 1, "decision": "deny", "confirmed": False})
    assert result["done"] is True and result["status"] == "deny"
