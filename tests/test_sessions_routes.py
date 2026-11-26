import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agent_companion import server  # noqa: E402
from agent_companion.reader import WAITING_DIR  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t" * 32
OWNER = "o" * 64
KEY = {"x-agent-companion-key": OWNER}
SAME = {"origin": "http://testserver", "host": "testserver"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "examples"))
    import demo_home
    home = demo_home.build(tmp_path)
    monkeypatch.setenv("AGENT_COMPANION_CLAUDE_HOME", str(home))
    monkeypatch.setenv("AGENT_COMPANION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(server, "FEATURES", ["sessions", "push"])
    yield home, TestClient(server.create_app(token=TOKEN, owner_key=OWNER))
    for f in (home / "sessions").glob("*.json"):
        try:
            os.kill(json.loads(f.read_text())["pid"], signal.SIGTERM)
        except (OSError, ValueError, KeyError):
            pass


def test_features_list_sessions_and_push(env):
    _, c = env
    assert [f["id"] for f in c.get("/api/features").json()] == ["sessions", "push"]
    assert [f["title"] for f in c.get("/api/features").json()] == ["Sessions", "Push"]


def test_sessions_rows_come_from_the_temp_home(env):
    _, c = env
    rows = c.get("/api/sessions", headers=KEY).json()
    assert {r["name"] for r in rows} == {"api-1", "web-checkout", "docs", "infra"}
    r = next(r for r in rows if r["name"] == "web-checkout")
    for k in ("project", "model", "cost_usd", "context_pct", "last_reply", "status", "waiting"):
        assert k in r
    assert r["waiting"] is False
    assert "Fixed" in r["last_reply"]


def test_sessions_rows_include_card_suggestions(env, monkeypatch):
    from agent_companion.server import chat

    def targets(rows, sessions_dir):
        return {row["name"].lower(): {"name": row["name"], "pid": row["pid"], "pane": "%1",
                                        "status": row["status"], "waiting": row["waiting"]} for row in rows}

    monkeypatch.setattr(chat, "targets", targets)
    monkeypatch.setattr(chat.addons, "chat_target", lambda target: True)
    monkeypatch.setattr(chat, "suggestion", lambda target: "Review the status" if target["status"] == "idle" else None)
    monkeypatch.setattr(chat, "seen", lambda: {})
    monkeypatch.setattr(chat, "unread", lambda row, seen_at: row["name"] == "web-checkout")
    _, c = env
    rows = c.get("/api/sessions", headers=KEY).json()
    idle = next(row for row in rows if row["status"] == "idle")
    assert isinstance(idle["pid"], int) and idle["session_id"]
    assert idle["suggestion"] == "Review the status"
    assert next(row for row in rows if row["name"] == "web-checkout")["unread"] is True


def test_sessions_rows_include_background_jobs(env, monkeypatch):
    from agent_companion.server import sessions

    def annotate(rows):
        for row in rows:
            row["bg_job_list"] = [{"cmd": "pytest -q", "age_s": 120}] if row["name"] == "api-1" else []
            row["bg_jobs"] = len(row["bg_job_list"])

    monkeypatch.setattr(sessions.bgjobs, "annotate", annotate)
    _, c = env
    rows = c.get("/api/sessions", headers=KEY).json()
    row = next(item for item in rows if item["name"] == "api-1")
    assert row["bg_jobs"] == 1
    assert row["bg_job_list"] == [{"cmd": "pytest -q", "age_s": 120}]


def test_a_waiting_marker_shows_as_waiting(env):
    home, c = env
    pid = json.loads(next((home / "sessions").glob("*.json")).read_text())["pid"]
    d = home / WAITING_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / str(pid)).write_text("")
    future = time.time() + 5
    os.utime(d / str(pid), (future, future))
    rows = c.get("/api/sessions", headers=KEY).json()
    assert sum(r["waiting"] for r in rows) == 1


def test_usage_has_both_windows_and_the_limits_line(env):
    _, c = env
    u = c.get("/api/usage", headers=KEY).json()
    assert u["five_hour"]["cost_usd"] >= 0 and u["seven_day"]["cost_usd"] > 0
    assert u["limits"]["available"] is False   # no credentials in the temp home


def test_push_key_and_subscribe_need_the_write_rules(env):
    _, c = env
    key = c.get("/api/push/key", headers=KEY).json()["key"]
    assert len(key) > 40
    sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "x", "auth": "y"}}
    assert c.post("/api/push/subscribe", json=sub, headers=SAME).status_code == 401
    assert c.post("/api/push/subscribe", json=sub, headers={**KEY, "origin": "http://evil.example", "host": "testserver"}).status_code == 403
    r = c.post("/api/push/subscribe", json=sub, headers={**SAME, **KEY})
    assert r.status_code == 200 and r.json()["devices"] == 1
    assert c.post("/api/push/subscribe", json={"nope": 1}, headers={**SAME, **KEY}).status_code == 400
    assert c.get("/api/push/key", headers=KEY).json()["devices"] == 1


def test_push_test_sends_through_the_store(env, monkeypatch):
    _, c = env
    from agent_companion import webpush
    sent = []
    monkeypatch.setattr(webpush.PushStore, "send", lambda self, title, body, **kw: sent.append((title, body)) or 1)
    assert c.post("/api/push/test", json={}, headers=SAME).status_code == 401
    r = c.post("/api/push/test", json={}, headers={**SAME, **KEY})
    assert r.status_code == 200 and r.json()["sent"] == 1 and sent


def test_reads_need_the_key(env):
    _, c = env
    for path in ("/api/sessions", "/api/usage", "/api/push/key"):
        assert c.get(path).status_code == 401, path


def test_a_pending_ask_rides_on_its_sessions_row_and_on_no_other(env):
    from agent_companion.server import asks

    _, c = env
    rows = c.get("/api/sessions", headers=KEY).json()
    assert all("ask" not in r for r in rows), "nothing is pending yet"
    target = next(r for r in rows if r["name"] == "web-checkout")
    made = asks.create("", "web", target["session_id"], "Bash", {"command": "npm run migrate"})
    rows = c.get("/api/sessions", headers=KEY).json()
    mine = next(r for r in rows if r["name"] == "web-checkout")
    assert mine["ask"]["id"] == made["id"]
    assert mine["ask"]["tool"] == "Bash" and "npm run migrate" in mine["ask"]["what"]
    assert [r["name"] for r in rows if "ask" in r] == ["web-checkout"]


def test_the_sessions_route_needs_the_key_so_an_ask_is_never_read_without_it(env):
    _, c = env
    assert c.get("/api/sessions").status_code in (401, 403)

