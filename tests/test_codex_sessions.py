"""Live Codex sessions from a fake /proc and fixture rollouts."""
import json
import os
from pathlib import Path

from agent_companion import codex_sessions as cx

T0 = "2026-01-02T03:04:05.000Z"


def rollout(home: Path, thread: str, source="cli", records=()) -> Path:
    folder = home / "sessions" / "2026" / "01" / "02"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-01-02T03-04-05-{thread}.jsonl"
    head = {"timestamp": T0, "type": "session_meta",
            "payload": {"id": thread, "timestamp": T0, "cwd": "/work/demo", "originator": "codex-tui",
                        "source": source, "git": {"branch": "feat/x"}}}
    path.write_text("\n".join(json.dumps(r) for r in [head, *records]) + "\n")
    return path


def proc_entry(proc: Path, pid: int, argv: list[str], fds=(), env=None, start=12345):
    d = proc / str(pid)
    (d / "fd").mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in (env or {}).items()) + b"\0")
    (d / "stat").write_text(f"{pid} (co dex) S 1 " + " ".join(["0"] * 15) + f" {start} 0 0\n")
    for i, target in enumerate(fds):
        os.symlink(str(target), d / "fd" / str(i + 3))


def test_only_a_cli_session_held_by_the_native_codex_is_live(tmp_path):
    home, proc = tmp_path / "codex", tmp_path / "proc"
    mine = rollout(home, "01a1-aaaa-1041")
    job = rollout(home, "01a1-bbbb-2222", source="exec")
    proc_entry(proc, 10, ["node", "/x/bin/codex", "-p", "p"], fds=[mine], env={"TMUX_PANE": "%9"})
    proc_entry(proc, 11, ["/x/vendor/codex", "-p", "p"], fds=[tmp_path / "other.log", mine],
               env={"TMUX_PANE": "%7", "PATH": "/bin"})
    proc_entry(proc, 12, ["/x/vendor/codex", "exec"], fds=[job])
    proc_entry(proc, 13, ["/bin/bash"], fds=[mine])
    (proc / "self").mkdir()
    got = cx.live(proc=proc, home=home)
    assert [(s["pid"], s["thread_id"], s["pane"], s["cwd"], s["branch"]) for s in got] == [
        (11, "01a1-aaaa-1041", "%7", "/work/demo", "feat/x")]
    assert got[0]["rollout"] == mine
    assert got[0]["started_at"] == cx._ts(T0)


def test_no_pane_and_an_odd_pane_are_both_empty(tmp_path):
    home, proc = tmp_path / "codex", tmp_path / "proc"
    a, b = rollout(home, "t-1"), rollout(home, "t-2")
    proc_entry(proc, 20, ["/v/codex"], fds=[a])
    proc_entry(proc, 21, ["/v/codex"], fds=[b], env={"TMUX_PANE": "nope"})
    assert [s["pane"] for s in cx.live(proc=proc, home=home)] == ["", ""]


def test_a_rollout_outside_the_codex_home_is_ignored(tmp_path):
    home, proc = tmp_path / "codex", tmp_path / "proc"
    stray = rollout(tmp_path / "elsewhere", "t-3")
    proc_entry(proc, 30, ["/v/codex"], fds=[stray])
    assert cx.live(proc=proc, home=home) == []


def test_an_unreadable_rollout_is_not_a_session(tmp_path):
    home, proc = tmp_path / "codex", tmp_path / "proc"
    bad = home / "sessions" / "rollout-x-t-4.jsonl"
    bad.parent.mkdir(parents=True)
    bad.write_text("not json\n")
    proc_entry(proc, 40, ["/v/codex"], fds=[bad])
    assert cx.live(proc=proc, home=home) == []
