"""Pure direct-chat safety checks."""
import pytest

pytest.importorskip("fastapi")

from agent_companion.server import chat


TARGET = {"name": "Ada", "pid": 11, "pane": "%3", "session_id": "s1", "status": "idle", "waiting": False}


class Run:
    def __init__(self):
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)

        class Result:
            stdout = "11\n"
        return Result()


def test_send_never_enters_when_a_dialog_appears_after_the_words(monkeypatch):
    states = iter([False, True])
    monkeypatch.setattr(chat, "target_waiting", lambda target: next(states))
    monkeypatch.setattr(chat, "in_front", lambda *args, **kwargs: (True, ""))
    run = Run()

    ok, detail = chat.send(TARGET, "continue", run=run, sleep=lambda _: None)

    assert detail.startswith("typed, not confirmed: it opened a dialog") and not ok
    # What matters is the keys: the words, and no Enter. Screen reads may come between.
    assert [c for c in run.calls if "send-keys" in c] == [["tmux", "send-keys", "-t", "%3", "-l", "--", "continue"]]


def test_send_never_retries_enter_when_a_dialog_appears(monkeypatch):
    states = iter([False, False, True])
    monkeypatch.setattr(chat, "target_waiting", lambda target: next(states))
    monkeypatch.setattr(chat, "in_front", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(chat, "_still_in_box", lambda *args: True)
    run = Run()

    ok, detail = chat.send(TARGET, "continue", run=run, sleep=lambda _: None)

    assert detail.startswith("typed, not confirmed: it opened a dialog") and not ok
    assert [c for c in run.calls if "send-keys" in c] == [["tmux", "send-keys", "-t", "%3", "-l", "--", "continue"],
                                                          ["tmux", "send-keys", "-t", "%3", "Enter"]]


def test_cave_stop_records_the_escape_as_a_stop(monkeypatch):
    monkeypatch.setattr(chat, "_target", lambda name: TARGET)
    monkeypatch.setattr(chat, "stop", lambda target: (True, ""))
    entries = []
    monkeypatch.setattr(chat, "record", entries.append)

    assert chat.cave_stop("Ada") == {"ok": True, "detail": ""}
    assert entries[-1]["text"] == "[stop]"


def test_upload_limits_reject_oversized_encoded_requests_before_decode(monkeypatch):
    assert chat.MAX_UPLOAD_ENCODED == (chat.MAX_UPLOAD * 4 + 2) // 3 + 4 * 1024
    monkeypatch.setattr(chat, "MAX_UPLOAD_ENCODED", 10)

    assert not chat.upload_content_too_large("10")
    assert chat.upload_content_too_large("11")
    assert not chat.upload_data_too_large("x" * 10)
    assert chat.upload_data_too_large("x" * 11)
