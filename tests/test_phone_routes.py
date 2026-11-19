"""The optional phone relay's HTTP boundary and opt-in gates."""
from __future__ import annotations

from fastapi.testclient import TestClient

from agent_companion import server
from agent_companion.phone import drive
from agent_companion.server import auth

TOKEN = "t" * 32
OWNER = "o" * 64
DEVICE = "emu-0123456789abcdef"
WRITE = {auth.HEADER_NAME: TOKEN, "content-type": "application/json"}

PHONE_PATHS = (
    "/api/phone/jobs", "/api/phone/jobs/x/cmd", "/api/phone/jobs/x/end",
    "/api/phone/device", "/api/phone/devices/x/approve", "/api/phone/devices/x/forget",
    "/api/phone/next", "/api/phone/result", "/api/phone/event",
    "/api/phone/jobs/x", "/api/phone/jobs/x/shot", "/api/phone/jobs/x/stop",
)


def test_every_phone_route_is_absent_while_driver_is_off(monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_PHONE_DRIVER", "false")
    app = server.create_app(token=TOKEN, owner_key=OWNER)
    client = TestClient(app)
    for path in PHONE_PATHS:
        assert client.get(path, headers={auth.OWNER_HEADER: OWNER}).status_code == 404
        assert client.post(path, json={}, headers=WRITE).status_code == 404


def test_real_device_cannot_pair_or_receive_a_job_until_separately_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_COMPANION_PHONE_DRIVER", "true")
    monkeypatch.setenv("AGENT_COMPANION_PHONE_DRIVER_REAL_PHONE", "false")
    app = server.create_app(token=TOKEN, owner_key=OWNER)
    client = TestClient(app)
    body = {"device_id": "phone-0123456789abcdef", "kind": "phone", "model": "test-device",
            "android": "15", "fcm": "token", "driver_on": True, "app_version": "1"}
    paired = client.post("/api/phone/device", json=body, headers=WRITE)
    assert paired.status_code == 403
    assert "real phone driver is off" in paired.json()["detail"]
    started = client.post("/api/phone/jobs", json={"device": "phone", "task": "read status",
                                                    "agent": "phone-agent"}, headers=WRITE)
    assert started.status_code == 403


def test_emulator_registration_uses_the_write_token_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_COMPANION_PHONE_DRIVER", "true")
    monkeypatch.setenv("AGENT_COMPANION_PHONE_DRIVER_REAL_PHONE", "false")
    from agent_companion.server import phone
    relay = drive.Relay(tmp_path / "phone", key_path=tmp_path / "agent.key")
    monkeypatch.setattr(phone, "relay", relay)
    client = TestClient(server.create_app(token=TOKEN, owner_key=OWNER))
    body = {"device_id": DEVICE, "kind": "emulator", "model": "test-emulator", "android": "15",
            "fcm": "token", "driver_on": True, "app_version": "1-emu"}
    assert client.post("/api/phone/device", json=body).status_code == 401
    result = client.post("/api/phone/device", json=body, headers=WRITE)
    assert result.status_code == 200
    assert result.json()["role"] == "emulator"


def test_phone_feature_and_dashboard_module_follow_the_opt_in(monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_PHONE_DRIVER", "false")
    off = server.create_app(token=TOKEN, owner_key=OWNER)
    assert "phone" not in [row["id"] for row in off.state.features]
    monkeypatch.setenv("AGENT_COMPANION_PHONE_DRIVER", "true")
    on = server.create_app(token=TOKEN, owner_key=OWNER)
    assert "phone" in [row["id"] for row in on.state.features]
    script = (server.STATIC / "phone.js").read_text()
    assert "CC.register('phone'" in script and "data-stop" in script
