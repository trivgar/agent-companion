"""Files: every checkout like a repository view, editable (agent_companion.files)."""
from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from agent_companion import files

KEYS = ("CHECKOUT_DIRS", "CHECKOUT_SKIP", "CHECKOUT_PREFER", "PROJECTS", "FILES_LOG", "FILES_AUTHOR_NAME",
        "FILES_AUTHOR_EMAIL", "DATA_DIR")


def git(d, *a):
    return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=True).stdout


def _settings(tmp_path, monkeypatch, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setenv("AGENT_COMPANION_CONFIG", str(path))
    for key in KEYS:
        monkeypatch.delenv("AGENT_COMPANION_" + key, raising=False)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    work = tmp_path / "work"
    r = work / "site"
    r.mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.name", "Repo Person")
    (r / "README.md").write_text("# Site\n\nHello.\n")
    (r / "app").mkdir()
    (r / "app" / "x.py").write_text("a = 1\nb = 2\n")
    (r / "win.txt").write_bytes(b"one\r\ntwo\r\n")
    (r / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (r / "blob.dat").write_bytes(b"a\0b")
    (r / ".gitignore").write_text(".env\nnode_modules/\n")
    (r / ".env").write_text("SECRET=1\n")
    git(r, "add", "-A")
    git(r, "-c", "user.email=first@example.com", "commit", "-qm", "first")
    git(r, "worktree", "add", "-q", "-b", "feat", str(work / "worktrees" / "site-feat"))
    _settings(tmp_path, monkeypatch,
              f'checkout_dirs = ["{work}", "{work / "worktrees"}"]\nfiles_log = "{tmp_path / "files.jsonl"}"\n'
              'files_author_name = "Ada"\nfiles_author_email = "ada@example.com"\n')
    return r


def test_a_repo_with_its_worktrees_as_branches(repo):
    got = files.repos()
    assert got == [{"repo": "site", "checkouts": [{"name": "site", "branch": "main"},
                                                  {"name": "site-feat", "branch": "feat"}]}]


def test_the_repo_page_lists_folders_files_and_the_readme(repo):
    t = files.tree("site")
    assert t["ok"] and t["dirs"] == ["app"] and "README.md" in t["files"] and ".env" not in t["files"]
    assert t["readme"] == "README.md" and t["readme_text"].startswith("# Site") and t["commit"]["subject"] == "first"
    assert t["branch"] == "main" and t["count"] == 6
    assert files.tree("site", "app")["files"] == ["x.py"]
    assert not files.tree("site", "nope")["ok"] and not files.tree("nobody")["ok"]
    assert files.all_paths("site")["paths"] == sorted(files.all_paths("site")["paths"])


def test_only_what_git_lists_can_be_read(repo):
    assert files.read("site", "app/x.py")["text"] == "a = 1\nb = 2\n"
    for bad in (".env", "../site/.env", "/etc/passwd", "app/../.env", ".git/config", ""):
        assert not files.read("site", bad)["ok"], bad


def test_a_symlink_anywhere_on_the_way_is_refused(repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "y.py").write_text("secret\n")
    (repo / "linked").symlink_to(outside, target_is_directory=True)
    (repo / "direct.txt").symlink_to(outside / "y.py")
    files._lists.clear()
    assert "direct.txt" in files.listing(repo, fresh=True)          # git lists the link itself
    assert not files.read("site", "direct.txt")["ok"] and files.raw("site", "direct.txt") is None
    assert not files.read("site", "linked/y.py")["ok"]


def test_generated_folders_are_never_listed(repo):
    (repo / "web").mkdir()
    (repo / "web" / "node_modules").symlink_to(repo / "app", target_is_directory=True)
    assert "web/node_modules" not in files.listing(repo, fresh=True)
    assert not files.read("site", "web/node_modules")["ok"]


def test_images_and_binaries_are_listed_and_never_editable(repo):
    img, blob = files.read("site", "logo.png"), files.read("site", "blob.dat")
    assert img["ok"] and img["image"] and not img["editable"]
    assert blob["ok"] and blob["binary"] and not blob["editable"] and "image" not in blob
    assert files.raw("site", "logo.png")[1] == "image/png" and files.raw("site", "blob.dat") is None


def test_a_clean_file_is_committed_as_the_author_setting_and_not_pushed(repo, tmp_path):
    d = files.read("site", "app/x.py")
    r = files.save("site", "app/x.py", "a = 1\nb = 3\n", d["sha"], "Fix b")
    assert r["ok"] and r["committed"] and r["commit"]["subject"] == "Fix b" and r["status_before"] == "clean"
    assert git(repo, "log", "-1", "--format=%an <%ae>|%cn|%s") == "Ada <ada@example.com>|Ada|Fix b\n"
    assert git(repo, "status", "--porcelain") == ""
    entry = json.loads((tmp_path / "files.jsonl").read_text().splitlines()[-1])
    assert (entry["checkout"], entry["path"], entry["committed"], entry["before"]) == ("site", "app/x.py", True, "clean")


def test_a_repositorys_own_email_wins(repo):
    git(repo, "config", "user.email", "own@example.com")
    d = files.read("site", "app/x.py")
    assert files.save("site", "app/x.py", "a = 2\n", d["sha"])["committed"]
    assert git(repo, "log", "-1", "--format=%an <%ae>") == "Ada <own@example.com>\n"


def test_no_author_setting_is_gits_own_identity(repo, tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, f'checkout_dirs = ["{repo.parent}"]\nfiles_log = "{tmp_path / "f.jsonl"}"\n')
    git(repo, "config", "user.email", "repo@example.com")
    d = files.read("site", "app/x.py")
    assert files.save("site", "app/x.py", "a = 3\n", d["sha"])["committed"]
    assert git(repo, "log", "-1", "--format=%an <%ae>|%s") == "Repo Person <repo@example.com>|Update x.py\n"


def test_an_agents_uncommitted_work_is_saved_not_committed(repo):
    (repo / "app" / "x.py").write_text("a = 1\nb = 2\nc = 3\n")          # the agent's edit
    (repo / "README.md").write_text("# Site\n\nStaged by the agent.\n")
    git(repo, "add", "README.md")
    d = files.read("site", "app/x.py")
    assert d["status"] == "modified"
    r = files.save("site", "app/x.py", "a = 1\nb = 2\nc = 4\n", d["sha"])
    assert r["ok"] and not r["committed"] and "uncommitted" in r["not_committed"]
    assert git(repo, "log", "-1", "--format=%s") == "first\n"
    assert (repo / "app" / "x.py").read_text().endswith("c = 4\n")


def test_a_new_file_is_saved_not_committed(repo):
    (repo / "new.txt").write_text("fresh\n")
    files._lists.clear()
    d = files.read("site", "new.txt")
    assert d["status"] == "untracked"
    r = files.save("site", "new.txt", "fresher\n", d["sha"])
    assert r["ok"] and not r["committed"] and "new file" in r["not_committed"]


def test_a_commit_leaves_what_the_agent_staged_alone(repo):
    (repo / "README.md").write_text("# Site\n\nStaged by the agent.\n")
    git(repo, "add", "README.md")
    d = files.read("site", "app/x.py")
    assert files.save("site", "app/x.py", "a = 9\nb = 2\n", d["sha"])["committed"]
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["app/x.py"]
    assert git(repo, "diff", "--cached", "--name-only").split() == ["README.md"]


def test_changed_since_opened_is_refused_with_the_file_as_it_is(repo):
    d = files.read("site", "app/x.py")
    (repo / "app" / "x.py").write_text("agent wrote this\n")
    r = files.save("site", "app/x.py", "mine\n", d["sha"])
    assert not r["ok"] and r["conflict"] and r["current"]["text"] == "agent wrote this\n"
    assert (repo / "app" / "x.py").read_text() == "agent wrote this\n"


def test_a_write_during_git_status_is_refused_too(repo, monkeypatch):
    d = files.read("site", "app/x.py")
    real = files._status

    def status_while_an_agent_writes(path, rel):
        got = real(path, rel)
        (repo / "app" / "x.py").write_text("landed meanwhile\n")
        return got
    monkeypatch.setattr(files, "_status", status_while_an_agent_writes)
    r = files.save("site", "app/x.py", "mine\n", d["sha"])
    assert not r["ok"] and r["conflict"] and r["current"]["text"] == "landed meanwhile\n"
    assert (repo / "app" / "x.py").read_text() == "landed meanwhile\n"


def test_an_unchanged_save_writes_nothing(repo):
    d = files.read("site", "app/x.py")
    assert files.save("site", "app/x.py", d["text"], d["sha"]) == {"ok": True, "unchanged": True, "committed": False}


def test_crlf_stays_crlf_and_the_mode_stays(repo):
    (repo / "win.txt").chmod(0o640)
    d = files.read("site", "win.txt")
    assert d["crlf"]
    files.save("site", "win.txt", "one\ntwo\nthree\n", d["sha"])
    assert (repo / "win.txt").read_bytes() == b"one\r\ntwo\r\nthree\r\n"
    assert (repo / "win.txt").stat().st_mode & 0o777 == 0o640
    assert not [p.name for p in repo.iterdir() if p.name.startswith(".win.txt.")], "no temporary left"


def test_a_binary_file_cannot_be_saved(repo):
    sha = hashlib.sha256((repo / "blob.dat").read_bytes()).hexdigest()
    assert files.save("site", "blob.dat", "text\n", sha) == {"ok": False, "detail": "This file cannot be edited here."}


def test_a_worktree_is_its_own_branch(repo):
    wt = repo.parent / "worktrees" / "site-feat"
    d = files.read("site-feat", "app/x.py")
    assert files.save("site-feat", "app/x.py", "on feat\n", d["sha"])["committed"]
    assert git(wt, "log", "-1", "--format=%s") == "Update x.py\n"
    assert git(repo, "log", "-1", "--format=%s") == "first\n"


def test_checkout_of_and_relative(repo):
    assert files.checkout_of(str(repo / "app")) == "site"
    assert files.checkout_of(str(repo.parent / "worktrees" / "site-feat" / "app")) == "site-feat"
    assert files.relative("site", str(repo / "app" / "x.py")) == "app/x.py"
    assert files.relative("site", "/etc/passwd") is None


def test_git_runs_without_optional_locks(repo, monkeypatch):
    seen = []
    real = subprocess.run

    def run(argv, **kw):
        seen.append((argv, kw.get("env") or {}))
        return real(argv, **kw)
    monkeypatch.setattr(files.subprocess, "run", run)
    files.read("site", "app/x.py")
    assert seen and all(env.get("GIT_OPTIONAL_LOCKS") == "0" and "core.fsmonitor=false" in argv for argv, env in seen)


def test_the_log_is_a_setting(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, f'data_dir = "{tmp_path / "data"}"\n')
    assert files.log_path() == tmp_path / "data" / "files.jsonl"
    _settings(tmp_path, monkeypatch, f'files_log = "{tmp_path / "log.jsonl"}"\n')
    assert files.log_path() == tmp_path / "log.jsonl"
