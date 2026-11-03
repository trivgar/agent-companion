import subprocess

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agent_companion import server  # noqa: E402
from agent_companion.server import connections  # noqa: E402

TOKEN = "t" * 32
OWNER = "o" * 64
H = {"x-agent-companion-key": OWNER}
W = {**H, "content-type": "application/json", "origin": "http://testserver"}

GOOD = {
    "config": {"path": "/x/config.toml", "exists": True, "problem": ""},
    "claude": {"found": True, "version": "2.1.0"},
    "sessions": {"exists": True},
    "tailscale": {"installed": True, "serving": True},
    "push": {"vapid": True, "devices": 2},
    "devices": {"count": 1},
    "mail": {"source": "gmail", "known": True, "accounts": 1, "last_run": {"ok": True, "at": "2026-10-29 07:00:01"}},
    "comms": {"piper": True, "voices": 3, "held": False},
    "voice_server": {"state": "up"},
    "collab": {"configured": True, "gh": True},
    "money": {"registry": True, "source": "csv", "known": True},
    "fitness": {"registry": True, "source": "csv", "known": True},
}


def by_id(rows):
    return {r["id"]: r for r in rows}


def test_all_good_is_all_ok():
    rows = connections.rows(GOOD)
    assert rows and all(r["state"] == "ok" for r in rows)
    assert all(set(r) == {"id", "name", "state", "detail", "fix"} for r in rows)
    assert all(r["fix"] == "" for r in rows)


@pytest.mark.parametrize("key,patch,id_,state", [
    ("config", {"problem": "/x/config.toml is not valid TOML (Cannot overwrite a value); every setting is at its default"}, "config", "broken"),
    ("config", {"exists": False}, "config", "off"),
    ("claude", {"found": False}, "claude", "broken"),
    ("sessions", {"exists": False}, "sessions", "broken"),
    ("tailscale", {"installed": False}, "tailscale", "off"),
    ("tailscale", {"serving": False}, "tailscale", "off"),
    ("push", {"vapid": False, "devices": 0}, "push", "off"),
    ("push", {"devices": 0}, "push", "off"),
    ("devices", {"count": 0}, "devices", "off"),
    ("mail", {"source": None, "known": False}, "mail", "off"),
    ("mail", {"source": "nope", "known": False}, "mail", "broken"),
    ("mail", {"last_run": {"ok": False, "at": "x", "note": "rc=1"}}, "mail", "broken"),
    ("mail", {"last_run": None}, "mail", "ok"),
    ("comms", {"piper": False}, "comms", "off"),
    ("comms", {"voices": 0}, "comms", "off"),
    ("comms", {"held": True}, "comms", "ok"),
    ("voice_server", {"state": "down"}, "voice_server", "off"),
    ("voice_server", {"state": "refused"}, "voice_server", "broken"),
    ("collab", {"configured": False}, "collab", "off"),
    ("collab", {"gh": False}, "collab", "broken"),
    ("money", {"source": None, "known": False}, "money", "off"),
    ("money", {"source": "zzz", "known": False}, "money", "broken"),
    ("fitness", {"source": None, "known": False}, "fitness", "off"),
    ("fitness", {"source": "zzz", "known": False}, "fitness", "broken"),
])
def test_state_combinations(key, patch, id_, state):
    p = {k: dict(v) for k, v in GOOD.items()}
    p[key].update(patch)
    r = by_id(connections.rows(p))[id_]
    assert r["state"] == state
    assert (r["fix"] != "") == (state != "ok")
    assert r["detail"]


def test_held_comms_says_so():
    p = {k: dict(v) for k, v in GOOD.items()}
    p["comms"]["held"] = True
    assert "hold" in by_id(connections.rows(p))["comms"]["detail"].lower()


def test_config_probe_reports_duplicate_key(tmp_path, monkeypatch):
    settings = tmp_path / "config.toml"
    settings.write_text("a = 1\na = 2\n")
    monkeypatch.setenv("AGENT_COMPANION_CONFIG", str(settings))
    probe = connections._probe_config()
    assert probe["exists"] is True
    assert "not valid TOML" in probe["problem"]
    settings.write_text("a = 1\n")
    assert connections._probe_config()["problem"] == ""


def test_config_row_comes_first():
    assert connections.rows(GOOD)[0]["id"] == "config"


def test_absent_money_and_fitness_registry_are_not_rows():
    p = {k: v for k, v in GOOD.items() if k not in ("money", "fitness")}
    ids = {r["id"] for r in connections.rows(p)}
    assert "money" not in ids and "fitness" not in ids and "claude" in ids


def test_a_failed_probe_is_a_broken_row_not_a_crash():
    p = dict(GOOD)
    p["claude"] = {"error": "boom"}
    r = by_id(connections.rows(p))["claude"]
    assert r["state"] == "broken" and r["fix"]


def test_no_em_dashes_in_copy():
    import inspect
    assert "—" not in inspect.getsource(connections)
    assert "—" not in (connections.STATIC / "connections.js").read_text()


@pytest.fixture
def client(monkeypatch):
    connections.clear_cache()
    monkeypatch.setattr(connections, "INJECT", None)
    orig = server.FEATURES
    server.FEATURES = ["connections"]
    app = server.create_app(token=TOKEN, owner_key=OWNER)
    server.FEATURES = orig
    yield TestClient(app)
    connections.clear_cache()


def test_probes_never_run_under_pytest_without_injection(client, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a probe ran a process")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    r = client.get("/api/connections", headers=H)
    assert r.status_code == 200 and r.json() == []


def test_routes_need_the_owner_key(client):
    assert client.get("/api/connections").status_code == 401
    assert client.get("/api/connections", headers={"x-agent-companion-token": TOKEN}).status_code == 401
    assert client.post("/api/connections/recheck", headers={"content-type": "application/json",
                                                           "origin": "http://testserver"}, json={}).status_code == 401
    assert client.post("/api/connections/recheck", headers=H).status_code == 415


def test_route_serves_injected_rows_and_caches_until_recheck(client, monkeypatch):
    calls = []
    def inject():
        calls.append(1)
        return GOOD
    monkeypatch.setattr(connections, "INJECT", inject)
    assert len(client.get("/api/connections", headers=H).json()) == len(connections.rows(GOOD))
    client.get("/api/connections", headers=H)
    assert len(calls) == 1
    assert client.post("/api/connections/recheck", headers=W, json={}).status_code == 200
    client.get("/api/connections", headers=H)
    assert len(calls) == 2


def test_feature_is_listed(client):
    assert {"id": "connections", "title": "Connections"} in client.get("/api/features").json()
