"""The stuck background-job reaper against a fixture /proc. Ages: btime 1000
and 100 ticks a second, so a process started at tick t is t / 100 s old at
boot + that; _start() gives the tick for an age at NOW."""
import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from agent_companion import bgjobs, jobreaper

NOW = 1000 + 30 * 3600
HOUR = 3600
KEEP = r"# keep-running|\bhttp\.server\b"


@pytest.fixture(autouse=True)
def ticks(monkeypatch):
    monkeypatch.setattr(bgjobs, "_clock_ticks", lambda: 100)


def _proc(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text("btime 1000\n")

    def add(pid, ppid, started, command, comm="process"):
        folder = proc / str(pid)
        folder.mkdir()
        fields = ["S", str(ppid)] + ["0"] * 17 + [str(started)]
        (folder / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + "\n")
        (folder / "cmdline").write_bytes(b"\0".join(part.encode() for part in command) + b"\0")
        (folder / "comm").write_text(comm + "\n")

    return proc, add


def _shell(command="sleep 60"):
    return ["/bin/bash", "-c",
            "source /tmp/.claude/shell-snapshots/snapshot-bash-7.sh && eval '" + command + "'"]


def _start(age_s, now=NOW):
    return int((now - 1000 - age_s) * 100)


def _session(add):
    add(1, 0, 0, ["init"], "init")
    add(100, 1, 0, ["claude", "--resume", "x"], "claude")


def test_stuck_finds_only_old_snapshot_shells_without_a_claude_parent(tmp_path):
    proc, add = _proc(tmp_path)
    add(201, 1, 100, _shell(), "bash")
    add(202, 201, 150, ["sleep", "60"])
    add(203, 1, 399900, _shell("sleep 1"), "bash")

    rows = jobreaper.stuck(str(proc), now=5000, minimum_age_s=3600)

    assert rows == [{"pid": 201, "ppid": 1, "start_ticks": 100, "age_s": 3999,
                     "cmd": "sleep 60", "why": "orphaned, over 1 h"}]


def test_reaper_never_signals_a_snapshot_shell_with_a_live_claude_owner(tmp_path):
    proc, add = _proc(tmp_path)
    add(101, 1, 1, ["claude", "--resume"], "claude")
    add(201, 101, 100, _shell(), "bash")
    sent = []

    rows = jobreaper.reap(str(proc), tmp_path / "data", now=5000, minimum_age_s=1,
                          signal_fn=lambda pid, sig: sent.append((pid, sig)), sleep_fn=lambda _: None)

    assert rows == []
    assert sent == []
    assert not (tmp_path / "data" / jobreaper.LOG_NAME).exists()


def test_kill_does_not_enter_a_descendant_live_claude_session(tmp_path):
    proc, add = _proc(tmp_path)
    add(201, 1, 100, _shell(), "bash")
    add(202, 201, 110, ["claude", "--resume"], "claude")
    add(203, 202, 120, _shell(), "bash")
    row = jobreaper.stuck(str(proc), now=5000, minimum_age_s=1)[0]
    sent = []

    jobreaper.kill(row, str(proc), signal_fn=lambda pid, sig: sent.append((pid, sig)),
                   sleep_fn=lambda _: None, now=5000, minimum_age_s=1)

    assert sent == [(201, signal.SIGTERM), (201, signal.SIGKILL)]


def test_reaper_signals_the_orphan_tree_and_writes_jsonl(tmp_path):
    proc, add = _proc(tmp_path)
    add(201, 1, 100, _shell("pytest -q"), "bash")
    add(202, 201, 150, ["sleep", "60"])
    sent = []

    rows = jobreaper.reap(str(proc), tmp_path / "data", now=5000, minimum_age_s=3600,
                          signal_fn=lambda pid, sig: sent.append((pid, sig)), sleep_fn=lambda _: None)

    assert [row["pid"] for row in rows] == [201]
    assert sent == [(202, signal.SIGTERM), (201, signal.SIGTERM),
                    (202, signal.SIGKILL), (201, signal.SIGKILL)]
    record = json.loads((tmp_path / "data" / jobreaper.LOG_NAME).read_text())
    assert record == {"at": "1970-01-01T01:23:20+00:00", "pid": 201, "ppid": 1,
                      "age_s": 3999, "why": "orphaned, over 1 h", "cmd": "pytest -q"}


def test_the_worker_is_off_until_the_owner_sets_an_interval(monkeypatch):
    for name in ("AGENT_COMPANION_JOB_REAPER_EVERY_MIN",):
        monkeypatch.delenv(name, raising=False)

    assert jobreaper.every_s() == 0, "a stranger's dev server must not vanish unasked"
    monkeypatch.setenv("AGENT_COMPANION_JOB_REAPER_EVERY_MIN", "10")
    assert jobreaper.every_s() == 600


def test_zero_interval_disables_the_worker(monkeypatch):
    monkeypatch.setattr(jobreaper.config, "get", lambda key, default=None: 0 if key == "job_reaper_every_min" else default)

    assert jobreaper.every_s() == 0
    assert jobreaper.start() is None


def test_a_job_under_a_live_session_is_stuck_only_past_max_hours(tmp_path, monkeypatch):
    proc, add = _proc(tmp_path)
    _session(add)
    add(110, 100, _start(7 * HOUR), _shell("pytest -q"), "bash")
    add(111, 100, _start(5 * HOUR), _shell("pytest -q"), "bash")

    assert jobreaper.stuck(str(proc), now=NOW) == [], "off unless job_reaper_max_hours is set"
    monkeypatch.setenv("AGENT_COMPANION_JOB_REAPER_MAX_HOURS", "6")
    assert jobreaper.stuck(str(proc), now=NOW) == [
        {"pid": 110, "ppid": 100, "start_ticks": _start(7 * HOUR), "age_s": 7 * HOUR,
         "cmd": "pytest -q", "why": "over 6 h"}]


def test_kill_stops_an_over_age_job_but_never_its_session(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_JOB_REAPER_MAX_HOURS", "6")
    proc, add = _proc(tmp_path)
    _session(add)
    add(110, 100, _start(7 * HOUR), _shell("pytest -q"), "bash")
    add(111, 110, _start(7 * HOUR), ["timeout", "10"], "timeout")
    add(112, 111, _start(7 * HOUR), ["pytest"], "pytest")
    add(113, 110, _start(7 * HOUR), ["claude", "--child"], "claude")
    row = jobreaper.stuck(str(proc), now=NOW)[0]
    sent, waits = [], []

    result = jobreaper.kill(row, str(proc), signal_fn=lambda pid, sig: sent.append((pid, sig)),
                            sleep_fn=waits.append, own_pid=99999, now=NOW)

    assert result == [112, 111, 110]
    assert sent == [(112, signal.SIGTERM), (111, signal.SIGTERM), (110, signal.SIGTERM),
                    (112, signal.SIGKILL), (111, signal.SIGKILL), (110, signal.SIGKILL)]
    assert waits == [10.0]


def test_kill_judges_again_and_leaves_a_job_that_is_no_longer_stuck(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_JOB_REAPER_MAX_HOURS", "6")
    proc, add = _proc(tmp_path)
    _session(add)
    add(110, 100, _start(7 * HOUR), _shell("pytest -q"), "bash")
    row = jobreaper.stuck(str(proc), now=NOW)[0]
    monkeypatch.delenv("AGENT_COMPANION_JOB_REAPER_MAX_HOURS")
    sent = []

    assert jobreaper.kill(row, str(proc), signal_fn=lambda pid, sig: sent.append((pid, sig)),
                          sleep_fn=lambda _: None, now=NOW) == []
    assert sent == []


def test_after_the_grace_every_survivor_of_the_tree_is_forced(tmp_path):
    # The shell dies on SIGTERM; a child that ignored it is still forced, by
    # its own identity, rather than left running because its parent is gone.
    proc, add = _proc(tmp_path)
    add(201, 1, 100, _shell("pytest -q"), "bash")
    add(202, 201, 150, ["pytest"], "pytest")
    row = jobreaper.stuck(str(proc), now=5000, minimum_age_s=3600)[0]
    sent = []

    def term(pid, sig):
        sent.append((pid, sig))
        if (pid, sig) == (201, signal.SIGTERM):
            (proc / "201" / "stat").unlink()

    jobreaper.kill(row, str(proc), signal_fn=term, sleep_fn=lambda _: None, now=5000, minimum_age_s=3600)

    assert sent == [(202, signal.SIGTERM), (201, signal.SIGTERM), (202, signal.SIGKILL)]


def test_kept_commands_are_never_stuck(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_JOB_REAPER_MAX_HOURS", "6")
    monkeypatch.setenv("AGENT_COMPANION_JOB_REAPER_KEEP", KEEP)
    proc, add = _proc(tmp_path)
    _session(add)
    add(110, 100, _start(7 * HOUR), _shell("python3 -m HTTP.server 8000"), "bash")
    add(120, 1, _start(2 * HOUR), _shell("sleep 1 # keep-running"), "bash")
    # matched before redaction: the label is cut at -ppw, the marker still counts
    add(130, 1, _start(2 * HOUR), _shell("mysql -ppw db # keep-running"), "bash")
    add(140, 1, _start(2 * HOUR), _shell("pytest -q"), "bash")

    assert [row["pid"] for row in jobreaper.stuck(str(proc), now=NOW)] == [140]


def test_a_keep_pattern_that_cannot_be_read_reaps_nothing_and_says_why(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_COMPANION_JOB_REAPER_KEEP", "(")
    proc, add = _proc(tmp_path)
    add(201, 1, 100, _shell("pytest -q"), "bash")
    sent = []

    assert jobreaper.stuck(str(proc), now=5000, minimum_age_s=1) == []
    assert jobreaper.reap(str(proc), tmp_path / "data", now=5000, minimum_age_s=1,
                          signal_fn=lambda pid, sig: sent.append(pid), sleep_fn=lambda _: None) == []
    assert sent == []
    assert jobreaper.problem().startswith("job_reaper_keep is not a valid pattern")
    assert jobreaper.main([]) == 0
    assert "job_reaper_keep is not a valid pattern" in capsys.readouterr().out


def test_a_shell_inside_a_job_is_judged_with_that_job(tmp_path, monkeypatch):
    # `heavy --enter` starts a second snapshot shell under the first: a 2 h
    # test run is one 2 h job under a live session, not three, and no orphan.
    monkeypatch.setenv("AGENT_COMPANION_JOB_REAPER_MAX_HOURS", "6")
    proc, add = _proc(tmp_path)
    _session(add)
    add(110, 100, _start(2 * HOUR), _shell("heavy --enter; pytest -q"), "bash")
    add(120, 110, _start(2 * HOUR), _shell("heavy --enter; pytest -q"), "bash")
    add(121, 120, _start(2 * HOUR), _shell("heavy --enter; pytest -q"), "bash")

    assert jobreaper.stuck(str(proc), now=NOW) == []


def test_a_child_of_claude_that_is_no_job_is_never_stuck(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_JOB_REAPER_MAX_HOURS", "6")
    proc, add = _proc(tmp_path)
    _session(add)
    add(110, 100, _start(30 * HOUR), ["python3", "/m/mcp/server.py"], "python3")

    assert jobreaper.stuck(str(proc), now=NOW) == []


@pytest.mark.parametrize("raw, seconds", [
    ("6", 6 * HOUR), (6, 6 * HOUR), ("1.5", 5400), ("0", 0), ("-1", 0),
    ("soon", 0), (True, 0), ("inf", 0), ("nan", 0),
])
def test_max_hours_is_a_positive_number_or_off(monkeypatch, raw, seconds):
    monkeypatch.setattr(jobreaper.config, "get",
                        lambda key, default=None: raw if key == "job_reaper_max_hours" else default)
    assert jobreaper.max_s() == seconds


def test_a_dry_run_lists_what_would_go_and_signals_nothing(monkeypatch, capsys):
    rows = [{"pid": 110, "ppid": 100, "start_ticks": 1, "age_s": 7 * HOUR,
             "cmd": "pytest -q", "why": "over 6 h"}]
    calls = []
    monkeypatch.setattr(jobreaper, "stuck", lambda *a, **k: rows)
    monkeypatch.setattr(jobreaper, "kill", lambda *a, **k: calls.append("kill"))
    monkeypatch.setattr(jobreaper, "reap", lambda *a, **k: calls.append("reap"))

    assert jobreaper.main(["--dry-run"]) == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "would kill pid=110 age=7.0h why=over 6 h cmd=pytest -q" in out
    assert "1 stuck job(s), dry run" in out


def test_a_pass_from_the_command_line_reaps_and_says_how_many(monkeypatch, capsys):
    monkeypatch.setattr(jobreaper, "reap", lambda *a, **k: [{"pid": 1}, {"pid": 2}])
    assert jobreaper.main([]) == 0
    assert "killed 2 stuck job(s)" in capsys.readouterr().out


def test_unknown_arguments_reap_nothing(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(jobreaper, "reap", lambda *a, **k: calls.append("reap"))
    assert jobreaper.main(["--force"]) == 0
    assert calls == [] and "unsupported arguments" in capsys.readouterr().out


def test_it_runs_as_a_module():
    out = subprocess.run([sys.executable, "-m", "agent_companion.jobreaper", "--help"],
                         cwd=Path(__file__).resolve().parent.parent,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0
    assert out.stdout.strip() == "usage: python3 -m agent_companion.jobreaper [--dry-run]"


WORKERS = r"^node \(vitest|jest-worker"


def test_an_orphaned_test_worker_is_stuck_past_its_minutes_and_only_then(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_JOB_REAPER_ORPHANS", WORKERS)
    monkeypatch.setenv("AGENT_COMPANION_JOB_REAPER_ORPHAN_MIN", "15")
    proc, add = _proc(tmp_path)
    _session(add)
    add(300, 1, _start(3 * HOUR), ["node (vitest 25)"], "node")          # orphaned, old: stuck
    add(301, 1, _start(5 * 60), ["node (vitest 3)"], "node")             # orphaned, young
    add(302, 100, _start(3 * HOUR), ["node (vitest 4)"], "node")         # its runner (here claude) lives
    add(303, 1, _start(3 * HOUR), ["node", "server.js"], "node")         # a daemon, no match
    add(304, 1, _start(3 * HOUR), ["node (vitest 9) # keep-running"], "node")
    monkeypatch.setenv("AGENT_COMPANION_JOB_REAPER_KEEP", KEEP)
    rows = jobreaper.stuck(str(proc), now=NOW)
    assert [(r["pid"], r["why"]) for r in rows] == [(300, "orphaned worker, over 15 min")]


def test_orphaned_workers_are_left_alone_until_the_owner_names_them(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_COMPANION_JOB_REAPER_ORPHANS", raising=False)
    proc, add = _proc(tmp_path)
    add(300, 1, _start(3 * HOUR), ["node (vitest 25)"], "node")
    assert jobreaper.stuck(str(proc), now=NOW) == []


def test_an_orphaned_worker_and_its_children_are_reaped_and_logged(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_JOB_REAPER_ORPHANS", WORKERS)
    proc, add = _proc(tmp_path)
    add(300, 1, _start(HOUR), ["node (vitest 25)"], "node")
    add(301, 300, _start(HOUR), ["esbuild", "--service"], "esbuild")
    sent = []
    rows = jobreaper.reap(str(proc), tmp_path / "data", now=NOW,
                          signal_fn=lambda pid, sig: sent.append((pid, sig)), sleep_fn=lambda _: None)
    assert [r["pid"] for r in rows] == [300]
    assert sent == [(301, signal.SIGTERM), (300, signal.SIGTERM), (301, signal.SIGKILL), (300, signal.SIGKILL)]
    assert json.loads((tmp_path / "data" / jobreaper.LOG_NAME).read_text())["why"] == "orphaned worker, over 15 min"


def test_an_orphans_pattern_that_cannot_be_read_reaps_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_JOB_REAPER_ORPHANS", "(")
    proc, add = _proc(tmp_path)
    add(201, 1, 100, _shell(), "bash")
    assert jobreaper.stuck(str(proc), now=5000, minimum_age_s=1) == []
    assert "job_reaper_orphans" in jobreaper.problem()
