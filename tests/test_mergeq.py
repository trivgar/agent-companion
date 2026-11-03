import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agent_companion import mergeq

SUITE = "if [ -e BAD ]; then echo '1 failed'; exit 1; fi; echo '5 passed in 0.01s'"
ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def sh(cwd, *args):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env={**os.environ, **ENV})
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


FAKE_GH = """#!PYEXE
import json, os, subprocess, sys, tempfile
a = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write(json.dumps(a) + "\\n")
if a[:2] == ["auth", "status"]:
    sys.exit(1 if os.environ.get("FAKE_GH_UNAUTHED") else 0)
st = os.environ["FAKE_LOG"] + ".prs"
prs = json.load(open(st)) if os.path.exists(st) else {}
for k, v in json.loads(os.environ.get("FAKE_EXISTING_PRS", "{}")).items():
    prs.setdefault(k, v)          # what GitHub did since (a merge) wins over how the test set it up
origin = os.environ["FAKE_ORIGIN"]
env = dict(os.environ, GIT_AUTHOR_NAME="gh", GIT_AUTHOR_EMAIL="gh@example.com",
           GIT_COMMITTER_NAME="gh", GIT_COMMITTER_EMAIL="gh@example.com")
def oid(br):
    r = subprocess.run(["git", "-C", origin, "rev-parse", "refs/heads/" + br], capture_output=True, text=True)
    return r.stdout.strip()
def info(n):
    v = prs[n]
    return v if isinstance(v, dict) else {"branch": v}
def head_oid(br):
    o = os.environ.get("FAKE_HEAD_OVERRIDE", "")
    brx, _, val = o.partition("=")
    return val if brx == br else oid(br)
def opt(name):
    return a[a.index(name) + 1]
def is_open(n, br, base=None):
    p = info(n)
    return p["branch"] == br and p.get("state", "OPEN") == "OPEN" and base in (None, p.get("base", "main"))
def once(flag):
    # an outside hand lands one commit on main, the first time only
    if not os.environ.get(flag) or os.path.exists(st + "." + flag):
        return
    open(st + "." + flag, "w").close()
    d = tempfile.mkdtemp()
    for c in (["clone", "-q", origin, d], ["-C", d, "commit", "-q", "--allow-empty", "-m", "outside " + flag],
              ["-C", d, "push", "-q", "origin", "HEAD:main"]):
        subprocess.run(["git"] + c, env=env, capture_output=True, check=True)
if a[:2] == ["pr", "create"]:
    br, base = opt("--head"), opt("--base")
    if os.environ.get("FAKE_FAIL_CREATE") == br:
        sys.stderr.write("create exploded\\n")
        sys.exit(1)
    if any(is_open(n, br, base) for n in prs):
        sys.stderr.write('a pull request for branch "%s" into branch "%s" already exists\\n' % (br, base))
        sys.exit(1)
    n = max([int(x) for x in prs] + [0]) + 1
    prs[str(n)] = {"branch": br, "base": base}
    json.dump(prs, open(st, "w"))
    print("https://github.com/o/r/pull/%d" % n)
if a[:2] == ["pr", "list"]:
    br = opt("--head")
    print(json.dumps([{"number": int(n), "headRefOid": head_oid(br), "baseRefName": info(n).get("base", "main"),
                       "headRepositoryOwner": {"login": "o"}, "isCrossRepository": False}
                      for n in prs if is_open(n, br, opt("--base") if "--base" in a else None)]))
if a[:2] == ["pr", "view"]:
    p = info(a[2])
    state = "MERGED" if os.environ.get("FAKE_MERGED_VIEW") == a[2] else p.get("state", "OPEN")
    print(json.dumps({"headRefName": p["branch"], "headRefOid": head_oid(p["branch"]),
                      "isCrossRepository": p.get("isCrossRepository", False), "state": state,
                      "title": p.get("title", "queued change"), "baseRefName": p.get("base", "main"),
                      "mergeCommit": {"oid": p["merged"]} if p.get("merged") else None}))
if a[:2] == ["pr", "checks"]:
    once("FAKE_LAND_DURING_CHECKS")
    if os.environ.get("FAKE_RETARGET_DURING_CHECKS"):
        prs[a[2]] = dict(info(a[2]), base=os.environ["FAKE_RETARGET_DURING_CHECKS"])
        json.dump(prs, open(st, "w"))
    if os.environ.get("FAKE_CHECKS_FAIL") == a[2]:
        print("pytest (3.11)\\tfail\\t1m\\thttps://example.com")
        sys.exit(1)
    if os.environ.get("FAKE_NO_REQUIRED"):
        sys.stderr.write("no required checks reported on the 'x' branch\\n")
        sys.exit(1)
if a[:2] == ["pr", "merge"]:
    br = info(a[2])["branch"]
    if "--match-head-commit" in a and opt("--match-head-commit") != head_oid(br):
        sys.stderr.write("head moved\\n")
        sys.exit(1)
    if os.environ.get("FAKE_MERGE_QUEUE"):
        sys.exit(0)               # GitHub's own merge queue took it: accepted, not merged
    once("FAKE_LAND_BEFORE_MERGE")
    d = tempfile.mkdtemp()
    for c in (["clone", "-q", origin, d], ["-C", d, "merge", "-q", "--no-ff", "-m", "pr", "origin/" + br],
              ["-C", d, "push", "-q", "origin", "HEAD:" + os.environ.get("FAKE_MERGE_INTO", "main")]):
        if subprocess.run(["git"] + c, env=env, capture_output=True).returncode:
            sys.exit(1)
    merged = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    prs[a[2]] = dict(info(a[2]), state="MERGED", merged=merged)
    json.dump(prs, open(st, "w"))
    if os.environ.get("FAKE_BRANCH_MOVES_ON_MERGE") == br:
        for c in (["-C", d, "checkout", "-q", "origin/" + br], ["-C", d, "commit", "-q", "--allow-empty", "-m", "more"],
                  ["-C", d, "push", "-q", "origin", "HEAD:refs/heads/" + br]):
            subprocess.run(["git"] + c, env=env, capture_output=True, check=True)
    if os.environ.get("FAKE_MERGE_EXIT_1") == br:
        sys.stderr.write("merged, but cleanup failed\\n")
        sys.exit(1)
"""


@pytest.fixture
def world(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(origin))
    main = tmp_path / "main"
    sh(tmp_path, "git", "clone", "-q", str(origin), str(main))
    sh(main, "git", "checkout", "-q", "-B", "main")
    (main / "base.txt").write_text("base\n")
    sh(main, "git", "add", "base.txt")
    sh(main, "git", "commit", "-q", "-m", "base")
    sh(main, "git", "push", "-q", "origin", "main")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "gh.jsonl"
    gh = bindir / "gh"
    gh.write_text(FAKE_GH.replace("PYEXE", sys.executable))
    sc = bindir / "systemctl"
    sc.write_text(f"#!/bin/sh\necho \"$@\" >> {tmp_path}/systemctl.log\n")
    for f in (gh, sc):
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_ORIGIN", str(origin))
    monkeypatch.setenv("AGENT_COMPANION_MERGEQ_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("FAKE_GH_UNAUTHED", raising=False)
    monkeypatch.delenv("FAKE_EXISTING_PRS", raising=False)

    class W:
        pass
    w = W()
    w.tmp, w.origin, w.main, w.log = tmp_path, origin, main, log
    w.store = mergeq.Store()
    w.said = []
    w.notes = []

    def branch(name, files):
        wt = tmp_path / ("wt-" + name.replace("/", "-"))
        sh(main, "git", "worktree", "add", "-q", "-b", name, str(wt), "main")
        for f, text in files.items():
            (wt / f).write_text(text)
            sh(wt, "git", "add", f)
        sh(wt, "git", "commit", "-q", "-m", "work on " + name)
        return wt, sh(wt, "git", "rev-parse", "HEAD")

    def queue(name, files, summary=""):
        wt, sha = branch(name, files)
        w.store.add(name, sha, summary)
        return wt, sha

    def existing_pr(number, name, files, state="OPEN", is_cross_repository=False, title="queued change",
                    base="main"):
        wt, sha = branch(name, files)
        sh(wt, "git", "push", "-q", "origin", f"HEAD:refs/heads/{name}")
        prs = json.loads(os.environ.get("FAKE_EXISTING_PRS", "{}"))
        prs[str(number)] = {"branch": name, "state": state, "base": base,
                            "isCrossRepository": is_cross_repository, "title": title}
        monkeypatch.setenv("FAKE_EXISTING_PRS", json.dumps(prs))
        return wt, sha

    def go(**kw):
        return mergeq.run(repo=main, base=tmp_path / "state", suite=SUITE, say=w.said.append,
                          notify=lambda *a: w.notes.append(a), **kw)

    def calls(*prefix):
        if not log.exists():
            return []
        return [c for c in map(json.loads, log.read_text().splitlines()) if c[:len(prefix)] == list(prefix)]

    def origin_has(path, ref="main"):
        r = subprocess.run(["git", "-C", str(origin), "cat-file", "-e", f"{ref}:{path}"], capture_output=True)
        return r.returncode == 0

    w.branch, w.queue, w.existing_pr, w.go, w.calls, w.origin_has = branch, queue, existing_pr, go, calls, origin_has
    return w


def test_green_batch_opens_and_merges_prs(world):
    world.queue("feat/a", {"a.txt": "a\n"}, "adds a")
    world.queue("feat/b", {"b.txt": "b\n"}, "adds b")
    assert world.go() == 0
    assert world.origin_has("a.txt") and world.origin_has("b.txt")
    creates = world.calls("pr", "create")
    assert len(creates) == 2
    body = creates[0][creates[0].index("--body") + 1]
    assert "adds a" in body and "Tests: 5 passed on the integration run" in body
    assert "--fill" not in creates[0] and creates[0][creates[0].index("--title") + 1] == "adds a"
    merge = world.calls("pr", "merge")[0]
    assert merge[:2] == ["pr", "merge"] and "--merge" in merge and "--delete-branch" not in merge
    assert "--match-head-commit" in merge
    assert world.store.queue() == []
    assert [o["state"] for o in world.store.last()["outcomes"]] == ["merged", "merged"]
    # the clean main checkout followed, and the server was restarted
    assert (world.main / "a.txt").exists()
    assert "restart agent-companion" in (world.tmp / "systemctl.log").read_text()
    assert [o["branch"] for o in world.store.history()] == ["feat/a", "feat/b"]


def test_red_batch_bisects_and_lands_the_rest(world):
    world.queue("feat/a", {"a.txt": "a\n"})
    world.queue("feat/bad", {"BAD": "x\n"})
    world.queue("feat/c", {"c.txt": "c\n"})
    assert world.go() == 1
    assert world.origin_has("a.txt") and world.origin_has("c.txt")
    assert not world.origin_has("BAD")
    states = {o["branch"]: o["state"] for o in world.store.last()["outcomes"]}
    assert states == {"feat/bad": "failed", "feat/a": "merged", "feat/c": "merged"}
    assert world.store.queue() == []


def test_single_red_branch_merges_nothing(world):
    world.queue("feat/bad", {"BAD": "x\n"})
    assert world.go() == 1
    assert not world.origin_has("BAD")
    assert world.calls("pr", "create") == []
    assert world.store.last()["outcomes"][0]["state"] == "failed"


def test_conflict_is_dropped_and_reported(world):
    world.queue("feat/a", {"base.txt": "from a\n"})
    world.queue("feat/b", {"base.txt": "from b\n"})
    world.queue("feat/c", {"c.txt": "c\n"})
    assert world.go() == 1
    states = {o["branch"]: o["state"] for o in world.store.last()["outcomes"]}
    assert states == {"feat/b": "conflict", "feat/a": "merged", "feat/c": "merged"}
    assert world.origin_has("c.txt")
    assert world.notes == []          # our own problem files nothing to the other side


def test_pinned_sha_ignores_later_commits(world):
    wt, sha = world.queue("feat/a", {"a.txt": "a\n"})
    (wt / "late.txt").write_text("late\n")
    sh(wt, "git", "add", "late.txt")
    sh(wt, "git", "commit", "-q", "-m", "after queueing")
    assert world.go() == 0
    assert world.origin_has("a.txt")
    assert not world.origin_has("late.txt")


def test_requeue_replaces_the_pinned_sha(world):
    wt, sha = world.queue("feat/a", {"a.txt": "a\n"})
    (wt / "late.txt").write_text("late\n")
    sh(wt, "git", "add", "late.txt")
    sh(wt, "git", "commit", "-q", "-m", "more")
    world.store.add("feat/a", sh(wt, "git", "rev-parse", "HEAD"), "again")
    assert len(world.store.queue()) == 1
    world.go()
    assert world.origin_has("late.txt")


def test_gh_missing_stops_and_changes_nothing(world, monkeypatch):
    world.queue("feat/a", {"a.txt": "a\n"})
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert world.go(gh="no-such-gh") == 2
    assert "gh is not installed" in world.said[0]
    assert len(world.store.queue()) == 1
    assert not world.origin_has("a.txt")


def test_gh_not_signed_in_stops(world, monkeypatch):
    world.queue("feat/a", {"a.txt": "a\n"})
    monkeypatch.setenv("FAKE_GH_UNAUTHED", "1")
    assert world.go() == 2
    assert "not signed in" in world.said[0]
    assert len(world.store.queue()) == 1
    assert not (world.tmp / "state" / "int").exists()


def test_if_queued_with_empty_queue_is_silent(world):
    assert world.go(if_queued=True) == 0
    assert world.said == [] and world.calls() == []


def test_dirty_main_checkout_is_left_alone(world):
    world.queue("feat/a", {"a.txt": "a\n"})
    (world.main / "base.txt").write_text("edited\n")
    assert world.go() == 0
    assert not (world.main / "a.txt").exists()
    assert "left alone" in world.store.last()["after"]


def test_green_merge_for_partner_files_a_note(world):
    world.queue("feat/a", {"a.txt": "a\n"}, "for-sam: please look at the retry")
    world.go()
    assert world.notes and world.notes[0][0] == "for-sam"


def test_add_cli_pins_branch_and_refuses_main(world, capsys):
    wt, sha = world.branch("feat/z", {"z.txt": "z\n"})
    cwd = os.getcwd()
    os.chdir(wt)
    try:
        assert mergeq.main(["add", "--summary", "zed"]) == 0
        capsys.readouterr()
        assert mergeq.main(["status", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["queued"][0]["branch"] == "feat/z"
    finally:
        os.chdir(cwd)
    q = world.store.queue()
    assert q[0]["branch"] == "feat/z" and q[0]["sha"] == sha and q[0]["summary"] == "zed"
    assert "pr" not in q[0]
    os.chdir(world.main)
    try:
        assert mergeq.main(["add"]) == 1
    finally:
        os.chdir(cwd)


def test_add_pr_pins_the_head_oid(world, capsys):
    _, sha = world.existing_pr(42, "feat/queued-pr", {"queued.txt": "queued\n"},
                               title="queue an existing change")
    cwd = os.getcwd()
    os.chdir(world.main)
    try:
        assert mergeq.main(["add", "--pr", "42"]) == 0
    finally:
        os.chdir(cwd)
    assert "queued PR #42" in capsys.readouterr().out
    entry = world.store.queue()[0]
    assert entry["branch"] == "feat/queued-pr" and entry["sha"] == sha and entry["pr"] == 42
    assert entry["summary"] == "queue an existing change"
    assert world.calls("pr", "view")[0] == [
        "pr", "view", "42", "--json", "baseRefName,headRefName,headRefOid,isCrossRepository,state,title"
    ]


def test_green_queued_pr_merges_that_pr_without_creating_one(world, monkeypatch):
    _, sha = world.existing_pr(42, "feat/queued-pr", {"queued.txt": "queued\n"})
    cwd = os.getcwd()
    os.chdir(world.main)
    try:
        assert mergeq.main(["add", "--pr", "42"]) == 0
    finally:
        os.chdir(cwd)
    commands = []
    real_run = mergeq._run

    def record_run(args, *args_, **kwargs):
        commands.append(args)
        return real_run(args, *args_, **kwargs)

    monkeypatch.setattr(mergeq, "_run", record_run)
    assert world.go() == 0
    assert world.origin_has("queued.txt")
    assert world.calls("pr", "create") == []
    assert world.calls("pr", "merge") == [
        ["pr", "merge", "42", "--merge", "--match-head-commit", sha]
    ]
    assert not any(command[:1] == ["git"] and "push" in command for command in commands)
    assert world.store.last()["outcomes"][0]["state"] == "merged"


@pytest.mark.parametrize(
    ("state", "is_cross_repository", "expected"),
    [("CLOSED", False, "CLOSED"), ("MERGED", False, "MERGED"), ("OPEN", True, "another repository")],
)
def test_add_pr_refuses_closed_merged_or_forked_pr(world, capsys, state, is_cross_repository, expected):
    world.existing_pr(42, "feat/queued-pr", {"queued.txt": "queued\n"}, state=state,
                      is_cross_repository=is_cross_repository)
    cwd = os.getcwd()
    os.chdir(world.main)
    try:
        assert mergeq.main(["add", "--pr", "42"]) == 1
    finally:
        os.chdir(cwd)
    assert expected in capsys.readouterr().err
    assert world.store.queue() == []


def test_moved_queued_pr_head_is_reported_and_not_merged(world):
    wt, sha = world.existing_pr(42, "feat/queued-pr", {"queued.txt": "queued\n"})
    cwd = os.getcwd()
    os.chdir(world.main)
    try:
        assert mergeq.main(["add", "--pr", "42"]) == 0
    finally:
        os.chdir(cwd)
    (wt / "after-queue.txt").write_text("later\n")
    sh(wt, "git", "add", "after-queue.txt")
    sh(wt, "git", "commit", "-q", "-m", "after queueing")
    sh(wt, "git", "push", "-q", "origin", "HEAD:refs/heads/feat/queued-pr")
    assert world.go() == 1
    assert world.calls("pr", "create") == []
    assert world.calls("pr", "merge") == [
        ["pr", "merge", "42", "--merge", "--match-head-commit", sha]
    ]
    outcome = world.store.last()["outcomes"][0]
    assert outcome["state"] == "push-failed" and "head moved since it was pinned" in outcome["detail"]


def test_moved_pr_head_is_refused_and_not_merged(world, monkeypatch):
    _, sha = world.queue("feat/a", {"a.txt": "a\n"})
    monkeypatch.setenv("FAKE_HEAD_OVERRIDE", "feat/a=" + "0" * 40)
    assert world.go() == 1
    assert world.calls("pr", "merge") == []
    assert not world.origin_has("a.txt")
    o = world.store.last()["outcomes"][0]
    assert o["state"] == "push-failed" and "not the tested" in o["detail"]


def test_merge_passes_the_tested_sha(world):
    _, sha = world.queue("feat/a", {"a.txt": "a\n"})
    assert world.go() == 0
    merge = world.calls("pr", "merge")[0]
    assert merge[merge.index("--match-head-commit") + 1] == sha


def test_merged_pr_with_nonzero_gh_exit_carries_on(world, monkeypatch):
    world.queue("feat/a", {"a.txt": "a\\n"})
    world.queue("feat/b", {"b.txt": "b\\n"})
    monkeypatch.setenv("FAKE_MERGE_EXIT_1", "feat/a")
    monkeypatch.setenv("FAKE_MERGED_VIEW", "1")
    assert world.go() == 0
    assert world.origin_has("a.txt") and world.origin_has("b.txt")
    # every merge is confirmed by GitHub's word on the PR, whatever gh's exit said
    said = "state,mergeCommit,headRefOid,isCrossRepository"
    assert [c for c in world.calls("pr", "view") if c[-1] == said] == [["pr", "view", n, "--json", said]
                                                                        for n in ("1", "2")]
    assert [o["state"] for o in world.store.last()["outcomes"]] == ["merged", "merged"]


def test_successful_merge_deletes_only_the_remote_branch(world):
    world.queue("feat/a", {"a.txt": "a\\n"})
    assert world.go() == 0
    deleted = subprocess.run(
        ["git", "-C", str(world.origin), "show-ref", "--verify", "--quiet", "refs/heads/feat/a"]
    )
    assert deleted.returncode == 1


def test_a_conflict_while_finding_the_culprit_is_reported_and_not_kept(world, monkeypatch):
    world.queue("feat/a", {"a.txt": "a\n"})
    world.queue("feat/bad", {"BAD": "x\n"})
    real = mergeq.Merger._merge
    calls = {"n": 0}

    def flaky(self, e):
        calls["n"] += 1
        if calls["n"] > 2 and e["branch"] == "feat/a":      # tried alone on main, after the whole batch
            return ["a.txt"]
        return real(self, e)
    monkeypatch.setattr(mergeq.Merger, "_merge", flaky)
    world.go()
    states = {o["branch"]: o["state"] for o in world.store.last()["outcomes"]}
    assert states == {"feat/a": "conflict", "feat/bad": "failed"}
    assert not world.origin_has("a.txt")


def test_failure_stops_the_run_and_later_branches_stay_queued(world, monkeypatch):
    world.queue("feat/a", {"a.txt": "a\n"})
    world.queue("feat/b", {"b.txt": "b\n"})
    world.queue("feat/c", {"c.txt": "c\n"})
    monkeypatch.setenv("FAKE_FAIL_CREATE", "feat/b")
    assert world.go() == 1
    assert world.origin_has("a.txt") and not world.origin_has("c.txt")
    assert [e["branch"] for e in world.store.queue()] == ["feat/c"]
    states = {o["branch"]: o["state"] for o in world.store.last()["outcomes"]}
    assert states == {"feat/a": "merged", "feat/b": "push-failed"}
    assert world.store.last()["waiting"] == ["feat/c"]
    assert any("feat/b" in m and "stay queued" in m for m in world.said)


def test_the_merge_waits_for_the_required_checks(world):
    # main may require CI (agent-companion does since 2026-11-01); a PR the queue
    # opened a moment ago has it still running, so the queue waits for it first.
    world.queue("feat/a", {"a.txt": "a\n"})
    assert world.go() == 0
    log = [c[:2] for c in world.calls("pr")]
    assert log.index(["pr", "checks"]) < log.index(["pr", "merge"])
    assert "--required" in world.calls("pr", "checks")[0] and "--watch" in world.calls("pr", "checks")[0]


def test_red_required_checks_merge_nothing(world, monkeypatch):
    monkeypatch.setenv("FAKE_CHECKS_FAIL", "1")
    world.queue("feat/a", {"a.txt": "a\n"})
    world.go()
    assert not world.origin_has("a.txt") and world.calls("pr", "merge") == []
    o = world.store.last()["outcomes"][0]
    assert o["state"] != "merged" and "required checks did not pass" in o["detail"]


def test_a_branch_that_requires_no_checks_still_merges(world, monkeypatch):
    from agent_companion import mergeq
    monkeypatch.setattr(mergeq, "NO_CHECKS_GRACE_S", 0)
    monkeypatch.setenv("FAKE_NO_REQUIRED", "1")
    world.queue("feat/a", {"a.txt": "a\n"})
    assert world.go() == 0 and world.origin_has("a.txt")


def test_a_queue_written_as_one_list_becomes_an_entry_each_once(tmp_path):
    """The store before each entry had a file of its own: queue.json, one list.
    Its waiting entries become files on first use, and the list stays beside
    them as queue.json.migrated, so nothing queued is lost in the upgrade."""
    (tmp_path / "queue.json").write_text(json.dumps([
        {"branch": "feat/a", "sha": "a" * 40, "summary": "one", "added": 1.0},
        {"branch": "feat/b", "sha": "b" * 40, "summary": "", "added": 2.0, "pr": 7}]))
    q = mergeq.Store(tmp_path).queue()
    assert [(e["branch"], e["sha"], e["summary"], e["added"], e.get("pr")) for e in q] == [
        ("feat/a", "a" * 40, "one", 1.0, None), ("feat/b", "b" * 40, "", 2.0, 7)]
    assert not (tmp_path / "queue.json").exists() and (tmp_path / "queue.json.migrated").exists()
    assert len(mergeq.Store(tmp_path).queue()) == 2        # once: the next store finds no list


@pytest.mark.parametrize("env,land,base", [
    ({}, "pr", "main"),
    ({"AGENT_COMPANION_MERGEQ_LAND": "local", "AGENT_COMPANION_MERGEQ_BASE": "master"}, "local", "master"),
    ({"AGENT_COMPANION_MERGEQ_LAND": "rebase"}, "pr", "main"),
])
def test_the_settings_say_how_and_onto_what_a_batch_lands(monkeypatch, env, land, base):
    for k in ("AGENT_COMPANION_MERGEQ_LAND", "AGENT_COMPANION_MERGEQ_BASE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert (mergeq.land_mode(), mergeq.base_branch()) == (land, base)


def _add_pr(number):
    return mergeq.main(["add", "--pr", str(number)])


def test_add_pr_refuses_a_pr_onto_another_branch(world, capsys):
    world.existing_pr(42, "feat/queued-pr", {"queued.txt": "queued\n"}, base="release")
    cwd = os.getcwd()
    os.chdir(world.main)
    try:
        assert _add_pr(42) == 1
    finally:
        os.chdir(cwd)
    assert "targets release, not main" in capsys.readouterr().err
    assert world.store.queue() == []


def test_a_queued_pr_moved_onto_another_branch_is_not_merged(world, monkeypatch):
    world.existing_pr(42, "feat/queued-pr", {"queued.txt": "queued\n"})
    cwd = os.getcwd()
    os.chdir(world.main)
    try:
        assert _add_pr(42) == 0
    finally:
        os.chdir(cwd)
    prs = json.loads(os.environ["FAKE_EXISTING_PRS"])
    prs["42"]["base"] = "release"            # retargeted on GitHub after it was queued
    monkeypatch.setenv("FAKE_EXISTING_PRS", json.dumps(prs))
    assert world.go() == 1
    assert world.calls("pr", "merge") == []
    o = world.store.last()["outcomes"][0]
    assert o["state"] == "push-failed" and "targets release, not main" in o["detail"]


def test_an_open_pr_onto_another_branch_is_not_the_one_merged(world, monkeypatch):
    # GitHub keeps one open PR per head and base; the queue's is the one onto its own base.
    _, sha = world.existing_pr(42, "feat/a", {"a.txt": "a\n"})
    prs = json.loads(os.environ["FAKE_EXISTING_PRS"])
    prs["43"] = {"branch": "feat/a", "state": "OPEN", "base": "release"}
    monkeypatch.setenv("FAKE_EXISTING_PRS", json.dumps(prs))
    world.store.add("feat/a", sha)
    assert world.go() == 0
    assert [c[2] for c in world.calls("pr", "merge")] == ["42"]
    assert world.origin_has("a.txt")


def test_main_moving_while_the_checks_run_lands_nothing_and_keeps_the_queue(world, monkeypatch):
    # The checks can take half an hour, and a merge made after main moved would put the batch on a main
    # the suite never saw. The next batch tests them on the new main, through the PR already open.
    world.queue("feat/a", {"a.txt": "a\n"})
    world.queue("feat/b", {"b.txt": "b\n"})
    monkeypatch.setenv("FAKE_LAND_DURING_CHECKS", "1")
    assert world.go() == 0
    assert world.calls("pr", "merge") == [] and not world.origin_has("a.txt")
    assert [e["branch"] for e in world.store.queue()] == ["feat/a", "feat/b"]
    last = world.store.last()
    assert last["result"] == "waiting" and "moved outside the queue" in last["detail"]
    assert world.go() == 0
    assert world.origin_has("a.txt") and world.origin_has("b.txt")
    assert len(world.calls("pr", "create")) == 3       # feat/a twice (the second "already exists"), feat/b once


def test_a_merge_github_put_on_a_main_that_moved_after_the_check_is_called_out(world, monkeypatch):
    # GitHub pins a merge's head, never its base: a commit landing in the seconds between the queue's last
    # look and the merge goes in under it. The queue says so, and lands nothing more on top.
    world.queue("feat/a", {"a.txt": "a\n"})
    world.queue("feat/b", {"b.txt": "b\n"})
    monkeypatch.setenv("FAKE_LAND_BEFORE_MERGE", "1")
    assert world.go() == 1
    assert [c[2] for c in world.calls("pr", "merge")] == ["1"]
    assert {e["branch"]: e["state"] for e in world.store.entries()} == {"feat/a": "merged", "feat/b": "queued"}
    assert "never tested" in world.store.last()["detail"]


def test_a_merge_github_only_queued_is_not_called_merged(world, monkeypatch):
    # On a branch that requires GitHub's own merge queue, `gh pr merge` exits 0 having only queued the PR.
    monkeypatch.setattr(mergeq, "MERGE_CONFIRM_S", 0)
    monkeypatch.setenv("FAKE_MERGE_QUEUE", "1")
    world.queue("feat/a", {"a.txt": "a\n"})
    assert world.go() == 1
    o = world.store.last()["outcomes"][0]
    assert o["state"] == "push-failed" and "merge queue" in o["detail"]
    assert not world.origin_has("a.txt")
    kept = subprocess.run(["git", "-C", str(world.origin), "show-ref", "--verify", "--quiet", "refs/heads/feat/a"])
    assert kept.returncode == 0          # GitHub still needs the branch


def test_a_branch_that_moved_on_after_its_merge_is_not_deleted(world, monkeypatch):
    # More work pushed to the branch after the queue tested it: the delete must not take it with it.
    world.queue("feat/a", {"a.txt": "a\n"})
    monkeypatch.setenv("FAKE_BRANCH_MOVES_ON_MERGE", "feat/a")
    assert world.go() == 0
    assert world.origin_has("a.txt")
    kept = subprocess.run(["git", "-C", str(world.origin), "show-ref", "--verify", "--quiet", "refs/heads/feat/a"])
    assert kept.returncode == 0


def test_a_branch_inside_an_earlier_one_lands_with_it_and_needs_no_pr(world):
    _, b_sha = world.branch("feat/b", {"b.txt": "b\n"})
    wt_a = world.tmp / "wt-feat-a"
    sh(world.main, "git", "worktree", "add", "-q", "-b", "feat/a", str(wt_a), "feat/b")
    (wt_a / "a.txt").write_text("a\n")
    sh(wt_a, "git", "add", "a.txt")
    sh(wt_a, "git", "commit", "-q", "-m", "a, on top of b")
    world.store.add("feat/a", sh(wt_a, "git", "rev-parse", "HEAD"))
    world.store.add("feat/b", b_sha)
    assert world.go() == 0
    assert [c[c.index("--head") + 1] for c in world.calls("pr", "create")] == ["feat/a"]
    assert {e["branch"]: e["state"] for e in world.store.entries()} == {"feat/a": "merged", "feat/b": "merged"}
    assert world.origin_has("a.txt") and world.origin_has("b.txt")


def test_a_crash_after_a_pr_merged_says_main_moved(world, monkeypatch):
    world.queue("feat/a", {"a.txt": "a\n"})
    real = mergeq.Store.mark

    def disk_full(self, e):
        if e["state"] == "merged":
            raise OSError("disk full")
        return real(self, e)
    monkeypatch.setattr(mergeq.Store, "mark", disk_full)
    assert world.go() == 2
    last = world.store.last()
    assert last["result"] == "failed" and last["master_moved"] is True and "AFTER main moved" in last["detail"]
    assert world.origin_has("a.txt")


def test_a_local_landing_whose_push_fails_says_so_and_does_not_exit_clean(world):
    world.queue("feat/a", {"a.txt": "a\n"})
    sh(world.main, "git", "config", "remote.origin.pushurl", str(world.tmp / "nowhere.git"))
    assert world.go(land="local") == 1
    assert (world.main / "a.txt").exists() and not world.origin_has("a.txt")
    assert any(m.startswith("the push failed") for m in world.said)


def test_a_migration_cut_short_queues_nothing_twice(tmp_path, monkeypatch):
    (tmp_path / "queue.json").write_text(json.dumps([
        {"branch": "feat/a", "sha": "a" * 40, "summary": "", "added": 1.0},
        {"branch": "feat/b", "sha": "b" * 40, "summary": "", "added": 2.0}]))
    real = os.replace

    def killed(src, dst):
        if Path(src).name == "queue.json":
            raise OSError("killed")
        return real(src, dst)
    monkeypatch.setattr(mergeq.os, "replace", killed)
    with pytest.raises(OSError):
        mergeq.Store(tmp_path)
    monkeypatch.setattr(mergeq.os, "replace", real)
    assert [e["branch"] for e in mergeq.Store(tmp_path).queue()] == ["feat/a", "feat/b"]


def test_a_pr_retargeted_while_its_checks_run_is_not_merged(world, monkeypatch):
    world.queue("feat/a", {"a.txt": "a\n"})
    monkeypatch.setenv("FAKE_RETARGET_DURING_CHECKS", "release")
    assert world.go() == 1
    assert world.calls("pr", "merge") == []
    o = world.store.last()["outcomes"][0]
    assert o["state"] == "push-failed" and "targets release, not main" in o["detail"]


def test_a_merge_that_did_not_reach_main_is_called_out(world, monkeypatch):
    # Retargeted in the seconds between the queue's last look and the merge: GitHub merged it elsewhere,
    # onto a branch at the same commit, so only where its merge commit is tells.
    world.queue("feat/a", {"a.txt": "a\n"})
    world.queue("feat/b", {"b.txt": "b\n"})
    monkeypatch.setenv("FAKE_MERGE_INTO", "release")
    assert world.go() == 1
    assert not world.origin_has("a.txt") and [c[2] for c in world.calls("pr", "merge")] == ["1"]
    assert {e["branch"]: e["state"] for e in world.store.entries()} == {"feat/a": "merged", "feat/b": "queued"}
    assert "is not on main" in world.store.last()["detail"]


def test_a_migrated_entry_with_no_usable_time_still_moves_over(tmp_path):
    (tmp_path / "queue.json").write_text('[{"branch": "feat/a", "sha": "%s", "added": Infinity}]' % ("a" * 40))
    assert [e["branch"] for e in mergeq.Store(tmp_path).queue()] == ["feat/a"]
