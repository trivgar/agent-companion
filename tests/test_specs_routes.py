"""Specs routes: the owner's key, ids, and who is live in each repository."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent_companion import server  # noqa: E402
from agent_companion.server import auth, chat  # noqa: E402
from agent_companion.server import specs as routes  # noqa: E402

TOKEN = "t" * 32
OWNER = "o" * 64
KEY = {"x-agent-companion-key": OWNER}


def _spec(root, kind, name, text):
    d = root / "docs" / "superpowers" / kind
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text)


@pytest.fixture
def client(tmp_path, monkeypatch):
    work = tmp_path / "work"
    site = work / "site"
    (site / ".git").mkdir(parents=True)
    _spec(site, "specs", "2026-10-01-a-design.md", "# Alpha design\n")
    wt = work / "worktrees" / "site-branch"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {site}/.git/worktrees/site-branch\n")
    _spec(wt, "plans", "2026-10-02-b.md", "# Beta plan\n")
    config = tmp_path / "config.toml"
    config.write_text(f'checkout_dirs = ["{work}", "{work / "worktrees"}"]\n')
    monkeypatch.setenv("AGENT_COMPANION_CONFIG", str(config))
    for key in ("CHECKOUT_DIRS", "CHECKOUT_SKIP", "CHECKOUT_PREFER", "SPEC_FOLDERS", "PROJECTS"):
        monkeypatch.delenv("AGENT_COMPANION_" + key, raising=False)
    monkeypatch.setattr(chat, "known", lambda: {"ada": {"name": "Ada", "cwd": str(wt)},
                                                "kit": {"name": "Kit", "cwd": str(tmp_path)}})
    monkeypatch.setattr(server, "FEATURES", ["specs"])
    return TestClient(server.create_app(token=TOKEN, owner_key=OWNER))


def test_the_list_needs_the_owners_key_and_says_who_is_live(client):
    assert client.get("/api/specs").status_code == 401
    r = client.get("/api/specs", headers=KEY)
    d = r.json()
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store" and d["ok"]
    assert [x["id"] for x in d["docs"]] == ["site-branch/plans/2026-10-02-b.md", "site/specs/2026-10-01-a-design.md"]
    assert {x["repo"] for x in d["docs"]} == {"site"}
    assert d["agents"] == {"site": ["Ada"]}, "a session in a worktree counts for its repository"


def test_a_document_by_its_id_names_the_agents_of_its_repository(client):
    r = client.get("/api/specs/doc", params={"id": "site/specs/2026-10-01-a-design.md"}, headers=KEY)
    d = r.json()
    assert r.headers["cache-control"] == "no-store"
    assert d["ok"] and d["text"] == "# Alpha design\n" and d["title"] == "Alpha design" and d["agents"] == ["Ada"]
    for bad in ("site/specs/../../config.toml", "../../etc/passwd", "nobody/specs/x.md", ""):
        r = client.get("/api/specs/doc", params={"id": bad}, headers=KEY)
        assert r.status_code == 200 and r.json() == {"ok": False, "detail": "No such document."}, bad


def test_the_old_address_is_gone(client):
    assert client.get("/api/specs/file", params={"project": "site", "path": "x"}, headers=KEY).status_code == 404


def test_both_routes_use_the_owner_read_guard():
    got = {r.path: {d.call for d in r.dependant.dependencies} for r in routes.router.routes if isinstance(r, APIRoute)}
    assert got == {"/api/specs": {auth.require_token}, "/api/specs/doc": {auth.require_token}}


def test_the_spec_push_never_starts_under_tests(monkeypatch):
    started = []
    monkeypatch.setattr(routes.specs.Watcher, "start", lambda self: started.append(self))
    routes.start(None)
    assert started == []
