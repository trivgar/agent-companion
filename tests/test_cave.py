"""The full direct-chat helpers behind the /api/cave contract."""
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from agent_companion.server import chat


KNOWN = {"ada": {"name": "Ada", "pid": 11, "pane": "%3", "cwd": "/w/proj", "session_id": "s1",
                 "status": "idle", "waiting": False, "project": "proj"}}


class Run:
    def __init__(self, pane_pid="11"):
        self.calls, self.pane_pid = [], pane_pid

    def __call__(self, args, **kwargs):
        self.calls.append(args)

        class Result:
            stdout = self.pane_pid + "\n"
        return Result()


_RULE = "\x1b[38;5;244m" + "─" * 40 + "\x1b[39m"
_DIM_SUGGESTION = "\n".join([_RULE, "\x1b[39m❯\u00a0 \x1b[2mReview the change\x1b[0m", _RULE])


def _box(*rows):
    return "\n".join(["x", _RULE, "\x1b[39m❯\u00a0" + rows[0], *rows[1:], _RULE, "status"])


def _sent_line(files, caption="", cwd="/work/proj"):
    return {"type": "assistant", "timestamp": "2026-01-01T00:00:00Z", "uuid": "a1", "cwd": cwd,
            "message": {"content": [{"type": "tool_use", "name": "SendUserFile",
                                        "input": {"files": files, "caption": caption}}]}}


def test_an_optional_host_receives_unaddressed_words():
    assert chat.route("please check", KNOWN, "Ada") == ("Ada", "please check")
    assert chat.route("Ada: please check", KNOWN, "Ada") == ("Ada", "please check")
    assert chat.route("please check", KNOWN) == ("", "please check")


def test_suggestion_reads_only_a_dim_claude_input(monkeypatch):
    assert chat.parse_suggestion(_DIM_SUGGESTION) == "Review the change"
    assert chat.parse_suggestion("\n".join([_RULE, "❯\u00a0 Review the change", _RULE])) is None
    monkeypatch.setattr(chat, "in_front", lambda *args, **kwargs: (True, ""))
    chat._SUGGESTION_CACHE.clear()

    class Screen(Run):
        def __call__(self, args, **kwargs):
            self.calls.append(args)

            class Result:
                stdout = _DIM_SUGGESTION
            return Result()

    run = Screen()
    assert chat.suggestion(KNOWN["ada"], run=run) == "Review the change"
    assert run.calls == [["tmux", "capture-pane", "-e", "-p", "-t", "%3"]]


def test_suggestion_never_reads_a_busy_waiting_or_other_pane(monkeypatch):
    calls = []
    chat._SUGGESTION_CACHE.clear()
    monkeypatch.setattr(chat, "in_front", lambda *args, **kwargs: calls.append(args) or (False, "gone"))
    assert chat.suggestion({**KNOWN["ada"], "status": "busy"}, run=Run()) is None
    assert chat.suggestion({**KNOWN["ada"], "waiting": True}, run=Run()) is None
    assert chat.suggestion(KNOWN["ada"], run=Run()) is None
    assert len(calls) == 1


def test_seen_state_is_private_and_only_new_idle_replies_are_unread(tmp_path):
    path = tmp_path / "seen.json"
    row = {"session_id": "s1", "status": "idle", "last_reply_at": 100.0, "last_prompt_at": 50.0}
    assert chat.unread(row, chat.seen(path))
    chat.mark_seen("s1", at=120.0, path=path)
    assert not chat.unread(row, chat.seen(path))
    assert chat.unread(dict(row, last_reply_at=130.0), chat.seen(path))
    assert not chat.unread(dict(row, status="busy"), {})
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_an_attachment_is_private_and_found_only_by_its_id(tmp_path):
    path = chat.save_upload(b"image", "image/png", root=tmp_path, now=1)
    assert path.suffix == ".png" and path.read_bytes() == b"image"
    assert chat.upload_path(path.name, root=tmp_path) == path
    assert chat.upload_path("../x.png", root=tmp_path) is None
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(ValueError):
        chat.save_upload(b"x", "text/html", root=tmp_path)


def test_delivered_files_hide_the_path_and_stay_with_its_transcript(tmp_path):
    real = tmp_path / "out.png"
    real.write_bytes(b"png")
    transcript = tmp_path / "a.jsonl"
    transcript.write_text(json.dumps(_sent_line([str(real)], "Here it is")) + "\n")
    messages = chat.conversation(transcript)
    assert messages[0]["files"][0]["name"] == "out.png"
    assert str(tmp_path) not in json.dumps(messages)
    file_id = messages[0]["files"][0]["id"]
    assert chat.sent_file(transcript, file_id) == real
    assert chat.sent_file(transcript, "../etc/passwd") is None


def test_delivered_file_rejects_links_directories_and_large_files(tmp_path, monkeypatch):
    safe = tmp_path / "safe.png"
    safe.write_bytes(b"ok")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"secret")
    link = tmp_path / "link.png"
    link.symlink_to(outside)
    directory = tmp_path / "dir"
    directory.mkdir()
    monkeypatch.setattr(chat, "MAX_SENT", 1)
    assert chat.sent_file_ok(link) is None
    assert chat.sent_file_ok(directory) is None
    assert chat.sent_file_ok(safe) is None


def test_conversation_includes_spoken_words_and_delivered_files(tmp_path):
    transcript = tmp_path / "a.jsonl"
    rows = [
        {"type": "assistant", "timestamp": "2026-01-01T00:00:00Z", "uuid": "a1", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            {"type": "tool_use", "name": "mcp__comms__text_to_speech", "input": {"text": "Done."}}]}},
        _sent_line(["/missing.txt"], "A file"),
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    got = chat.conversation(transcript)
    assert [(item["role"], item["text"]) for item in got] == [("work", ""), ("spoke", "Done."), ("agent", "A file")]


def test_effort_reads_the_latest_real_assistant_entry(tmp_path):
    transcript = tmp_path / "a.jsonl"
    lines = [
        {"type": "assistant", "effort": "low", "message": {"content": []}},
        {"type": "assistant", "isSidechain": True, "effort": "high", "message": {"content": []}},
        {"type": "assistant", "effort": "medium", "message": {"content": []}},
    ]
    transcript.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    assert chat.effort(transcript) == "medium"


def test_cleared_unprompted_requires_no_later_human_message(tmp_path):
    transcript = tmp_path / "a.jsonl"
    clear = {"type": "user", "message": {"content": "<command-name>/clear</command-name>"}}
    transcript.write_text(json.dumps(clear) + "\n")
    assert chat.cleared_unprompted(transcript)
    later = {"type": "user", "message": {"content": "continue"}}
    transcript.write_text(json.dumps(clear) + "\n" + json.dumps(later) + "\n")
    assert not chat.cleared_unprompted(transcript)


def test_effort_confirmation_only_accepts_its_matching_dialog(monkeypatch):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    dialog = "Change effort level?\n❯ 1. Yes, switch to max\n"

    class Pane(Run):
        def __call__(self, args, **kwargs):
            self.calls.append(args)

            class Result:
                stdout = dialog if "capture-pane" in args else "11\n"
            return Result()

    run = Pane()
    assert chat.confirm_effort(KNOWN["ada"], "/effort max", run=run, sleep=lambda _: None)
    assert run.calls[-1] == ["tmux", "send-keys", "-t", "%3", "Enter"]
    assert not chat.confirm_effort(KNOWN["ada"], "/model a", run=Pane(), sleep=lambda _: None)


def test_model_confirmation_only_accepts_the_switch_model_dialog(monkeypatch):
    # The picker's /model send can raise "Switch model?" (the cache warning),
    # which no hook reports, so the phone never showed it (2026-11-09).
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    screens = {"now": "   Switch model?\n   ❯ 1. Yes, switch to Fable 5.2\n     2. No, go back\n"}

    class Pane(Run):
        def __call__(self, args, **kwargs):
            self.calls.append(args)

            class Result:
                stdout = screens["now"] if "capture-pane" in args else "11\n"
            return Result()

    run = Pane()
    assert chat.confirm_model(KNOWN["ada"], "/model claude-fable-5-2", run=run, sleep=lambda _: None)
    assert run.calls[-1] == ["tmux", "send-keys", "-t", "%3", "Enter"]
    assert not chat.confirm_model(KNOWN["ada"], "/effort max", run=Pane(), sleep=lambda _: None)
    assert not chat.confirm_model(KNOWN["ada"], "/model x; rm -rf ~", run=Pane(), sleep=lambda _: None)
    screens["now"] = "Change effort level?\n❯ 1. Yes, switch to max\n"
    other = Pane()
    assert not chat.confirm_model(KNOWN["ada"], "/model opus", run=other, sleep=lambda _: None)
    assert not any("send-keys" in c for c in other.calls)


def test_quick_replies_are_drafts_not_sends():
    assert chat.quick_replies("Ready to push it?") == [{"action": "say-push", "title": "Push", "words": "push"}]
    assert chat.quick_replies("Want me to update it?") == [{"action": "say-yes", "title": "Yes", "words": "yes"}]
    assert chat.quick_replies("Completed.") == []


def test_cave_ask_routes_delegate_to_the_core_ask_handlers(monkeypatch):
    decided, answered = [], []
    monkeypatch.setattr(chat.asks, "asks_decide", lambda ask_id, body: decided.append((ask_id, body)) or {"ok": True})
    monkeypatch.setattr(chat.asks, "asks_answer", lambda ask_id, body: answered.append((ask_id, body)) or {"ok": True})
    assert chat.cave_ask_decide("a1", {"decision": "deny"}) == {"ok": True}
    assert chat.cave_ask_answer("a2", {"answers": {"Continue?": "No"}}) == {"ok": True}
    assert decided == [("a1", {"decision": "deny"})]
    assert answered == [("a2", {"answers": {"Continue?": "No"}})]


def test_suggest_route_uses_a_fresh_server_read_and_the_card_identity(monkeypatch):
    target = KNOWN["ada"]
    monkeypatch.setattr(chat, "_target", lambda name: target if name.lower() == "ada" else None)
    monkeypatch.setattr(chat, "suggestion", lambda found, fresh=False: "Review  the\nchange")
    sent = []
    monkeypatch.setattr(chat, "send", lambda found, words: sent.append((found, words)) or (True, ""))
    logged = []
    monkeypatch.setattr(chat, "record", logged.append)
    body = {"pid": 11, "session_id": "s1", "words": "Review the change"}
    assert json.loads(chat.cave_suggest("Ada", body).body) == {"ok": True, "detail": ""}
    assert sent == [(target, "Review  the\nchange")]
    assert logged[-1]["text"] == "[suggested] Review  the\nchange"
    changed = dict(body, words="Different")
    assert chat.cave_suggest("Ada", changed).status_code == 409
    assert chat.cave_suggest("Ada", {"pid": 99, "session_id": "s1", "words": "Review the change"}).status_code == 409


@pytest.mark.parametrize(("action", "body"), [
    ("resume", {"pid": 99, "session_id": "s1"}),
    ("end", {"pid": 99, "session_id": "s1"}),
    ("suggest", {"pid": 99, "session_id": "s1", "words": "Review the change"}),
])
def test_guarded_card_actions_reject_a_stale_pid_without_sending(monkeypatch, action, body):
    target = KNOWN["ada"]
    monkeypatch.setattr(chat, "_target", lambda name: target if name.lower() == "ada" else None)
    sent = []
    monkeypatch.setattr(chat, "send", lambda found, words: sent.append((found, words)) or (True, ""))

    response = getattr(chat, f"cave_{action}")("Ada", body)

    assert response.status_code == 409
    assert sent == []


def test_cave_addresses_keep_the_same_owner_and_write_guards(monkeypatch, tmp_path):
    """Route coverage is kept even though this sandbox does not run clients."""
    from fastapi.testclient import TestClient
    from agent_companion import server
    from agent_companion.server import auth

    owner = "o" * 64
    monkeypatch.setenv("AGENT_COMPANION_CHAT_LOG", str(tmp_path / "chat.jsonl"))
    monkeypatch.setenv("AGENT_COMPANION_CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setenv("AGENT_COMPANION_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("AGENT_COMPANION_OWNER_KEYS", str(tmp_path / "owner-keys.json"))
    monkeypatch.setattr(server, "FEATURES", ["chat"])
    monkeypatch.setattr(chat, "known", lambda: KNOWN)
    monkeypatch.setattr(chat, "send", lambda target, text: (True, ""))
    client = TestClient(server.create_app(token="t" * 32, owner_key=owner))
    same = {"origin": "http://testserver", "host": "testserver", auth.OWNER_HEADER: owner}
    assert client.get("/api/cave").status_code == 401
    agent = client.get("/api/cave", headers={auth.OWNER_HEADER: owner}).json()["agents"][0]
    assert agent["pid"] == 11 and agent["session_id"] == "s1"
    assert client.post("/api/cave/send", json={"to": "Ada", "text": "hi"}, headers={auth.OWNER_HEADER: owner}).status_code == 403
    assert client.post("/api/cave/send", json={"to": "Ada", "text": "hi"}, headers=same).json()["ok"] is True


def test_a_target_without_its_cwd_degrades_to_empty(tmp_path):
    from agent_companion.server import chat as c
    path = c.transcript_path({"pid": 1, "name": "Ada"}, tmp_path)
    assert not path.exists()
    assert c.replies(path, 0) == [] and c.effort(path) is None and c.conversation(path) == []


def test_model_and_effort_picker_commands_use_the_four_second_two_tap_pattern():
    source = (Path(__file__).resolve().parent.parent / "agent_companion/server/static/cave.js").read_text()
    assert "const PICKER_ARM_MS = 4000" in source
    assert "Tap again to switch to ${match[1]}" in source
    assert "if (!pickerTap(command)) return;" in source
