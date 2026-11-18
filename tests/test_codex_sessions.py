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


def rec(t, typ, payload):
    return {"timestamp": t, "type": typ, "payload": payload}


def msg(t, role, text, phase=None, kind="input_text"):
    p = {"type": "message", "id": f"m-{t}-{role}", "role": role, "content": [{"type": kind, "text": text}]}
    if phase:
        p["phase"] = phase
    return rec(t, "response_item", p)


T1, T2, T3, T4, T5 = (f"2026-01-02T03:0{i}:00.000Z" for i in range(5, 10))
TURN = [
    rec(T1, "event_msg", {"type": "task_started", "turn_id": "a"}),
    msg(T1, "developer", "house rules"),
    msg(T1, "user", "<environment_context>\n<cwd>/w</cwd>"),
    rec(T1, "turn_context", {"model": "gpt-test"}),
    msg(T1, "user", "Fix the build"),
    rec(T2, "response_item", {"type": "custom_tool_call", "name": "exec_command", "input": "pytest -q"}),
    rec(T2, "response_item", {"type": "custom_tool_call_output", "output": "ok"}),
    msg(T2, "assistant", "Looking at the log", phase="commentary", kind="output_text"),
    rec(T3, "response_item", {"type": "function_call", "name": "apply_patch", "arguments": "{}"}),
    msg(T4, "assistant", "Fixed: a missing import.", phase="final_answer", kind="output_text"),
    rec(T4, "event_msg", {"type": "task_complete", "turn_id": "a", "last_agent_message": "Fixed: a missing import."}),
]


def test_a_name_is_codex_and_the_last_four_of_its_thread():
    assert cx.name_of("01a1ed8d-7583-7b61-9c0a-1dd1d1ab1041") == "Codex-1041"


def test_the_row_reads_status_model_and_the_last_exchange(tmp_path):
    home, proc = tmp_path / "codex", tmp_path / "proc"
    path = rollout(home, "t-abcd", records=TURN[:5])
    proc_entry(proc, 50, ["/v/codex"], fds=[path], env={"TMUX_PANE": "%3"})
    reader = cx.CodexReader(live=lambda: cx.live(proc=proc, home=home))
    (row,) = reader.rows()
    assert (row["name"], row["status"], row["model"], row["last_prompt"]) == ("Codex-abcd", "busy", "gpt-test", "Fix the build")
    assert (row["backend"], row["cost_usd"], row["priced"], row["pane"]) == ("codex", None, False, "%3")
    assert (row["project"], row["session_id"], row["branch"], row["child"]) == ("demo", "t-abcd", "feat/x", False)
    with path.open("a") as fh:
        fh.write("\n".join(json.dumps(r) for r in TURN[5:]) + "\n")
    (row,) = reader.rows()
    assert (row["status"], row["last_reply"]) == ("idle", "Fixed: a missing import.")
    assert row["last_reply_at"] == cx._ts(T4) and row["last_prompt_at"] == cx._ts(T1)


def test_a_half_written_line_waits_for_its_end(tmp_path):
    home, proc = tmp_path / "codex", tmp_path / "proc"
    path = rollout(home, "t-half", records=TURN[:5])
    proc_entry(proc, 51, ["/v/codex"], fds=[path])
    reader = cx.CodexReader(live=lambda: cx.live(proc=proc, home=home))
    line = json.dumps(TURN[-1])
    with path.open("a") as fh:
        fh.write(line[:20])
    assert reader.rows()[0]["status"] == "busy"
    with path.open("a") as fh:
        fh.write(line[20:] + "\n")
    assert reader.rows()[0]["status"] == "idle"


def test_the_conversation_has_his_words_its_answers_and_the_work_between(tmp_path):
    path = rollout(tmp_path / "codex", "t-conv", records=TURN)
    got = cx.conversation(path)
    assert [m["role"] for m in got] == ["me", "work", "agent"]
    assert got[0]["text"] == "Fix the build" and got[0]["at"] == cx._ts(T1)
    assert got[1]["tools"] == ["exec_command", "apply_patch"]
    assert got[1]["last"].startswith("apply_patch")
    assert got[2]["text"] == "Fixed: a missing import."
    assert all("house rules" not in m["text"] and "environment_context" not in m["text"] for m in got)


def test_a_gone_process_drops_its_row(tmp_path):
    home, proc = tmp_path / "codex", tmp_path / "proc"
    path = rollout(home, "t-gone", records=TURN[:5])
    proc_entry(proc, 52, ["/v/codex"], fds=[path])
    reader = cx.CodexReader(live=lambda: cx.live(proc=proc, home=home))
    assert len(reader.rows()) == 1
    import shutil
    shutil.rmtree(proc / "52")
    assert reader.rows() == []


from agent_companion.reader import SessionReader


class Fixed:
    def __init__(self, rows):
        self._rows = rows

    def rows(self):
        return [dict(r) for r in self._rows]


def codex_row(cwd="/work/demo", pid=77):
    return {"pid": pid, "session_id": "t-1", "name": "Codex-0001", "cwd": cwd, "project": "demo",
            "status": "idle", "backend": "codex", "child": False, "last_reply_at": 5.0, "last_prompt_at": 4.0,
            "last_activity": 1.0, "cost_usd": None}


def test_codex_rows_join_the_snapshot(tmp_path):
    (tmp_path / "sessions").mkdir()
    got = SessionReader(tmp_path, codex=Fixed([codex_row()])).snapshot()
    assert [(r["name"], r["backend"]) for r in got] == [("Codex-0001", "codex")]
    assert "root_pid" in got[0]


def test_a_private_folder_hides_a_codex_row_too(tmp_path):
    (tmp_path / "sessions").mkdir()
    reader = SessionReader(tmp_path, hide=lambda: {"/home/other": "Their agent"},
                           codex=Fixed([codex_row(cwd="/home/other/x")]))
    assert reader.snapshot() == []


def test_a_broken_codex_reader_costs_only_its_rows(tmp_path):
    (tmp_path / "sessions").mkdir()

    class Broken:
        def rows(self):
            raise RuntimeError("boom")

    assert SessionReader(tmp_path, codex=Broken()).snapshot() == []


def test_a_test_home_never_scans_the_real_proc(tmp_path):
    (tmp_path / "sessions").mkdir()
    assert SessionReader(tmp_path)._codex is False


import subprocess
from agent_companion.server import chat


def test_a_codex_row_is_a_target_without_a_registry_file(tmp_path):
    row = codex_row() | {"pane": "%4", "rollout": "/r.jsonl"}
    got = chat.targets([row], tmp_path / "sessions")
    t = got["codex-0001"]
    assert (t["backend"], t["thread_id"], t["rollout"], t["pane"], t["pid"]) == ("codex", "t-1", "/r.jsonl", "%4", 77)


def test_queue_sends_through_codex_without_the_api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    seen = {}

    def run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw["env"]
        return subprocess.CompletedProcess(argv, 0, "Queued message m-1 for thread t-1.\n", "")

    ok, why = cx.queue({"name": "Codex-0001", "thread_id": "t-1"}, "fix it\nnow",
                       run=run, which=lambda _: "/bin/codex")
    assert (ok, why) == (True, "")
    assert seen["argv"] == ["/bin/codex", "queue", "--thread", "t-1", "--message", "fix it now"]
    assert "OPENAI_API_KEY" not in seen["env"]


def test_queue_says_why_it_failed():
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 2, "", "error: no such thread\nmore\n")

    assert cx.queue({"name": "C", "thread_id": "x"}, "hi", run=run, which=lambda _: "/bin/codex") == (
        False, "error: no such thread")
    assert cx.queue({"name": "C", "thread_id": "x"}, "hi", which=lambda _: None) == (False, "codex is not installed")

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 20)

    assert cx.queue({"name": "C", "thread_id": "x"}, "hi", run=slow, which=lambda _: "/bin/codex") == (
        False, "codex queue timed out")


def test_a_codex_chat_reads_its_rollout(monkeypatch, tmp_path):
    path = rollout(tmp_path / "codex", "t-chat", records=TURN)
    target = {"name": "Codex-chat", "pid": 9, "pane": "", "cwd": "/w", "session_id": "t-chat", "status": "idle",
              "waiting": False, "project": "demo", "backend": "codex", "rollout": str(path), "thread_id": "t-chat"}
    monkeypatch.setattr(chat, "_target", lambda name: target)
    monkeypatch.setattr(chat, "_rows", lambda: [])
    got = chat._chat_payload("Codex-chat")
    assert got["ok"] and [m["role"] for m in got["messages"]] == ["me", "work", "agent"]
    assert (got["tasks"], got["asks"], got["models"], got["efforts"]) == ([], [], [], [])


def test_a_codex_send_queues_and_is_recorded(monkeypatch):
    target = {"name": "Codex-0001", "pid": 9, "backend": "codex", "thread_id": "t-1", "status": "idle",
              "waiting": False, "project": "demo", "session_id": "t-1", "pane": "", "cwd": "/w", "rollout": "/r"}
    monkeypatch.setattr(chat, "known", lambda: {"codex-0001": target})
    sent, logged = [], []
    monkeypatch.setattr(chat.codex_sessions, "queue", lambda t, words: (sent.append(words) or (True, "")))
    monkeypatch.setattr(chat, "record", logged.append)
    assert chat._send({"to": "Codex-0001", "text": "run the tests"})["ok"] is True
    assert sent == ["run the tests"] and logged[0]["to"] == "Codex-0001" and logged[0]["ok"] is True
    got = chat._send({"to": "Codex-0001", "text": "/model gpt-x"})
    assert got["ok"] is False and "not for a Codex session" in got["detail"]
    assert sent == ["run the tests"]
