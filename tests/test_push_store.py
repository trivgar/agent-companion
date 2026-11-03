"""One store for every push: a PushStore in push_dir (default data_dir), or the
host app's own, set as agent_companion.server.push._store. And a guard of its own
for subscribing, so a host can stand its own check in for that write alone."""
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agent_companion import server, webpush  # noqa: E402
from agent_companion.server import auth, push  # noqa: E402

OWNER = "o" * 64
KEY = {"x-agent-companion-key": OWNER}
SAME = {"origin": "http://testserver", "host": "testserver"}
SUB = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "x", "auth": "y"}}


class Recorder(webpush.PushStore):
    """A host app's own store: it says what it was asked to send."""

    def __init__(self, home):
        super().__init__(home)
        self.sent = []

    def send(self, title, body, url="", tag="", **kw):
        self.sent.append((title, body, url, tag))
        return 1


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(server, "FEATURES", ["push"])
    monkeypatch.setattr(push, "_store", None)
    return server.create_app(token="t" * 32, owner_key=OWNER)


@pytest.fixture
def client(tmp_path, monkeypatch):
    return TestClient(_app(tmp_path, monkeypatch))


def test_the_store_is_in_data_dir_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AGENT_COMPANION_PUSH_DIR", raising=False)
    monkeypatch.setattr(push, "_store", None)
    assert webpush.push_dir() == tmp_path / "data"
    assert push.store().home == tmp_path / "data"


def test_push_dir_names_a_folder_of_its_own(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENT_COMPANION_PUSH_DIR", "~/keys")
    assert webpush.push_dir() == Path.home() / "keys"


def test_the_routes_keep_the_key_and_the_devices_in_push_dir(client, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_PUSH_DIR", str(tmp_path / "keys"))
    key = client.get("/api/push/key", headers=KEY).json()["key"]
    assert json.loads((tmp_path / "keys" / webpush.VAPID_NAME).read_text())["public"] == key
    r = client.post("/api/push/subscribe", json=SUB, headers={**SAME, **KEY})
    assert r.json() == {"ok": True, "devices": 1}
    assert [s["endpoint"] for s in webpush.PushStore(tmp_path / "keys").subscriptions()] == [SUB["endpoint"]]
    assert not (tmp_path / "data" / webpush.VAPID_NAME).exists(), "a second key was minted in data_dir"


def test_a_host_apps_store_serves_the_routes(client, tmp_path, monkeypatch):
    host = Recorder(tmp_path / "host")
    monkeypatch.setattr(push, "_store", host)
    assert client.get("/api/push/key", headers=KEY).json() == {"key": host.public_key(), "devices": 0}
    assert client.post("/api/push/subscribe", json=SUB, headers={**SAME, **KEY}).status_code == 200
    assert host.devices() == 1
    assert client.post("/api/push/test", json={}, headers={**SAME, **KEY}).json() == {"ok": True, "sent": 1}
    assert host.sent == [("Companion", "Notifications are on.", "/", "push-test")]


def test_every_push_the_core_sends_goes_through_that_store(tmp_path, monkeypatch):
    from agent_companion.server import connections, health, livecall, specs
    host = Recorder(tmp_path / "host")
    monkeypatch.setattr(push, "_store", host)
    assert server._send_clear_push("clear", "b", "/u", "t1")
    assert health._send("health", "b", "/u", "t2")
    assert specs._send("specs", "b", "/u", "t3")
    assert livecall._send_push("ring", "b", "/u", "t4", [])
    assert [s[0] for s in host.sent] == ["clear", "health", "specs", "ring"]
    assert connections._probe_push() == {"vapid": False, "devices": 0}
    host.public_key()
    assert connections._probe_push() == {"vapid": True, "devices": 0}


def test_a_needs_you_push_comes_from_push_dir(tmp_path, monkeypatch):
    from agent_companion import notify
    monkeypatch.setenv("AGENT_COMPANION_PUSH_DIR", str(tmp_path / "keys"))
    homes = []
    monkeypatch.setattr(webpush.PushStore, "send", lambda self, *a, **k: homes.append(self.home) or 1)
    notify.notify("Pick one", speaker="Ada")
    assert homes == [tmp_path / "keys"]


def test_subscribing_takes_the_write_rules_here(client):
    assert client.post("/api/push/subscribe", json=SUB, headers=SAME).status_code == 401
    plain = {**SAME, **KEY, "content-type": "text/plain"}
    assert client.post("/api/push/subscribe", content=json.dumps(SUB), headers=plain).status_code == 415
    foreign = {**KEY, "origin": "http://evil.example", "host": "testserver"}
    assert client.post("/api/push/subscribe", json=SUB, headers=foreign).status_code == 403


def test_subscribing_has_a_guard_of_its_own(tmp_path, monkeypatch):
    """A host app stands its own check in for subscribing alone: the test push
    still takes require_write."""
    app = _app(tmp_path, monkeypatch)
    app.dependency_overrides[auth.require_subscribe] = lambda: None
    c = TestClient(app)
    assert c.post("/api/push/subscribe", json=SUB).status_code == 200
    assert c.post("/api/push/test", json={}).status_code == 401
