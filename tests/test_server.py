import threading

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import APIRouter, Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent_companion import server  # noqa: E402
from agent_companion.server import auth  # noqa: E402

TOKEN = "t" * 32


def _app(monkeypatch, features=("sessions",)):
    monkeypatch.setattr(server, "FEATURES", list(features))
    return server.create_app(token=TOKEN)


def test_a_missing_feature_is_skipped_and_the_page_still_serves(monkeypatch):
    app = _app(monkeypatch, features=("no_such_feature",))
    c = TestClient(app)
    assert c.get("/api/features").json() == []
    assert c.get("/").status_code == 200
    assert c.get("/static/app.js").status_code == 200


OWNER = "o" * 64
KEY = {auth.OWNER_HEADER: OWNER}


def _write_app():
    app = server.create_app(token=TOKEN, owner_key=OWNER)
    r = APIRouter()

    @r.post("/api/x", dependencies=[Depends(auth.require_write)])
    def x():
        return {"ok": True}
    app.include_router(r)
    return TestClient(app)


def test_a_write_needs_json_the_owner_key_and_our_own_origin(monkeypatch):
    monkeypatch.setattr(server, "FEATURES", [])
    c = _write_app()
    same = {"origin": "http://testserver", "host": "testserver"}
    assert c.post("/api/x", content="a=1", headers={**same, **KEY, "content-type": "application/x-www-form-urlencoded"}
                  ).status_code == 415
    assert c.post("/api/x", json={}, headers=same).status_code == 401
    assert c.post("/api/x", json={}, headers={**same, auth.OWNER_HEADER: "wrong"}).status_code == 401
    assert c.post("/api/x", json={}, headers={**same, auth.HEADER_NAME: TOKEN}).status_code == 401, "hook token refused"
    assert c.post("/api/x", json={}, headers=same, params={"k": OWNER}).status_code == 401, "no key in a URL"
    assert c.post("/api/x", json={}, headers={**KEY, "origin": "http://evil.example", "host": "testserver"}
                  ).status_code == 403
    assert c.post("/api/x", json={}, headers={**KEY, "sec-fetch-site": "same-origin"}).status_code == 403
    assert c.post("/api/x", json={}, headers={**KEY, "host": "testserver"}).status_code == 403, "no Origin at all"
    assert c.post("/api/x", json={}, headers={**KEY, **same}).json() == {"ok": True}


def test_a_retired_owner_header_is_not_accepted(monkeypatch):
    monkeypatch.setattr(server, "FEATURES", [])
    c = _write_app()
    same = {"origin": "http://testserver", "host": "testserver"}

    assert c.post("/api/x", json={}, headers={**same, "x-claude-companion-key": OWNER}).status_code == 401


def test_behind_the_proxy_the_forwarded_host_is_the_page(monkeypatch):
    monkeypatch.setattr(server, "FEATURES", [])
    c = _write_app()
    h = {**KEY, "origin": "https://box.example", "host": "127.0.0.1:8770", "x-forwarded-host": "box.example"}
    assert c.post("/api/x", json={}, headers=h).status_code == 200
    assert c.post("/api/x", json={}, headers={**h, "origin": "https://other.example"}).status_code == 403


def test_create_app_starts_no_threads_under_pytest(monkeypatch):
    # Other tests' threads may come and go; only threads started by create_app count.
    before = set(threading.enumerate())
    _app(monkeypatch, features=list(server.FEATURES))
    started = [t for t in threading.enumerate() if t not in before and t.is_alive()]
    assert started == []


def test_the_token_is_made_once_and_private(tmp_path):
    p = tmp_path / "tok"
    a = auth.load_or_create_token(p)
    assert a and auth.load_or_create_token(p) == a
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_a_pairing_code_never_reaches_the_access_log():
    assert auth.redact_key("GET /api/mail?k=secret&x=1") == "GET /api/mail?k=REDACTED&x=1"
    assert auth.redact_key("GET /pair?code=ABCD2345") == "GET /pair?code=REDACTED"
