"""Chat (agent_companion/server/chat.py): the owner directs sessions from the
page. Nothing here touches tmux: every run is a fake."""
import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agent_companion import server  # noqa: E402
from agent_companion.server import asks, chat  # noqa: E402

TOKEN = "t" * 32
OWNER = "o" * 64
KEY = {"x-agent-companion-key": OWNER}
SAME = {"origin": "http://testserver", "host": "testserver"}
MINE = {**SAME, **KEY}
KNOWN = {"ada": {"name": "Ada", "pid": 11, "pane": "%3", "cwd": "/w/proj", "session_id": "s1",
                 "status": "idle", "waiting": False, "project": "proj"},
         "bo": {"name": "Bo", "pid": 12, "pane": "%2", "cwd": "/w/other", "session_id": "s2",
                "status": "busy", "waiting": False, "project": "other"}}


def test_addressed_messages_go_to_that_session_without_the_name():
    assert chat.route("Ada, push it", KNOWN) == ("Ada", "push it")
    assert chat.route("@ada push it", KNOWN) == ("Ada", "push it")
    assert chat.route("Ada: status?", KNOWN) == ("Ada", "status?")


def test_everything_else_has_no_addressee():
    assert chat.route("what's everyone doing?", KNOWN) == ("", "what's everyone doing?")
    assert chat.route("Ada said it shipped, check", KNOWN) == ("", "Ada said it shipped, check")
    assert chat.route("Zed, hello", KNOWN) == ("", "Zed, hello"), "an unknown name is not a target"


def test_targets_come_from_the_registry_panes_and_name_by_speaker_then_session(tmp_path):
    (tmp_path / "11.json").write_text(json.dumps({"tmux": "w:@4.%3", "sessionId": "s1"}))
    (tmp_path / "13.json").write_text(json.dumps({"tmux": ""}))
    (tmp_path / "16.json").write_text(json.dumps({"tmux": "w:@4.%7"}))
    rows = [{"speaker": "Ada", "pid": 11, "cwd": "/w", "status": "idle"},
            {"speaker": "Wren", "pid": 13}, {"speaker": "x", "pid": 14, "child": True}, {"pid": 15},
            {"speaker": "", "name": "plain-8f", "pid": 16}]
    got = chat.targets(rows, tmp_path)
    assert sorted(got) == ["ada", "plain-8f"] and got["ada"]["pane"] == "%3"


class Run:
    def __init__(self, pane_pid="11"):
        self.calls, self.pane_pid = [], pane_pid

    def __call__(self, args, **kw):
        self.calls.append(args)
        class R:
            stdout = self.pane_pid + "\n"
        return R()


_RULE = "\x1b[38;5;244m" + "─" * 40 + "\x1b[39m"


def _box(*rows):
    return "\n".join(["x", _RULE, "\x1b[39m❯ " + rows[0], *rows[1:], _RULE, "  status line"])


def test_send_types_one_line_then_enter(monkeypatch):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])   # pgrp == tpgid
    run = Run()
    ok, why = chat.send(KNOWN["ada"], "push it\nplease", run=run, sleep=lambda s: None)
    assert ok, why
    keys = [c for c in run.calls if "send-keys" in c]
    assert keys == [["tmux", "send-keys", "-t", "%3", "-l", "--", "push it please"],
                    ["tmux", "send-keys", "-t", "%3", "Enter"]]


def test_parse_input_reads_what_sits_in_the_box():
    assert chat.parse_input(_box("")) == ""
    assert chat.parse_input(_box("board posts about the", "  updates", "  /clear")) == "board posts about the  updates  /clear"
    assert chat.parse_input("\n".join([_RULE, "\x1b[39m❯  \x1b[2mpush it\x1b[0m", _RULE])) == " "
    assert chat.parse_input("no box here") is None


class Screen(Run):
    """tmux whose input box keeps the words for the first `stuck` captures."""
    def __init__(self, words, stuck):
        super().__init__()
        self.words, self.stuck, self.seen, self.slept = words, stuck, 0, []

    def __call__(self, args, **kw):
        if "capture-pane" not in args:
            return super().__call__(args, **kw)
        self.calls.append(args)
        self.seen += 1
        text = _box(self.words if self.seen <= self.stuck else "")
        class R:
            stdout = text
        return R()


def test_an_enter_that_became_a_newline_is_pressed_again(monkeypatch):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    run = Screen("two board posts", stuck=1)
    ok, why = chat.send(KNOWN["ada"], "two board posts", run=run, sleep=run.slept.append)
    assert ok, why
    assert [c[-1] for c in run.calls if "send-keys" in c] == ["two board posts", "Enter", "Enter"]
    assert run.slept[0] == chat.SUBMIT_GAP_S


def test_words_that_never_submit_say_so(monkeypatch):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    run = Screen("two board posts", stuck=99)
    ok, why = chat.send(KNOWN["ada"], "two board posts", run=run, sleep=lambda s: None)
    assert not ok and "still in its box" in why
    assert sum(c[-1] == "Enter" for c in run.calls) == 1 + chat.SUBMIT_RETRIES


def test_words_clear_after_five_looks_are_sent(monkeypatch):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    run = Screen("two board posts", stuck=5)
    ok, why = chat.send(KNOWN["ada"], "two board posts", run=run, sleep=lambda s: None)
    assert ok, why
    assert sum(c[-1] == "Enter" for c in run.calls) == 1 + chat.SUBMIT_RETRIES


def test_words_that_never_clear_wait_for_every_look(monkeypatch):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    looks = int(chat.SUBMIT_WAIT_S / chat.SUBMIT_CHECK_S)
    run = Screen("two board posts", stuck=looks)
    ok, why = chat.send(KNOWN["ada"], "two board posts", run=run, sleep=lambda s: None)
    assert not ok and "still in its box" in why
    assert run.seen == looks
    assert sum(c[-1] == "Enter" for c in run.calls) == 1 + chat.SUBMIT_RETRIES


def test_nothing_is_typed_when_the_session_is_not_in_front(monkeypatch):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "99"])   # someone else in front
    run = Run()
    ok, why = chat.send(KNOWN["ada"], "push it", run=run)
    assert not ok and "in front" in why and not any("send-keys" in c for c in run.calls)


def test_nothing_is_typed_into_a_prompt_or_a_dialog():
    for t in ({**KNOWN["ada"], "waiting": True}, {**KNOWN["ada"], "status": "waiting"}):
        run = Run()
        ok, why = chat.send(t, "push it", run=run)
        assert not ok and "waiting on your answer" in why and not any("send-keys" in c for c in run.calls)


def test_stop_is_escape_into_its_own_window_only(monkeypatch):
    calls = []
    monkeypatch.setattr(chat, "in_front", lambda pid, pane, run=None: (True, ""))
    ok, _ = chat.stop(KNOWN["ada"], run=lambda a, **k: calls.append(a))
    assert ok and calls == [["tmux", "send-keys", "-t", "%3", "Escape"]]
    monkeypatch.setattr(chat, "in_front", lambda pid, pane, run=None: (False, "something else is in front"))
    calls.clear()
    ok, why = chat.stop(KNOWN["ada"], run=lambda a, **k: calls.append(a))
    assert not ok and not calls and "in front" in why


def test_the_thread_is_the_owners_sends_and_their_replies(tmp_path):
    log = tmp_path / "chat.jsonl"
    chat.record({"at": 1000.0, "to": "Ada", "text": "push it", "ok": True}, log)
    d = tmp_path / "projects" / "-w-proj"
    d.mkdir(parents=True)
    (d / "s1.jsonl").write_text("\n".join(json.dumps(x) for x in [
        {"type": "assistant", "timestamp": "1970-01-01T00:16:30Z", "uuid": "a",
         "message": {"content": [{"type": "text", "text": "before"}]}},
        {"type": "assistant", "timestamp": "1970-01-01T00:16:50Z", "uuid": "b",
         "message": {"content": [{"type": "text", "text": "Pushed."}, {"type": "tool_use"}]}}]) + "\n")
    got = chat.thread(KNOWN, tmp_path / "projects", path=log)
    assert [(m["role"], m["text"]) for m in got] == [("me", "push it"), ("agent", "Pushed.")]
    assert oct(log.stat().st_mode & 0o777) == "0o600"


def _lines(tmp_path, items):
    f = tmp_path / "s.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in items) + "\n")
    return f


def test_one_sessions_window_is_prompts_replies_and_work_between(tmp_path):
    f = _lines(tmp_path, [
        {"type": "user", "timestamp": "2026-01-01T00:00:00Z", "uuid": "u1", "message": {"content": "do it"}},
        {"type": "assistant", "timestamp": "2026-01-01T00:00:01Z", "uuid": "a1", "message": {
            "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}]}},
        {"type": "assistant", "timestamp": "2026-01-01T00:00:02Z", "uuid": "a2", "message": {
            "model": "m", "content": [{"type": "text", "text": "Done."}]}},
        {"type": "user", "timestamp": "2026-01-01T00:00:03Z", "isMeta": True, "message": {"content": "hidden"}},
    ])
    got = chat.conversation(f)
    assert [(m["role"], m["text"]) for m in got] == [("me", "do it"), ("work", ""), ("agent", "Done.")]
    assert got[1]["tools"] == ["Bash"] and got[1]["last"] == "Bash: ls -la"


def test_a_question_is_in_the_window_with_its_choices_and_the_pick(tmp_path):
    q = {"questions": [{"question": "Which nav?", "options": [{"label": "Five"}, {"label": "Leave"}]}]}
    f = _lines(tmp_path, [
        {"type": "assistant", "timestamp": "2026-01-01T00:00:01Z", "message": {
            "content": [{"type": "tool_use", "id": "t1", "name": "AskUserQuestion", "input": q}]}},
        {"type": "user", "timestamp": "2026-01-01T00:00:05Z", "toolUseResult": {"answers": {"Which nav?": "Five"}},
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
    ])
    (m,) = chat.conversation(f)
    assert m["role"] == "asked" and m["options"] == ["Five", "Leave"] and m["answer"] == "Five" and m["closed"]
    assert m["answered_at"] - m["at"] == 4                 # answered at 00:05, asked at 00:01


def test_a_message_sent_while_it_works_is_in_the_window(tmp_path):
    f = _lines(tmp_path, [{"type": "attachment", "timestamp": "2026-01-01T00:00:01Z", "uuid": "q1", "attachment": {
        "type": "queued_command", "commandMode": "prompt", "origin": {"kind": "human"}, "prompt": "also this"}},
        {"type": "attachment", "timestamp": "2026-01-01T00:00:02Z", "attachment": {
            "type": "queued_command", "commandMode": "prompt", "origin": {"kind": "peer"}, "prompt": "not the owner"}}])
    assert [(m["role"], m["text"]) for m in chat.conversation(f)] == [("me", "also this")]


def test_conversation_reads_further_back_when_the_tail_is_short(tmp_path):
    items = [{"type": "user", "timestamp": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z", "uuid": f"u{i}",
              "message": {"content": "hello " + "x" * 600}} for i in range(40)]
    f = _lines(tmp_path, items)
    assert len(chat._conversation(f, 40, 5000)) < 40
    assert len(chat.conversation(f, 40, 5000)) == 40


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_COMPANION_CHAT_LOG", str(tmp_path / "chat.jsonl"))
    monkeypatch.setenv("AGENT_COMPANION_ASKS", str(tmp_path / "asks"))
    monkeypatch.setenv("AGENT_COMPANION_CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setattr(server, "FEATURES", ["chat"])
    monkeypatch.setattr(chat, "known", lambda: KNOWN)
    c = TestClient(server.create_app(token=TOKEN, owner_key=OWNER))
    c.sent = []
    monkeypatch.setattr(chat, "send", lambda t, w, run=None: c.sent.append((t["name"], w)) or (True, ""))
    return c


def test_the_feature_loads_titled_chat(client):
    assert client.get("/api/features").json() == [{"id": "chat", "title": "Chat"}]


def test_reads_need_the_token(client):
    assert client.get("/api/chat").status_code == 401
    assert client.get("/api/chat/window/Ada").status_code == 401
    assert [a["name"] for a in client.get("/api/chat", headers=KEY).json()["agents"]] == ["Ada", "Bo"]


def test_the_send_route_needs_the_key_json_and_the_page(client, tmp_path):
    url = f"/api/chat/send"
    assert client.post("/api/chat/send", json={"text": "Ada, push it"}, headers=SAME).status_code == 401
    assert client.post(url, json={"text": "Ada, push it"}, headers=KEY).status_code == 403, "not from the page"
    assert client.post(url, content=b'{"text":"x"}', headers=MINE).status_code == 415
    r = client.post(url, json={"text": "Ada, push it"}, headers=MINE)
    assert r.json() == {"ok": True, "to": "Ada", "detail": ""} and client.sent == [("Ada", "push it")]
    log = [json.loads(x) for x in (tmp_path / "chat.jsonl").read_text().splitlines()]
    assert len(log) == 1 and log[0]["to"] == "Ada" and log[0]["text"] == "push it" and log[0]["ok"] is True


def test_the_picker_names_who_unless_the_words_name_someone(client):
    h = MINE
    url = f"/api/chat/send"
    client.post(url, json={"to": "Ada", "text": "push it"}, headers=h)
    client.post(url, json={"to": "Ada", "text": "Bo, what's up?"}, headers=h)
    assert client.sent == [("Ada", "push it"), ("Bo", "what's up?")]
    assert client.post(url, json={"text": "no one named"}, headers=h).status_code == 400
    assert client.post(url, json={"text": "  "}, headers=h).status_code == 400


def test_a_refused_send_is_logged_with_its_reason(client, tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "send", lambda t, w, run=None: (False, "something else is in front"))
    r = client.post(f"/api/chat/send", json={"to": "Ada", "text": "hi"}, headers=MINE)
    assert r.json()["ok"] is False
    assert json.loads((tmp_path / "chat.jsonl").read_text())["detail"] == "something else is in front"


def test_stop_is_a_guarded_write(client, monkeypatch):
    calls = []
    monkeypatch.setattr(chat, "stop", lambda t, run=None: calls.append(t["name"]) or (True, ""))
    assert client.post("/api/chat/stop/Ada", json={}, headers=KEY).status_code == 403
    assert client.post(f"/api/chat/stop/Ada", json={}, headers=MINE).json()["ok"] is True
    assert client.post(f"/api/chat/stop/Nobody", json={}, headers=MINE).status_code == 404
    assert calls == ["Ada"]


def test_a_window_carries_that_sessions_pending_asks(client, tmp_path):
    asks.create("Ada", "proj", "s1", "Bash", {"command": "ls"})
    asks.create("Bo", "other", "s2", "Bash", {"command": "pwd"})
    w = client.get("/api/chat/window/Ada", headers=KEY).json()
    assert w["name"] == "Ada" and [a["what"] for a in w["asks"]] == ["ls"]
    assert client.get("/api/chat/window/Zed", headers=KEY).status_code == 404


def test_transcript_path_names_the_folder_like_claude_code(tmp_path):
    t = {"cwd": "/home/t/my_proj/.wt", "session_id": "abc"}
    assert chat.transcript_path(t, tmp_path) == tmp_path / "-home-t-my-proj--wt" / "abc.jsonl"


def test_chat_reads_the_same_claude_home_key_as_sessions(monkeypatch, tmp_path):
    from agent_companion.server import sessions
    monkeypatch.setenv("AGENT_COMPANION_CLAUDE_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "other"))
    assert chat.claude_home() == sessions.claude_home() == tmp_path / "h"
