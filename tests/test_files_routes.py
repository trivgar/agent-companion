"""Files routes: the owner's key, a checkout's name, and a save from this page only."""
from __future__ import annotations

import subprocess

import pytest

pytest.importorskip("fastapi")
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent_companion import server  # noqa: E402
from agent_companion.server import auth, chat  # noqa: E402
from agent_companion.server import files as routes  # noqa: E402

TOKEN = "t" * 32
OWNER = "o" * 64
KEY = {"x-agent-companion-key": OWNER}
SAME = {"origin": "http://testserver", "host": "testserver"}
WRITE = {**KEY, **SAME}


def git(d, *a):
    return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    work = tmp_path / "work"
    r = work / "site"
    (r / "app").mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.name", "Repo Person")
    git(r, "config", "user.email", "repo@example.com")
    (r / "app" / "x.py").write_text("a = 1\n")
    (r / "logo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    git(r, "add", "-A")
    git(r, "commit", "-qm", "first")
    config = tmp_path / "config.toml"
    config.write_text(f'checkout_dirs = ["{work}"]\nfiles_log = "{tmp_path / "files.jsonl"}"\n')
    monkeypatch.setenv("AGENT_COMPANION_CONFIG", str(config))
    for key in ("CHECKOUT_DIRS", "CHECKOUT_SKIP", "CHECKOUT_PREFER", "PROJECTS", "FILES_LOG"):
        monkeypatch.delenv("AGENT_COMPANION_" + key, raising=False)
    monkeypatch.setattr(chat, "known", lambda: {"ada": {"name": "Ada", "cwd": str(r / "app")},
                                                "kit": {"name": "Kit", "cwd": str(tmp_path)}})
    return r


@pytest.fixture
def client(repo, monkeypatch):
    monkeypatch.setattr(server, "FEATURES", ["files"])
    return TestClient(server.create_app(token=TOKEN, owner_key=OWNER))


def test_the_repositories_need_the_owners_key_and_say_who_works_where(client):
    assert client.get("/api/files").status_code == 401
    r = client.get("/api/files", headers=KEY)
    assert r.headers["cache-control"] == "no-store"
    assert r.json() == {"ok": True, "repos": [{"repo": "site", "checkouts": [{"name": "site", "branch": "main"}]}],
                        "agents": {"site": ["Ada"]}}


def test_tree_paths_and_file_answer_by_checkout_and_say_a_miss_in_words(client):
    t = client.get("/api/files/tree", params={"checkout": "site", "path": "app"}, headers=KEY).json()
    assert t["ok"] and t["files"] == ["x.py"]
    assert client.get("/api/files/paths", params={"checkout": "site"}, headers=KEY).json()["paths"] == [
        "app/x.py", "logo.svg"]
    f = client.get("/api/files/file", params={"checkout": "site", "path": "app/x.py"}, headers=KEY).json()
    assert f["ok"] and f["text"] == "a = 1\n" and f["editable"] and f["sha"]
    for route, params in (("tree", {"checkout": "nobody"}), ("paths", {"checkout": "nobody"}),
                          ("file", {"checkout": "site", "path": "../config.toml"})):
        r = client.get(f"/api/files/{route}", params=params, headers=KEY)
        assert r.status_code == 200 and r.json()["ok"] is False and r.json()["detail"], route


def test_an_image_is_served_sandboxed(client):
    r = client.get("/api/files/raw", params={"checkout": "site", "path": "logo.svg"}, headers=KEY)
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert "sandbox" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/files/raw", params={"checkout": "site", "path": "app/x.py"}, headers=KEY).status_code == 404
    assert client.get("/api/files/raw", params={"checkout": "site", "path": "logo.svg"}).status_code == 401


def test_where_a_path_in_an_agents_chat_lives(client, repo):
    def where(**params):
        return client.get("/api/files/where", params=params, headers=KEY).json()
    assert where(path=str(repo / "app" / "x.py")) == {"ok": True, "checkout": "site", "path": "app/x.py"}
    assert where(agent="Ada", path="x.py") == {"ok": True, "checkout": "site", "path": "app/x.py"}
    assert where(agent="Ada", path="logo.svg") == {"ok": True, "checkout": "site", "path": "logo.svg"}, \
        "a path from the repository's root, wherever the agent stands"
    assert where(agent="Kit", path="x.py")["ok"] is False and where(path="/etc/passwd")["ok"] is False


def test_a_save_needs_the_key_json_strings_and_this_page(client, repo):
    f = client.get("/api/files/file", params={"checkout": "site", "path": "app/x.py"}, headers=KEY).json()
    body = {"checkout": "site", "path": "app/x.py", "text": "a = 2\n", "expect": f["sha"], "message": "Two"}
    assert client.post("/api/files/save", json=body, headers=SAME).status_code == 401
    assert client.post("/api/files/save", json=body, headers=KEY).status_code == 403
    assert client.post("/api/files/save", json=body, headers={**KEY, "origin": "http://elsewhere.example"}).status_code == 403
    assert client.post("/api/files/save", json={**body, "expect": 5}, headers=WRITE).status_code == 400
    assert (repo / "app" / "x.py").read_text() == "a = 1\n"
    r = client.post("/api/files/save", json=body, headers=WRITE)
    assert r.status_code == 200 and r.json()["committed"] and git(repo, "log", "-1", "--format=%s") == "Two\n"


def test_a_conflict_is_a_200_with_the_file_as_it_is(client, repo):
    f = client.get("/api/files/file", params={"checkout": "site", "path": "app/x.py"}, headers=KEY).json()
    (repo / "app" / "x.py").write_text("agent wrote this\n")
    r = client.post("/api/files/save", json={"checkout": "site", "path": "app/x.py", "text": "mine\n",
                                             "expect": f["sha"]}, headers=WRITE)
    d = r.json()
    assert r.status_code == 200 and d["ok"] is False and d["conflict"] and d["current"]["text"] == "agent wrote this\n"


def test_reads_use_the_owner_read_guard_and_the_save_the_write_guard():
    got = {(r.path, tuple(sorted(r.methods))): {d.call for d in r.dependant.dependencies}
           for r in routes.router.routes if isinstance(r, APIRoute)}
    reads = {"/api/files", "/api/files/tree", "/api/files/paths", "/api/files/file", "/api/files/raw", "/api/files/where"}
    assert {p for (p, m) in got if m == ("GET",)} == reads
    assert all(got[(p, ("GET",))] == {auth.require_token} for p in reads)
    assert got[("/api/files/save", ("POST",))] == {auth.require_write}
