"""The merge queue's engine (agent_companion.mergeq.Merger, land = "local")
against real throwaway git repos. The suite, push, restart and notices are
fakes: nothing here touches a real checkout, the network or systemd. Landing
through PRs, the CLI and the queue's files are tests/test_mergeq.py."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_companion import mergeq as mq


def sh(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def world(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sh(repo, "init", "-q", "-b", "main")
    (repo / "base.txt").write_text("base\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "a.md").write_text("a\n")
    sh(repo, "add", "."); sh(repo, "commit", "-qm", "base")

    class W:
        pass
    w = W()
    w.repo, w.calls = repo, {"push": 0, "restart": 0, "notify": [], "handoff": []}
    w.store = mq.Store(tmp_path / "q")

    def branch(name, files):
        sh(repo, "checkout", "-q", "-b", name, "main")
        for f, text in files.items():
            (repo / f).parent.mkdir(exist_ok=True)
            (repo / f).write_text(text)
        sh(repo, "add", "."); sh(repo, "commit", "-qm", name)
        sh(repo, "checkout", "-q", "main")
        return mq.Store.add(w.store, name, sh(repo, "rev-parse", name), speaker=name.upper(), session="s-" + name)
    w.branch = branch

    def suite(tree, ids):
        bad_file = Path(tree) / "BAD"
        bad = bad_file.read_text().split() if bad_file.exists() else []
        if ids is not None:
            bad = [b for b in bad if b in ids]
        return (not bad), sorted(bad), "tail"

    class P:
        returncode = 0
        stderr = ""

    def push(repo_):
        w.calls["push"] += 1
        return P()

    def restart():
        w.calls["restart"] += 1

    w.merger = mq.Merger(repo=repo, store=w.store, tree=tmp_path / "int", suite=suite, push=push, restart=restart,
                         notify=lambda t, b: w.calls["notify"].append(b),
                         handoff=lambda e, t: w.calls["handoff"].append((e["session"], t)))
    return w


def test_green_batch_fast_forwards_master_pushes_and_restarts_once(world):
    world.branch("a", {"a.txt": "a"}); world.branch("b", {"b.txt": "b"})
    before = sh(world.repo, "rev-parse", "main")
    r = world.merger.run()
    assert r["result"] == "green" and len(r["merged"]) == 2, r
    assert sh(world.repo, "rev-parse", "main") != before
    assert sh(world.repo, "merge-base", "--is-ancestor", "a", "main") == ""
    assert (world.repo / "a.txt").exists() and (world.repo / "b.txt").exists()
    assert sh(world.repo, "status", "--porcelain") == "" and sh(world.repo, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert world.calls["push"] == 1 and world.calls["restart"] == 1
    assert [e["state"] for e in world.store.entries()] == ["merged", "merged"]
    assert not world.merger.tree.exists()
    assert len(world.calls["notify"]) == 1 and "A" in world.calls["notify"][0]


def test_a_conflict_is_ejected_and_told_to_its_session_the_rest_merge(world):
    world.branch("a", {"base.txt": "from a\n"}); world.branch("b", {"base.txt": "from b\n"})
    world.branch("c", {"c.txt": "c"})
    r = world.merger.run()
    assert r["result"] == "green" and len(r["merged"]) == 2 and len(r["conflict"]) == 1
    states = {e["branch"]: e["state"] for e in world.store.entries()}
    assert states == {"a": "merged", "b": "conflict", "c": "merged"}
    assert world.calls["handoff"][0][0] == "s-b" and "base.txt" in world.calls["handoff"][0][1]
    assert world.calls["restart"] == 1


def test_a_red_batch_ejects_the_culprit_with_its_ids_and_ships_the_rest(world):
    world.branch("a", {"a.txt": "a"}); world.branch("b", {"BAD": "test_x test_y\n"}); world.branch("c", {"c.txt": "c"})
    r = world.merger.run()
    assert r["result"] == "green" and len(r["failed"]) == 1
    e = {x["branch"]: x for x in world.store.entries()}
    assert e["b"]["state"] == "failed" and e["b"]["ids"] == ["test_x", "test_y"]
    assert e["a"]["state"] == e["c"]["state"] == "merged"
    assert not (world.repo / "BAD").exists()
    assert world.calls["handoff"][0][0] == "s-b"


def test_red_that_cannot_be_pinned_on_an_entry_moves_nothing(world):
    world.branch("a", {"a.txt": "a"}); world.branch("b", {"b.txt": "b"})
    calls = {"n": 0}
    real = world.merger.suite

    def flaky(tree, ids):
        calls["n"] += 1
        return (True, [], "") if ids is not None else (False, ["test_flaky"], "boom")
    world.merger.suite = flaky
    before = sh(world.repo, "rev-parse", "main")
    r = world.merger.run()
    assert r["result"] == "red" and sh(world.repo, "rev-parse", "main") == before
    assert world.calls["push"] == world.calls["restart"] == 0
    assert "red again on a second full run" in r["detail"]


def test_a_flake_that_passes_on_a_second_full_run_ships_the_batch(world):
    world.branch("a", {"a.txt": "a"}); world.branch("b", {"b.txt": "b"})
    full = {"n": 0}

    def once(tree, ids):
        if ids is not None:
            return True, [], ""
        full["n"] += 1
        return (False, ["test_flaky"], "boom") if full["n"] == 1 else (True, [], "")
    world.merger.suite = once
    r = world.merger.run()
    assert r["result"] == "green" and r["flaky"] == ["test_flaky"] and full["n"] == 2
    assert r["red_tail"] == "boom"
    assert {x["branch"]: x["state"] for x in world.store.entries()} == {"a": "merged", "b": "merged"}
    assert world.calls["push"] == 1


def test_a_red_master_stops_the_queue_and_blames_nobody(world):
    (world.repo / "BAD").write_text("test_old\n"); sh(world.repo, "add", "BAD"); sh(world.repo, "commit", "-qm", "old bug")
    world.branch("a", {"a.txt": "a"})
    before = sh(world.repo, "rev-parse", "main")
    r = world.merger.run()
    assert r["result"] == "master_red" and "test_old" in r["detail"] and not r["failed"]
    _nothing_moved(world, before)
    assert world.store.entries()[0]["state"] == "queued", "waits, not ejected"
    assert any("stopped" in n for n in world.calls["notify"])
    n = len(world.calls["notify"])
    world.merger.run()
    assert len(world.calls["notify"]) == n, "once an hour at most"


def test_a_crashing_suite_finds_its_culprit_by_testing_entries_alone(world):
    world.branch("a", {"a.txt": "a"}); world.branch("b", {"BAD": "x"}); world.branch("c", {"c.txt": "c"})
    world.merger.suite = lambda tree, ids: (not (Path(tree) / "BAD").exists(), [], "crashed, no id named")
    r = world.merger.run()
    e = {x["branch"]: x["state"] for x in world.store.entries()}
    assert r["result"] == "green" and e == {"a": "merged", "b": "failed", "c": "merged"}
    assert not (world.repo / "BAD").exists() and world.calls["restart"] == 1


def test_an_id_that_only_exists_in_the_entry_does_not_mis_blame_the_others(world):
    world.branch("a", {"a.txt": "a"}); world.branch("b", {"BAD": "test_new"})
    inner = world.merger.suite

    def suite(tree, ids):
        ok, bad, tail = inner(tree, ids)
        if ids is not None and not (Path(tree) / "BAD").exists():
            return False, [], "ERROR: not found: test_new"      # the id does not exist on main
        return ok, bad, tail
    world.merger.suite = suite
    r = world.merger.run()
    e = {x["branch"]: x["state"] for x in world.store.entries()}
    assert e == {"a": "merged", "b": "failed"}


def test_running_out_of_time_stalls_and_says_so_with_nothing_moved(world):
    world.branch("a", {"a.txt": "a"}); world.branch("b", {"BAD": "test_x"})
    world.merger.budget_s = -1
    before = sh(world.repo, "rev-parse", "main")
    r = world.merger.run()
    assert r["result"] == "stalled"
    _nothing_moved(world, before)
    assert any("stalled" in n for n in world.calls["notify"])


def test_a_failed_restart_is_loud(world):
    world.branch("a", {"a.txt": "a"})

    class R:
        returncode, stderr = 1, "Unit not found"
    world.merger.restart = lambda: R()
    r = world.merger.run()
    assert r["restarted"] is False and "Unit not found" in r["restart_error"]
    assert any("restart FAILED" in n for n in world.calls["notify"])


def test_a_host_with_no_service_is_not_told_it_restarted(world):
    """restart() answers None when the app has no service of its own: nothing
    restarted, and nothing failed either, so no restart notice."""
    world.branch("a", {"a.txt": "a"})
    world.merger.restart = lambda: None
    r = world.merger.run()
    assert r["result"] == "green" and r["restarted"] is None and "restart_error" not in r
    assert not any("restart" in n for n in world.calls["notify"])


def test_pushed_commits_are_counted_before_the_push(world, tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    sh(world.repo, "remote", "add", "origin", str(origin)); sh(world.repo, "push", "-q", "origin", "main")
    sh(world.repo, "fetch", "-q", "origin")
    world.branch("a", {"a.txt": "a"})
    world.merger.push = lambda repo: subprocess.run(["git", "-C", str(repo), "push", "origin", "main"], capture_output=True, text=True)
    r = world.merger.run()
    assert r["pushed"] is True and r["pushed_commits"] == 2        # the branch commit and the merge


def test_an_exception_after_the_fast_forward_says_master_moved(world):
    world.branch("a", {"a.txt": "a"})
    before = sh(world.repo, "rev-parse", "main")
    world.store.mark = lambda e: (_ for _ in ()).throw(OSError("disk"))
    r = world.merger.run()
    assert r["result"] == "failed" and r["master_moved"] is True and "AFTER main moved" in r["detail"]
    assert sh(world.repo, "rev-parse", "main") != before


def test_waiting_says_so_once_an_hour_with_how_long(world):
    world.branch("a", {"a.txt": "a"})
    (world.repo / "base.txt").write_text("dirty\n")
    clock = [100000.0]
    world.merger.now = lambda: clock[0]
    world.merger.run(); world.merger.run()
    assert sum("waiting" in n for n in world.calls["notify"]) == 1
    clock[0] += 3700
    world.merger.run()
    notes = [n for n in world.calls["notify"] if "waiting" in n]
    assert len(notes) == 2 and "61 min" in notes[1]


def test_mark_does_not_resurrect_an_entry_add_replaced(world):
    old = mq.Store.add(world.store, "x", "1111")
    mq.Store.add(world.store, "x", "2222")
    old["state"] = "failed"
    world.store.mark(old)
    es = world.store.entries()
    assert [(e["sha"], e["state"]) for e in es] == [("2222", "queued")]


def test_a_dirty_master_waits_and_says_so(world):
    world.branch("a", {"a.txt": "a"})
    (world.repo / "base.txt").write_text("edited by someone\n")
    before = sh(world.repo, "rev-parse", "main")
    r = world.merger.run()
    assert r["result"] == "waiting" and "uncommitted" in r["detail"]
    assert sh(world.repo, "rev-parse", "main") == before
    assert (world.repo / "base.txt").read_text() == "edited by someone\n", "never touched"
    assert world.store.entries()[0]["state"] == "queued" and world.calls["push"] == 0
    (world.repo / "base.txt").write_text("base\n")
    assert world.merger.run()["result"] == "green"


def test_a_checkout_not_on_master_waits(world):
    world.branch("a", {"a.txt": "a"})
    sh(world.repo, "checkout", "-q", "a")
    assert world.merger.run()["result"] == "waiting"


def test_a_branch_that_moved_after_queueing_is_stale_not_merged(world):
    world.branch("a", {"a.txt": "a"})
    sh(world.repo, "checkout", "-q", "a"); (world.repo / "more.txt").write_text("m"); sh(world.repo, "add", "."); sh(world.repo, "commit", "-qm", "more")
    sh(world.repo, "checkout", "-q", "main")
    r = world.merger.run()
    assert r["result"] == "empty" and world.store.entries()[0]["state"] == "stale"


def test_master_moving_by_docs_alone_also_requeues(world):
    """Fail closed: what was tested is not what main would become."""
    world.branch("a", {"a.txt": "a"})

    def docs_commit_during_suite(tree, ids):
        (world.repo / "docs" / "a.md").write_text("changed\n"); sh(world.repo, "commit", "-qam", "docs")
        return True, [], ""
    world.merger.suite = docs_commit_during_suite
    r = world.merger.run()
    assert r["result"] == "waiting" and world.calls["push"] == world.calls["restart"] == 0


def test_master_moving_by_code_requeues(world):
    world.branch("a", {"a.txt": "a"})

    def code_commit_during_suite(tree, ids):
        (world.repo / "base.txt").write_text("new\n"); sh(world.repo, "commit", "-qam", "code")
        return True, [], ""
    world.merger.suite = code_commit_during_suite
    r = world.merger.run()
    assert r["result"] == "waiting" and world.store.entries()[0]["state"] == "queued"


def test_an_empty_queue_runs_no_suite(world):
    world.merger.suite = lambda *a: (_ for _ in ()).throw(AssertionError("suite ran"))
    assert world.merger.run()["result"] == "empty"


def test_adding_a_branch_again_replaces_its_queued_entry(world):
    world.branch("a", {"a.txt": "a"})
    world.branch("a2", {"x": "x"})
    mq.Store.add(world.store, "a", "deadbeef")
    assert [e["branch"] for e in world.store.entries()].count("a") == 1


def test_a_second_merger_does_not_run_at_once(world):
    world.branch("a", {"a.txt": "a"})
    with world.store.locked(".run.lock"):
        assert world.merger.run()["result"] == "busy"


def test_merged_branch_worktree_is_removed_when_clean(world, tmp_path):
    world.branch("a", {"a.txt": "a"})
    wt = tmp_path / "wt-a"
    sh(world.repo, "worktree", "add", "-q", str(wt), "a")
    world.merger.run()
    assert not wt.exists()


def test_status_for_a_dashboard(world):
    world.branch("a", {"a.txt": "a"}); world.branch("b", {"base.txt": "z\n"}); world.branch("c", {"base.txt": "y\n"})
    s = mq.status(world.store, lambda: "10:30")
    assert s["queued"] == 3 and s["next_run"] == "10:30" and s["last"] is None
    world.merger.run()
    s = mq.status(world.store)
    assert s["queued"] == 0 and s["last"]["result"] == "green" and s["last"]["merged"] == 2
    assert [t["branch"] for t in s["trouble"]] == ["c"] and s["trouble"][0]["state"] == "conflict"


def test_trouble_that_reached_master_another_way_is_settled(world):
    # 2026-10-18: fix/audit-comms was ejected red, then landed inside another merge; its red sat in the
    # status for 21 hours with nothing wrong. An entry whose pinned sha is on main is no longer trouble.
    world.branch("b", {"base.txt": "z\n"}); world.branch("c", {"base.txt": "y\n"})
    world.merger.run()
    assert [t["branch"] for t in mq.status(world.store)["trouble"]] == ["c"]
    sh(world.repo, "merge", "-q", "-X", "theirs", "-m", "c by hand", "c")
    world.branch("d", {"d.txt": "d"})
    world.merger.run()
    assert mq.status(world.store)["trouble"] == []
    e = {x["branch"]: x for x in world.store.entries()}
    assert e["c"]["state"] == "merged" and "outside the queue" in e["c"]["reason"]


def test_settle_alone_clears_landed_trouble_and_leaves_the_rest(world):
    world.branch("b", {"base.txt": "z\n"}); world.branch("c", {"base.txt": "y\n"})
    world.merger.run()
    assert world.merger.settle() == []
    sh(world.repo, "merge", "-q", "-X", "theirs", "-m", "c by hand", "c")
    assert world.merger.settle() == ["c"]
    assert mq.status(world.store)["trouble"] == []


def _nothing_moved(world, before):
    assert sh(world.repo, "rev-parse", "main") == before
    assert world.calls["push"] == world.calls["restart"] == 0
    assert all(e["state"] != "merged" for e in world.store.entries())


@pytest.mark.parametrize("code,out", [
    (1, "1 failed, 5 passed in 1.0s"),
    (0, "3 passed, 1 failed in 1.0s"),
    (0, "3 passed, 2 errors in 1.0s"),
    (0, "no tests ran in 0.01s"),
    (0, "0 passed in 0.01s"),
    (5, "no tests ran in 0.01s"),
    (0, ""),
    (0, "something we cannot parse"),
    (-9, "12 passed in 1.0s"),
    (2, "12 passed, 1 error in 1.0s"),
])
def test_only_an_explicit_pass_is_green(code, out):
    assert not mq.green_by_the_book(code, out)


def test_an_explicit_pass_is_green():
    assert mq.green_by_the_book(0, "...\n2188 passed, 2 warnings in 905.33s (0:15:05)")
    assert mq.green_by_the_book(0, "=== 12 passed in 1.0s ===")


def test_a_suite_that_crashes_names_no_test_and_moves_nothing(world):
    world.branch("a", {"a.txt": "a"})
    before = sh(world.repo, "rev-parse", "main")
    world.merger.suite = lambda tree, ids: (False, [], "crashed")
    r = world.merger.run()
    assert r["result"] == "master_red", "the bare main is red too, so nothing is ejected"
    _nothing_moved(world, before)
    assert world.store.entries()[0]["state"] == "queued"


def test_an_exception_anywhere_is_a_failed_batch_and_nothing_moves(world):
    world.branch("a", {"a.txt": "a"})
    before = sh(world.repo, "rev-parse", "main")

    def boom(tree, ids):
        raise RuntimeError("disk full")
    world.merger.suite = boom
    r = world.merger.run()
    assert r["result"] == "failed" and "disk full" in r["detail"]
    _nothing_moved(world, before)
    assert world.store.last()["result"] == "failed" and not world.merger.tree.exists()


def test_an_exception_in_the_final_gate_is_a_failed_batch(world):
    world.branch("a", {"a.txt": "a"})
    before = sh(world.repo, "rev-parse", "main")
    world.merger._master_ready = lambda: (_ for _ in ()).throw(OSError("gone"))
    assert world.merger.run()["result"] == "failed"
    _nothing_moved(world, before)


def test_a_tree_changed_after_testing_is_not_fast_forwarded(world):
    world.branch("a", {"a.txt": "a"})
    before = sh(world.repo, "rev-parse", "main")
    real = world.merger._master_ready

    def tamper():
        (world.merger.tree / "sneaky.txt").write_text("x")
        sh(world.merger.tree, "add", "."); sh(world.merger.tree, "commit", "-qm", "untested")
        return real()
    world.merger._master_ready = tamper
    r = world.merger.run()
    assert r["result"] == "waiting" and "changed after it was tested" in r["detail"]
    _nothing_moved(world, before)


def test_an_entry_whose_branch_moved_during_the_suite_is_not_merged(world):
    world.branch("a", {"a.txt": "a"})
    before = sh(world.repo, "rev-parse", "main")
    inner = world.merger.suite

    def suite(tree, ids):
        sh(world.repo, "checkout", "-q", "a"); (world.repo / "late.txt").write_text("l")
        sh(world.repo, "add", "."); sh(world.repo, "commit", "-qm", "late"); sh(world.repo, "checkout", "-q", "main")
        return inner(tree, ids)
    world.merger.suite = suite
    r = world.merger.run()
    assert r["result"] == "waiting" and "moved while the suite ran" in r["detail"]
    _nothing_moved(world, before)


@pytest.mark.parametrize("marker", ["MERGE_HEAD", "CHERRY_PICK_HEAD", "rebase-merge"])
def test_a_merge_or_rebase_in_progress_in_the_live_checkout_waits(world, marker):
    world.branch("a", {"a.txt": "a"})
    before = sh(world.repo, "rev-parse", "main")
    inner = world.merger.suite

    def suite(tree, ids):
        gp = world.repo / ".git" / marker
        gp.mkdir() if marker == "rebase-merge" else gp.write_text(before + "\n")
        return inner(tree, ids)
    world.merger.suite = suite
    r = world.merger.run()
    assert r["result"] == "waiting" and marker in r["detail"]
    _nothing_moved(world, before)


def test_staged_changes_in_the_live_checkout_wait_too(world):
    world.branch("a", {"a.txt": "a"})
    (world.repo / "base.txt").write_text("staged\n"); sh(world.repo, "add", "base.txt")
    before = sh(world.repo, "rev-parse", "main")
    assert world.merger.run()["result"] == "waiting"
    _nothing_moved(world, before)


def test_a_failing_push_or_restart_exception_is_reported_not_hidden_and_not_fatal(world):
    world.branch("a", {"a.txt": "a"})
    world.merger.push = lambda repo: (_ for _ in ()).throw(OSError("offline"))
    world.merger.restart = lambda: (_ for _ in ()).throw(OSError("no systemd"))
    r = world.merger.run()
    assert r["result"] == "green" and r["pushed"] is False and "offline" in r["push_error"]
    assert r["restarted"] is False and "no systemd" in r["restart_error"]


def test_red_with_no_test_named_is_run_once_more_before_anyone_is_blamed(world):
    world.branch("a", {"a.txt": "a"}); world.branch("b", {"b.txt": "b"})
    runs = []

    def flaky(tree, ids):
        runs.append(ids)
        return (len(runs) > 1), [], "Killed"
    world.merger.suite = flaky
    r = world.merger.run()
    assert r["result"] == "green" and r.get("retried") and runs == [None, None]
    assert {x["state"] for x in world.store.entries()} == {"merged"}


def test_a_batch_takes_the_hold_once_and_lets_it_go(world):
    """A host app's other work stands down while the suite runs: held once
    however many suite runs the batch needs, and let go when it ends."""
    held = []
    world.merger.hold = held.append
    world.branch("a", {"a.txt": "a"}); world.branch("b", {"BAD": "test_x\n"})
    r = world.merger.run()
    assert r["result"] == "green" and len(r["failed"]) == 1
    assert held == [True, False]


def test_an_empty_queue_never_takes_the_hold(world):
    held = []
    world.merger.hold = held.append
    world.merger.run()
    assert held == []


def test_a_crashed_batch_still_lets_the_hold_go(world):
    held = []
    world.merger.hold = held.append
    world.branch("a", {"a.txt": "a"})
    world.store.mark = lambda e: (_ for _ in ()).throw(OSError("disk"))
    assert world.merger.run()["result"] == "failed"
    assert held == [True, False]


PARAM_OUT = (
    "FAILED tests/test_page_layout.py::test_badge[badge1-Rain \xb7 nothing to show]\n"
    "FAILED tests/test_page_layout.py::test_badge[None-Rain \xb7 not read] - AssertionError: x\n"
    "FAILED tests/test_page_layout.py::test_why[unknown-The service does not know where it is right now,\n"
    "FAILED tests/test_queue.py::test_enter - AssertionError: boom\n"
    "4 failed, 10 passed in 3.00s\n")


def test_a_parametrized_failure_with_spaces_names_its_test():
    """A parametrized id can hold spaces; the test it names is what a rerun needs."""
    assert mq.failing_ids(PARAM_OUT) == ["tests/test_page_layout.py::test_badge",
                                         "tests/test_page_layout.py::test_why",
                                         "tests/test_queue.py::test_enter"]



def test_merges_carry_the_mergers_name_whatever_the_environment_says(world, monkeypatch):
    """The merge commits' author and committer are the merger's identity, even
    when the environment names someone else (a session can export GIT_AUTHOR_*)."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "someone"); monkeypatch.setenv("GIT_COMMITTER_EMAIL", "someone@else")
    world.merger.identity = ("queue", "queue@example.com")
    world.branch("a", {"a.txt": "a"})
    assert world.merger.run()["result"] == "green"
    assert sh(world.repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>", "main") == \
        "queue <queue@example.com>|queue <queue@example.com>"


@pytest.mark.parametrize("file_mode", [False, True])
def test_only_a_merger_that_asks_gives_its_tree_gits_executable_bits(world, file_mode):
    """file_mode turns core.fileMode on in the integration tree alone (a Linux
    tree of a checkout on a Windows drive). Without it the checkout's config is
    left exactly as it was."""
    seen, real = [], world.merger.suite

    def suite(tree, ids):
        seen.append(sh(tree, "config", "--worktree", "--get", "core.fileMode") if file_mode else "")
        return real(tree, ids)
    world.merger.suite, world.merger.file_mode = suite, file_mode
    world.branch("a", {"a.txt": "a"})
    assert world.merger.run()["result"] == "green"
    worktree_config = subprocess.run(["git", "-C", str(world.repo), "config", "--get", "extensions.worktreeConfig"],
                                     capture_output=True, text=True).stdout.strip()
    assert (seen, worktree_config) == ((["true"], "true") if file_mode else ([""], ""))


def test_what_the_suite_says_about_its_run_is_kept_in_the_result(world):
    """A host's suite can say how it ran (where, what it left out): the note it
    fills is kept under note_key, and emptied before each full run."""
    note, real = {"left": "from an earlier batch"}, world.merger.suite

    def suite(tree, ids):
        note["where"] = "here"
        return real(tree, ids)
    world.merger.suite, world.merger.note, world.merger.note_key = suite, note, "runner"
    world.branch("a", {"a.txt": "a"})
    assert world.merger.run()["runner"] == {"where": "here"}


def test_a_merged_entry_says_how_many_passed_as_a_pull_request_does(world):
    """Landing locally, each merged entry and its history row carry the suite's
    passed count, as the pull request path's always did ("1444 passed")."""
    real = world.merger.suite

    def suite(tree, ids):
        green, bad, _ = real(tree, ids)
        return green, bad, "12 passed in 3.40s"
    world.merger.suite = suite
    world.branch("a", {"a.txt": "a"}); world.branch("b", {"b.txt": "b"})
    r = world.merger.run()
    assert r["result"] == "green", r
    assert [o["detail"] for o in r["outcomes"]] == ["12 passed", "12 passed"]
    assert [e["reason"] for e in world.store.entries()] == ["12 passed", "12 passed"]
    assert [h["detail"] for h in world.store.history()] == ["12 passed", "12 passed"]


def test_a_merged_entry_with_no_count_in_the_tail_claims_none(world):
    """A tail with no summary in it gives an empty detail, never "0 passed"."""
    world.branch("a", {"a.txt": "a"})
    assert world.merger.run()["outcomes"][0]["detail"] == ""


def test_a_queued_branch_already_on_main_is_settled_and_nothing_restarts(world):
    world.branch("a", {"a.txt": "a"})
    sh(world.repo, "merge", "-q", "--no-ff", "-m", "a by hand", "a")
    r = world.merger.run()
    assert r["result"] == "empty" and r["merged"] == []
    assert world.calls["push"] == world.calls["restart"] == 0
    e = world.store.entries()[0]
    assert e["state"] == "merged" and "outside the queue" in e["reason"]
