import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agent_companion import clearline, server  # noqa: E402
from agent_companion.server import sessions  # noqa: E402

TOKEN = "t" * 32
OWNER = "o" * 64
KEY = {"x-agent-companion-key": OWNER}
WRITE = {**KEY, "origin": "http://testserver", "host": "testserver"}


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("")
    monkeypatch.setenv("AGENT_COMPANION_CONFIG", str(path))
    monkeypatch.setenv("AGENT_COMPANION_OWNER_KEYS", str(tmp_path / "owner-keys.json"))
    monkeypatch.delenv("AGENT_COMPANION_CLEAR_LINE", raising=False)


def test_get_and_put_use_the_config_default_and_enforce_the_range(tmp_path, monkeypatch):
    path = tmp_path / "line.json"
    config = tmp_path / "config.toml"
    config.write_text("")
    monkeypatch.setenv("AGENT_COMPANION_CLEARLINE", str(path))
    monkeypatch.setenv("AGENT_COMPANION_CONFIG", str(config))
    monkeypatch.delenv("AGENT_COMPANION_CLEAR_LINE", raising=False)
    assert clearline.get() == clearline.DEFAULT
    config.write_text("clear_line = 600000\n")
    assert clearline.get() == 600_000
    assert clearline.put(650_000) == 650_000
    assert clearline.get() == 650_000
    assert not path.with_name(path.name + ".tmp").exists()
    for bad in (49_999, 2_000_001, "400000", 400_000.0, True, None):
        with pytest.raises(ValueError):
            clearline.put(bad)
    path.write_text(json.dumps({"tokens": 3}))
    assert clearline.get() == clearline.DEFAULT


def test_over_skips_children_and_rows_without_context():
    assert clearline.over({"context_used": 400_000}, 400_000)
    assert not clearline.over({"context_used": 399_999}, 400_000)
    assert not clearline.over({"context_used": 900_000, "child": True}, 400_000)
    assert not clearline.over({}, 400_000)
    assert not clearline.over({"context_used": None}, 400_000)
    assert not clearline.over({"context_used": True}, 400_000)


def test_the_over_file_is_a_setting(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    monkeypatch.setenv("AGENT_COMPANION_CONFIG", str(config))
    monkeypatch.setenv("AGENT_COMPANION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AGENT_COMPANION_CLEAR_OVER", raising=False)
    config.write_text("")
    assert clearline.over_file() == tmp_path / "data" / "needs-clear.json"
    config.write_text('clear_over_file = "~/hooks/needs-clear.json"\n')
    assert clearline.over_file() == Path.home() / "hooks" / "needs-clear.json"
    monkeypatch.setenv("AGENT_COMPANION_CLEAR_OVER", str(tmp_path / "env.json"))
    assert clearline.over_file() == tmp_path / "env.json"


def test_watcher_pushes_once_per_crossing_and_writes_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_CLEARLINE", str(tmp_path / "line.json"))
    rows = [{"session_id": "abcdef1234", "speaker": "Session", "project": "sample",
             "context_used": 447_000}]
    sent = []
    state = tmp_path / "over.json"
    watcher = clearline.Watcher(lambda: [dict(row) for row in rows],
                                lambda *args: sent.append(args) or True, path=state)
    assert watcher.tick() == ["abcdef1234"]
    assert sent == [("Needs a /clear", "Session (sample) is at 447k: needs a /clear", "/?agent=Session",
                     "clear-abcdef12")]
    saved = clearline.read_over(state)["over"]["abcdef1234"]
    assert saved["speaker"] == "Session" and saved["project"] == "sample"
    assert saved["used"] == 447_000 and saved["pushed"] is True and saved["since"]
    assert watcher.tick() == [] and len(sent) == 1
    rows[0]["context_used"] = 20_000
    assert watcher.tick() == [] and clearline.read_over(state)["over"] == {}
    rows[0]["context_used"] = 410_000
    assert watcher.tick() == ["abcdef1234"] and len(sent) == 2


def test_watcher_retries_a_failed_push(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_CLEARLINE", str(tmp_path / "line.json"))
    sent = [False]
    watcher = clearline.Watcher(lambda: [{"session_id": "s1", "context_used": 500_000}],
                                lambda *args: sent[0], path=tmp_path / "over.json")
    assert watcher.tick() == []
    sent[0] = True
    assert watcher.tick() == ["s1"]


def test_watcher_waits_while_the_owner_sleeps_and_taps_through(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_CLEARLINE", str(tmp_path / "line.json"))
    rows = [{"session_id": "abcdef1234", "speaker": "Night Owl", "project": "sample",
             "context_used": 447_000}]
    sent, sleeping = [], [True]
    state = tmp_path / "over.json"
    watcher = clearline.Watcher(lambda: [dict(row) for row in rows],
                                lambda *args: sent.append(args) or True, path=state,
                                asleep=lambda: sleeping[0])
    assert watcher.tick() == [] and sent == []                 # asleep: it waits, unsent
    assert "abcdef1234" in clearline.read_over(state)["over"]  # and the flag is still written
    sleeping[0] = False
    assert watcher.tick() == ["abcdef1234"]
    assert sent[0][2] == "/?agent=Night%20Owl"
    assert watcher.tick() == [] and len(sent) == 1


def test_an_asleep_check_that_fails_counts_as_awake(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_CLEARLINE", str(tmp_path / "line.json"))

    def broken():
        raise RuntimeError("no idea")
    sent = []
    watcher = clearline.Watcher(lambda: [{"session_id": "s1", "context_used": 500_000}],
                                lambda *args: sent.append(args) or True, path=tmp_path / "over.json",
                                asleep=broken)
    assert watcher.tick() == ["s1"] and sent[0][2] == "/"


def test_watcher_refuses_to_start_under_pytest():
    watcher = clearline.Watcher(lambda: [], lambda *args: True)
    watcher.start()
    assert watcher.thread is None


def test_routes_protect_the_line_and_add_session_flags(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text("")
    monkeypatch.setenv("AGENT_COMPANION_CONFIG", str(config))
    monkeypatch.setenv("AGENT_COMPANION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENT_COMPANION_CLEARLINE", str(tmp_path / "line.json"))
    monkeypatch.setattr(server, "FEATURES", ["sessions"])
    parts = {"reader": type("Reader", (), {"snapshot": lambda self: [
        {"pid": 1, "session_id": "over", "project": "sample", "cwd": "", "context_used": 400_000,
         "child": False},
        {"pid": 2, "session_id": "child", "project": "sample", "cwd": "", "context_used": 500_000,
         "child": True},
    ]})(), "git": object()}
    monkeypatch.setattr(sessions, "_parts", lambda: parts)
    client = TestClient(server.create_app(token=TOKEN, owner_key=OWNER))
    assert client.get("/api/clearline").status_code == 401
    assert client.get("/api/clearline", headers=KEY).json() == {"tokens": 400_000}
    rows = {row["session_id"]: row for row in client.get("/api/sessions", headers=KEY).json()}
    assert rows["over"]["clear_line"] == 400_000 and rows["over"]["needs_clear"] is True
    assert rows["child"]["needs_clear"] is False
    assert client.post("/api/clearline", json={"tokens": 600_000}, headers=KEY).status_code == 403
    for tokens in (49_999, 2_000_001, "400000", True):
        assert client.post("/api/clearline", json={"tokens": tokens}, headers=WRITE).status_code == 400
    assert client.post("/api/clearline", json={"tokens": 600_000}, headers=WRITE).json() == {"tokens": 600_000}
    rows = {row["session_id"]: row for row in client.get("/api/sessions", headers=KEY).json()}
    assert rows["over"]["clear_line"] == 600_000 and rows["over"]["needs_clear"] is False


def test_the_line_has_a_router_of_its_own_without_the_sessions_view():
    from agent_companion.server import clearline as routes
    assert {(m, r.path) for r in routes.router.routes for m in r.methods} == {
        ("GET", "/api/clearline"), ("POST", "/api/clearline")}
    assert not [r for r in sessions.router.routes if r.path == "/api/clearline"]


def test_the_line_is_never_cached_and_a_failed_save_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENT_COMPANION_CLEARLINE", str(tmp_path / "line.json"))
    client = TestClient(server.create_app(token=TOKEN, owner_key=OWNER))
    got = client.get("/api/clearline", headers=KEY)
    assert got.json() == {"tokens": 400_000} and got.headers["cache-control"] == "no-store"
    saved = client.post("/api/clearline", json={"tokens": 600_000}, headers=WRITE)
    assert saved.json() == {"tokens": 600_000} and saved.headers["cache-control"] == "no-store"

    def full(tokens):
        raise OSError("disk full")
    monkeypatch.setattr(clearline, "put", full)
    failed = client.post("/api/clearline", json={"tokens": 700_000}, headers=WRITE)
    assert failed.status_code == 500
    assert failed.json() == {"tokens": 600_000, "detail": "could not save it: disk full"}
