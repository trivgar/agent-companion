import threading

from agent_companion.gitlive import LiveGit


class SlowGit:
    """for_repo blocks until released, counting calls."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = 0
        self.go = threading.Event()

    def for_repo(self, cwd):
        self.calls += 1
        self.go.wait(5)
        return self.answer


def test_a_fast_repo_answers_in_the_same_call():
    g = SlowGit({"branch": "master"})
    g.go.set()
    assert LiveGit(g, wait=2).state("/r") == {"branch": "master"}


def test_a_slow_cold_repo_gives_nothing_now_and_the_answer_next_poll():
    g = SlowGit({"branch": "master"})
    live = LiveGit(g, wait=0.05)
    assert live.state("/r") is None              # did not wait for git status
    g.go.set()
    fut = live._running.get("/r")
    if fut:
        fut.result(2)
    g.go.clear()
    # Next poll: refreshing again (slow), but the last answer comes straight back.
    assert live.state("/r") == {"branch": "master"}
    g.go.set()


def test_one_refresh_per_repo_at_a_time():
    g = SlowGit({"branch": "x"})
    live = LiveGit(g, wait=0.01)
    for _ in range(5):
        live.state("/r")
    assert g.calls == 1
    g.go.set()


def test_a_failed_refresh_keeps_the_last_good_answer():
    class Flaky:
        n = 0

        def for_repo(self, cwd):
            self.n += 1
            if self.n == 2:
                raise RuntimeError("index.lock")
            return {"branch": "b"} if self.n == 1 else None

    live = LiveGit(Flaky(), wait=2)
    assert live.state("/r") == {"branch": "b"}
    assert live.state("/r") == {"branch": "b"}   # failed: the last good answer
    assert live.state("/r") is None              # a real None (not a repo) stands


def test_blocking_mode_and_empty_cwd():
    g = SlowGit({"branch": "m"})
    g.go.set()
    live = LiveGit(g, wait=None)
    assert live.state("/r") == {"branch": "m"}
    assert live.state("") is None and g.calls == 1


def test_a_per_call_wait_of_zero_never_blocks_and_blocking_mode_wins():
    g = SlowGit({"branch": "z"})
    live = LiveGit(g, wait=5)
    import time
    s = time.perf_counter()
    assert live.state("/r", wait=0) is None
    assert time.perf_counter() - s < 1
    g.go.set()
    assert LiveGit(g, wait=None).state("/q", wait=0) == {"branch": "z"}


def test_the_card_list_never_waits_on_a_cold_repo(monkeypatch):
    """Outside pytest the sessions list asks with wait 0: a slow repo shows
    nothing (or its last answer) at once and its answer on the next poll."""
    import sys
    from agent_companion.server import sessions
    g = SlowGit({"branch": "main"})
    monkeypatch.delitem(sys.modules, "pytest")
    monkeypatch.setattr(sessions, "_LIVE", {})
    try:
        assert sessions._git_states(g, ["/r"]) == {"/r": None}
    finally:
        g.go.set()
    fut = sessions._LIVE[id(g)]._running.get("/r")
    if fut:
        fut.result(2)
    assert sessions._git_states(g, ["/r"]) == {"/r": {"branch": "main"}}
