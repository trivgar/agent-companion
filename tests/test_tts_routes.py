"""Coordinator HTTP routes. These use TestClient and are left to the route-test run."""
import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agent_companion import server  # noqa: E402
from agent_companion.server import auth  # noqa: E402

TOKEN = "t" * 32
OWNER = "o" * 64
KEY = {auth.OWNER_HEADER: OWNER}
SAME = {"origin": "http://testserver", "host": "testserver"}


def _coordinator(tmp_path):
    tts = tmp_path / "tts"
    tts.mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "audio-effects.cfg").write_text("Speaker|gain 8||0.7\n")
    return tts


def test_missing_coordinator_is_a_plain_setup_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_TTS_DIR", str(tmp_path / "missing"))
    monkeypatch.setattr(server, "FEATURES", ["comms"])
    client = TestClient(server.create_app(token=TOKEN, owner_key=OWNER))
    response = client.get("/api/tts", headers=KEY)
    assert response.status_code == 503 and "not set up" in response.json()["detail"]


def test_outbox_routes_take_and_ack_a_clip(tmp_path, monkeypatch):
    tts = _coordinator(tmp_path)
    outbox = tts / "outbox"
    outbox.mkdir()
    (outbox / ".seq").write_text("7\n")
    (outbox / "7.json").write_text(json.dumps({"seq": 7, "name": "Speaker", "at": 1, "duration": 1}))
    (outbox / "7.opus").write_bytes(b"OggS")
    monkeypatch.setenv("AGENT_COMPANION_TTS_DIR", str(tts))
    monkeypatch.setattr(server, "FEATURES", ["comms"])
    client = TestClient(server.create_app(token=TOKEN, owner_key=OWNER))
    assert client.get("/api/tts/outbox", headers=KEY).json()["clips"][0]["seq"] == 7
    assert client.get("/api/tts/clip/7?who=web", headers=KEY).status_code == 200
    assert client.post("/api/tts/played", json={"seq": 7, "result": "played"}, headers={**SAME, **KEY}).json() == {"ok": True}


def test_android_gets_refuse_requests_without_an_owner_key(tmp_path, monkeypatch):
    tts = _coordinator(tmp_path)
    outbox = tts / "outbox"
    outbox.mkdir()
    (outbox / "1.json").write_text(json.dumps({"seq": 1, "name": "Speaker", "at": 1, "duration": 1}))
    (outbox / "1.opus").write_bytes(b"OggS")
    monkeypatch.setenv("AGENT_COMPANION_TTS_DIR", str(tts))
    monkeypatch.setattr(server, "FEATURES", ["comms"])
    client = TestClient(server.create_app(token=TOKEN, owner_key=OWNER))
    assert client.get("/api/tts/outbox").status_code == 401
    assert client.get("/api/tts/clip/1?who=app").status_code == 401


def test_write_routes_and_mobile_registration_carry_owner_and_setup_guards(tmp_path, monkeypatch):
    tts = _coordinator(tmp_path)
    monkeypatch.setenv("AGENT_COMPANION_TTS_DIR", str(tts))
    monkeypatch.setattr(server, "FEATURES", ["comms"])
    client = TestClient(server.create_app(token=TOKEN, owner_key=OWNER))
    assert client.post("/api/tts/hold", json={"minutes": 5}, headers=SAME).status_code == 401
    assert client.post("/api/tts/hold", json={"minutes": 5}, headers={**SAME, **KEY}).status_code == 200
    response = client.post("/api/tts/app", json={"token": "token"}, headers={**SAME, **KEY})
    assert response.status_code == 503 and "fcm_service_account_file" in response.json()["detail"]


def test_android_build_route_uses_settings_not_a_fixed_path(tmp_path, monkeypatch):
    tts = _coordinator(tmp_path)
    apk = tmp_path / "build.apk"
    monkeypatch.setenv("AGENT_COMPANION_TTS_DIR", str(tts))
    monkeypatch.setenv("AGENT_COMPANION_APP_NAME", "Example App")
    monkeypatch.setenv("AGENT_COMPANION_ANDROID_APK_FILE", str(apk))
    monkeypatch.setattr(server, "FEATURES", ["comms"])
    client = TestClient(server.create_app(token=TOKEN, owner_key=OWNER))
    assert client.get("/app/exampleapp.apk", headers=KEY).status_code == 404
    apk.write_bytes(b"PK")
    assert client.get("/app/exampleapp.apk", headers=KEY).content == b"PK"


def test_a_phone_that_can_ring_is_recorded_as_the_live_calls_ringer(tmp_path, monkeypatch):
    # One registration route for the phone: comms owns it, the ring reads it.
    from agent_companion import fcm
    from agent_companion.server import comms as comms_routes
    tts = _coordinator(tmp_path)
    monkeypatch.setenv("AGENT_COMPANION_TTS_DIR", str(tts))
    monkeypatch.setattr(server, "FEATURES", ["comms"])
    monkeypatch.setattr(comms_routes.commsapp, "configured", lambda: True)
    monkeypatch.setattr(comms_routes.commsapp, "register", lambda tts, token, notify, ring: {"ok": True})
    saved = []
    monkeypatch.setattr(fcm, "save_device", lambda token, ring=False: saved.append((token, ring)) or True)
    client = TestClient(server.create_app(token=TOKEN, owner_key=OWNER))
    assert client.post("/api/tts/app", json={"token": "abc", "ring": True}, headers=SAME).status_code == 401
    assert saved == []
    assert client.post("/api/tts/app", json={"token": "abc", "ring": True}, headers={**SAME, **KEY}).json() == {"ok": True}
    assert saved == [("abc", True)]
    assert client.post("/api/tts/app", json={"token": "def"}, headers={**SAME, **KEY}).json() == {"ok": True}
    assert saved == [("abc", True)], "a phone that cannot ring is not a ringer"


def _client(tts, monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_TTS_DIR", str(tts))
    monkeypatch.setattr(server, "FEATURES", ["comms"])
    return TestClient(server.create_app(token=TOKEN, owner_key=OWNER))


def test_the_state_is_the_sound_pages_shape_with_the_hold_and_the_app(tmp_path, monkeypatch):
    body = _client(_coordinator(tmp_path), monkeypatch).get("/api/tts", headers=KEY).json()
    assert set(body) == {"speakers", "tracks", "presets", "voices", "music_enabled", "stop_available", "quiet",
                         "health", "held", "app"}
    assert body["quiet"]["hold"] == {"until": None, "waiting": 0} and body["held"] is False


def test_the_buttons_answer_in_the_sound_pages_words(tmp_path, monkeypatch):
    tts = _coordinator(tmp_path)
    (tts / "volume.conf").write_text("voice_gain=3\neffects.Speaker=\n")
    client, write = _client(tts, monkeypatch), {**SAME, **KEY}
    level = client.post("/api/tts/level", json={"key": "voice_gain", "value": 5}, headers=write).json()
    assert level == {"voice_gain": 5, "music_volume": 0.7, "speakers": {}}
    assert (tts / "volume.conf").read_text() == "voice_gain=5\neffects.Speaker=\n"
    assert client.post("/api/tts/level", json={"key": "voice_gain", "value": 99}, headers=write).status_code == 400
    assert client.post("/api/tts/release", json={}, headers=write).json() == {
        "released": 0, "detail": "release is unavailable: no executable tts/quiet.sh"}
    assert client.post("/api/tts/test", json={}, headers=write).json() == {
        "spoken": False, "detail": "test is unavailable: no executable tts/quiet.sh"}
    assert client.post("/api/tts/stop", json={}, headers=write).json() == {
        "stopped": False, "queue_cleared": False, "detail": "stop is unavailable: no executable tts/stop.sh"}


def test_the_transcript_offers_a_replay_only_for_a_spoken_line(tmp_path, monkeypatch):
    import time
    tts, stamp = _coordinator(tmp_path), "2026-11-02 10:00:00"
    words = "The tests pass and the build is green."
    (tts / "speak.log").write_text(f"{stamp} SPOKE Speaker 3.0s :: Speaker here, on the shop. {words}\n"
                                   f"{stamp} MUTED(Speaker) :: {words}\n")
    (tts / "outbox").mkdir()
    (tts / "outbox" / "7.json").write_text(json.dumps(
        {"seq": 7, "name": "Speaker", "text": words, "at": time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))}))
    muted, spoke = _client(tts, monkeypatch).get("/api/tts/transcript", headers=KEY).json()["clips"]
    assert (spoke["state"], spoke["body"], spoke["seq"]) == ("spoke", words, 7)
    assert muted["state"] == "muted" and "seq" not in muted
