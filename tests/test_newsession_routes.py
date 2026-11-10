"""New session's routes: the owner's key, a configured project's key, and a
log line that keeps the first message's length, never its words."""
from __future__ import annotations

import json
import subprocess

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agent_companion import newsession as ns  # noqa: E402
from agent_companion import server  # noqa: E402
from agent_companion.server import auth, chat  # noqa: E402
from agent_companion.server import newsession as routes  # noqa: E402

TOKEN = "t" * 32
OWNER = "o" * 64
KEY = {"x-agent-companion-key": OWNER}
SAME = {"origin": "http://testserver", "host": "testserver"}
WRITE = {**KEY, **SAME}
CP = subprocess.CompletedProcess


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def snapshot(self):
        return self.rows


@pytest.fixture
def site(tmp_path, monkeypatch):
    folder = tmp_path / "site"
    folder.mkdir()
    config = tmp_path / "config.toml"
    config.write_text('[new_session]\ndefault_model = "sonnet"\n\n'
                      f'[[projects]]\nname = "Site"\npath = "{folder}"\nkey = "site"\nprefix = "claude-site"\n\n'
                      f'[[projects]]\nname = "Gone"\npath = "{tmp_path / "gone"}"\nkey = "gone"\n')
    monkeypatch.setenv("AGENT_COMPANION_CONFIG", str(config))
    for key in ("PROJECTS", "NEW_SESSION"):
        monkeypatch.delenv("AGENT_COMPANION_" + key, raising=False)
    monkeypatch.setattr(chat, "log_path", lambda: tmp_path / "chat.jsonl")
    monkeypatch.setattr(routes, "_reader", Rows([
        {"pid": 21, "speaker": "Ada", "cwd": str(folder.resolve()), "child": False},
        {"pid": 22, "speaker": "Helper", "cwd": str(folder.resolve()), "child": True}]))
    return folder


@pytest.fixture
def client(site, monkeypatch):
    monkeypatch.setattr(server, "FEATURES", [])
    return TestClient(server.create_app(token=TOKEN, owner_key=OWNER))


def _log(site):
    f = site.parent / "chat.jsonl"
    return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []


def test_the_projects_route_lists_keys_labels_and_who_is_there_but_never_a_path(client, site):
    assert client.get("/api/newsession/projects").status_code == 401
    assert client.get("/api/newsession/projects", headers={"x-agent-companion-token": TOKEN}).status_code == 401
    r = client.get("/api/newsession/projects", headers=KEY)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == {"ok": True, "default_model": "sonnet", "launchers": [], "projects": [
        {"key": "site", "label": "Site", "sessions": [{"speaker": "Ada", "pid": 21}], "note": "Ada is in Site",
         "available": True, "reason": ""},
        {"key": "gone", "label": "Gone", "sessions": [], "note": "", "available": False,
         "reason": "Folder does not exist"}]}
    assert str(site.parent) not in r.text


def test_without_a_host_reader_the_listing_reads_the_sessions_views(client, site, monkeypatch):
    monkeypatch.setattr(routes, "_reader", None)
    monkeypatch.setattr(routes.sessions, "_parts", lambda: {"reader": Rows([
        {"pid": 31, "speaker": "Lin", "cwd": str(site.resolve()), "child": False}])})
    got = client.get("/api/newsession/projects", headers=KEY).json()["projects"][0]
    assert got["note"] == "Lin is in Site"


def test_start_refuses_an_unknown_key_or_a_path_and_logs_it(client, site, monkeypatch):
    launched = []
    monkeypatch.setattr(ns, "launch", lambda *a, **k: launched.append(a) or {})
    for bad in ("nope", str(site), "Site", None):
        r = client.post("/api/newsession", json={"project": bad}, headers=WRITE)
        assert r.status_code == 400 and r.json() == {"ok": False, "detail": "not a configured project"}, bad
    assert not launched
    log = _log(site)
    assert len(log) == 4 and not any(e["ok"] for e in log) and {e["project"] for e in log} == {"unknown"}


def test_start_refuses_a_message_over_4000(client, site, monkeypatch):
    monkeypatch.setattr(ns, "launch", lambda *a, **k: pytest.fail("launched"))
    r = client.post("/api/newsession", json={"project": "site", "message": "x" * 4001}, headers=WRITE)
    assert r.status_code == 400 and r.json()["detail"] == "the first message is over 4,000 characters"
    e = _log(site)[-1]
    assert e["message_len"] == 4001 and "xxxx" not in json.dumps(e)


def test_start_needs_the_owners_key_and_this_page(client, site, monkeypatch):
    monkeypatch.setattr(ns, "launch", lambda *a, **k: pytest.fail("launched"))
    assert client.post("/api/newsession", json={"project": "site"}, headers=SAME).status_code == 401
    foreign = {**KEY, "origin": "http://other.example", "host": "testserver"}
    assert client.post("/api/newsession", json={"project": "site"}, headers=foreign).status_code == 403
    assert client.post("/api/newsession", content="project=site",
                       headers={**WRITE, "content-type": "application/x-www-form-urlencoded"}).status_code == 415


def test_the_origin_check_holds_when_a_host_stands_in_for_the_write_guard(site, monkeypatch):
    monkeypatch.setattr(server, "FEATURES", [])
    monkeypatch.setattr(ns, "launch", lambda *a, **k: pytest.fail("launched"))
    app = server.create_app(token=TOKEN, owner_key=OWNER)
    app.dependency_overrides[auth.require_write] = lambda: None
    r = TestClient(app).post("/api/newsession", json={"project": "site"}, headers=KEY)
    assert r.status_code == 403


def test_start_launches_and_logs_the_length_not_the_words(client, site, monkeypatch):
    got = []

    def fake(proj, message):
        got.append((proj["key"], message))
        return {"ok": True, "status": 200, "tmux": "claude-site", "pid": 77, "detail": "", "log": "started"}
    monkeypatch.setattr(ns, "launch", fake)
    r = client.post("/api/newsession", json={"project": "site", "message": "ship the fix"}, headers=WRITE)
    assert r.status_code == 200 and r.json() == {"ok": True, "tmux": "claude-site", "pid": 77, "detail": ""}
    assert got == [("site", "ship the fix")]
    e = _log(site)[-1]
    assert (e["ok"], e["project"], e["tmux"], e["message_len"], e["who"]) == (True, "site", "claude-site", 12, "owner")
    assert "ship the fix" not in json.dumps(e)


def test_the_log_names_the_person_an_add_on_says_is_asking(client, site, monkeypatch):
    monkeypatch.setattr(routes.addons, "person", lambda request: {"id": "ada"})
    monkeypatch.setattr(ns, "launch", lambda proj, message: {"ok": True, "status": 200, "tmux": "claude-site",
                                                             "pid": 5, "detail": "", "log": "started"})
    assert client.post("/api/newsession", json={"project": "site"}, headers=WRITE).status_code == 200
    assert _log(site)[-1]["who"] == "ada"


def test_start_with_no_claude_is_a_500_in_words(client, site, monkeypatch):
    calls = []

    def run(argv, **kw):
        argv = [str(a) for a in argv]
        calls.append(argv)
        if "display-message" in argv:
            return CP(argv, 0, "/dev/pts/7\n", "")
        if "capture-pane" in argv:
            return CP(argv, 0, "bash: claude: command not found\n", "")
        return CP(argv, 0, "", "")
    t = [0.0]
    monkeypatch.setattr(ns, "RUN", run)
    monkeypatch.setattr(ns, "SLEEP", lambda s: t.__setitem__(0, t[0] + s))
    monkeypatch.setattr(ns, "CLOCK", lambda: t[0])
    monkeypatch.setattr(ns, "find_claude", lambda cwd, tty: None)
    monkeypatch.setattr(ns.shutil, "which", lambda n: f"/usr/bin/{n}")
    r = client.post("/api/newsession", json={"project": "site", "message": "secret plan"}, headers=WRITE)
    assert r.status_code == 500 and r.json()["ok"] is False and r.json()["tmux"] == "claude-site"
    assert r.json()["detail"].startswith("No Claude started in claude-site within 12 seconds.")
    assert any("new-session" in c for c in calls)
    e = _log(site)[-1]
    assert e["ok"] is False and e["detail"] == "no claude process within 12 s"
    assert "secret plan" not in json.dumps(e) and "command not found" not in json.dumps(e)


def _with_launcher(site, monkeypatch):
    cfg = site.parent / "config.toml"
    cfg.write_text(cfg.read_text().replace(
        '[new_session]\ndefault_model = "sonnet"\n',
        '[new_session]\ndefault_model = "sonnet"\n'
        '[[new_session.launchers]]\nkey = "grok"\nlabel = "Grok 4.7"\n'
        'program = "/home/me/bin/claude-other"\nmodel = "x-ai/grok-4.7"\n'))


def test_launchers_are_listed_by_key_and_label_never_the_program(client, site, monkeypatch):
    _with_launcher(site, monkeypatch)
    r = client.get("/api/newsession/projects", headers=KEY)
    assert r.json()["launchers"] == [{"key": "grok", "label": "Grok 4.7"}]
    assert "claude-other" not in r.text


def test_a_start_with_a_launcher_passes_its_row_and_logs_its_key(client, site, monkeypatch):
    _with_launcher(site, monkeypatch)
    got = []

    def fake(proj, message, launcher=None):
        got.append(launcher)
        return {"ok": True, "status": 200, "tmux": "claude-site", "pid": 9, "detail": "", "log": "started"}
    monkeypatch.setattr(ns, "launch", fake)
    assert client.post("/api/newsession", json={"project": "site", "launcher": "grok"},
                       headers=WRITE).status_code == 200
    assert got[0]["program"] == "/home/me/bin/claude-other" and got[0]["model"] == "x-ai/grok-4.7"
    e = _log(site)[-1]
    assert e["launcher"] == "grok" and e["text"] == "[new session: site, grok]"
    # "" is plain claude
    assert client.post("/api/newsession", json={"project": "site", "launcher": ""},
                       headers=WRITE).status_code == 200
    assert got[-1] is None


@pytest.mark.parametrize("bad", ["nope", "/bin/sh", 3, ["grok"]])
def test_an_unknown_launcher_starts_nothing(client, site, monkeypatch, bad):
    _with_launcher(site, monkeypatch)
    monkeypatch.setattr(ns, "launch", lambda *a, **k: pytest.fail("launched"))
    r = client.post("/api/newsession", json={"project": "site", "launcher": bad}, headers=WRITE)
    assert r.status_code == 400 and r.json()["detail"] == "not a configured launcher"
