"""Chat (agent_companion/server/chat.py): the owner directs sessions from the
page. Nothing here touches tmux: every run is a fake."""
import json
import subprocess
import time

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

SWITCH_MID_TURN = f"""✽ Generating… (8s · ↓ 419 tokens)
{"▔" * 80}
  Switch model?
  Your next response will be slower and use more tokens

  This conversation is cached for the current model. Switching to Fable 5.2
  means the full history gets re-read.

  ❯ 1. Yes, switch to Fable 5.2
    2. No, go back

  Enter to confirm · Esc to cancel
"""

EFFORT_MID_TURN = f"""✽ Generating… (8s · ↓ 419 tokens)
{"▔" * 80}
  Change effort level?
  Your next response will use more tokens

  ❯ 1. Yes, switch to max
    2. No, go back

  Enter to confirm · Esc to cancel
"""


def _box(*rows):
    return "\n".join(["x", _RULE, "\x1b[39m❯ " + rows[0], *rows[1:], _RULE, "  status line"])


def test_send_types_one_line_then_enter(monkeypatch):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])   # pgrp == tpgid
    run = Screen(before=["push it please"], after=[""])
    ok, why = chat.send(KNOWN["ada"], "push it\nplease", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert ok, why
    keys = [c for c in run.calls if "send-keys" in c]
    assert keys == [["tmux", "send-keys", "-t", "%3", "-l", "--", "push it please"],
                    ["tmux", "send-keys", "-t", "%3", "Enter"]]


def test_parse_input_reads_what_sits_in_the_box():
    assert chat.parse_input(_box("")) == ""
    assert chat.parse_input(_box("board posts about the", "  updates", "  /clear")) == "board posts about the  updates  /clear"
    assert chat.parse_input("\n".join([_RULE, "\x1b[39m❯  \x1b[2mpush it\x1b[0m", _RULE])) == " "
    assert chat.parse_input("no box here") is None


def test_parse_input_reads_a_box_that_has_scrolled():
    """A tall box draws "↑ 1 more" in its top rule and starts mid-message."""
    screen = "\n".join(["done", "\x1b[38;5;244m─── ↑ 1 more " + "─" * 30 + "\x1b[39m",
                         "  overnight until we are caught up. Read the plan,",
                         "  and work through it in order.", "  /clearRKeep", _RULE, "  status line"])
    box = chat.parse_input(screen)
    assert box is not None and "work through it" in box and box.endswith("/clearRKeep")


_CUT_OFF = """✻ Worked for 12s

╭──────────────────────────────────────────────────────────────────────────────╮
│ ✻ Bug report drafted: an example title                                       │
│ │ - What happened: an example detail line                                    │
│ clear text to review, send, or dismiss                                       │
╰──────────────────────────────────────────────────────────────────────────────╯

                                        ✔ Update installed · Restart to update
────────────────────────────────────────────────────────────────────────────────
❯ An example message long enough to wrap onto a second line of the input box
  and then onto a third line, which is the last line the window can show
  the end"""


@pytest.mark.parametrize(("screen", "cut_off"), [
    (_CUT_OFF, True),
    (_CUT_OFF + "\n" + _RULE + "\n  status line", False),
    ("no prompt line\n" + _RULE + "\n  status line", False),
    ("\n".join(["x", "─── ↑ 1 more ───", "  the end"]), True),
    ("\n".join(["x", "─── ↑ 1 more ───", "  the end", _RULE]), False),
], ids=["cut-off", "whole", "no-box", "scrolled-cut-off", "scrolled-whole"])
def test_input_cut_off_distinguishes_an_incomplete_box(screen, cut_off):
    assert chat.input_cut_off(screen) is cut_off
    if cut_off:
        assert chat.parse_input(screen) is None


def test_send_never_types_onto_a_scrolled_box(monkeypatch, tmp_path):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    _failed(tmp_path, monkeypatch)
    tall = "\n".join(["x", "\x1b[38;5;244m─── ↑ 1 more " + "─" * 30 + "\x1b[39m",
                       "  an earlier long message", _RULE, "  status line"])

    class Tall(Run):
        def __call__(self, args, **kw):
            if "capture-pane" not in args:
                return super().__call__(args, **kw)
            class R:
                stdout = tall
            return R()
    run = Tall()
    ok, why = chat.send(KNOWN["ada"] | {"status": "idle"}, "Keep", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert not ok and "already holds words" in why
    assert not [c for c in run.calls if "send-keys" in c]


class Screen(Run):
    """Fake tmux input-box states: `start` until anything is typed (`cleared`
    once Ctrl-C has been pressed), then `before` until the first Enter, then
    `after`."""
    def __init__(self, before, after, start="", cleared=""):
        super().__init__()
        self.before, self.after, self.start, self.cleared = before, after, start, cleared
        self.before_seen = self.after_seen = 0

    def __call__(self, args, **kw):
        if "capture-pane" not in args:
            return super().__call__(args, **kw)
        self.calls.append(args)
        keys = [call for call in self.calls if "send-keys" in call]
        if not any("-l" in call or call[-1] == "Enter" for call in keys):
            value = self.cleared if any(call[-1] == "C-c" for call in keys) else self.start
            class Idle:
                stdout = "no input box" if value is None else _box(*(value if isinstance(value, tuple) else (value,)))
            return Idle()
        entered = any(call[-1] == "Enter" for call in self.calls if "send-keys" in call)
        states, seen = (self.after, self.after_seen) if entered else (self.before, self.before_seen)
        value = states[min(seen, len(states) - 1)]
        if entered:
            self.after_seen += 1
        else:
            self.before_seen += 1
        class R:
            stdout = "no input box" if value is None else _box(*(value if isinstance(value, tuple) else (value,)))
        return R()


class CutOffScreen(Screen):
    """The bottom rule is missing until a detached window has more room."""
    def __init__(self, before, after, start="", window="0 24", refuse=False,
                 only_after_typing=False, redraws=0, stays_cut_off=False):
        super().__init__(before, after, start=start)
        self.window, self.refuse = window, refuse
        self.only_after_typing, self.redraws = only_after_typing, redraws
        self.stays_cut_off, self.resized = stays_cut_off, False

    def __call__(self, args, **kw):
        result = super().__call__(args, **kw)
        if "display-message" in args and args[-1] == "#{session_attached} #{window_height}":
            result.stdout = self.window
        if "resize-window" in args:
            if self.refuse:
                raise subprocess.CalledProcessError(1, args)
            self.resized = True
        if "capture-pane" in args:
            keys = [call for call in self.calls if "send-keys" in call]
            typed = any("-l" in call for call in keys)
            entered = any(call[-1] == "Enter" for call in keys)
            if not entered and (not self.only_after_typing or typed):
                if not self.resized or self.redraws or self.stays_cut_off:
                    result.stdout = "\n".join(result.stdout.splitlines()[:-2])
                    if self.resized and self.redraws:
                        self.redraws -= 1
        return result


def test_send_makes_room_for_a_cut_off_box_before_typing(monkeypatch):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    run = CutOffScreen(before=["push it"], after=[""], redraws=2)
    waits = []
    ok, why = chat.send(KNOWN["ada"], "push it", run=run, sleep=waits.append,
                        appear_wait_s=.1, clear_wait_s=.1, check_s=.1)
    assert ok, why
    resize = ["tmux", "resize-window", "-t", "%3", "-y", "50"]
    unset = ["tmux", "set-window-option", "-t", "%3", "-u", "window-size"]
    assert [c for c in run.calls if "resize-window" in c or "set-window-option" in c] == [resize, unset]
    typed = ["tmux", "send-keys", "-t", "%3", "-l", "--", "push it"]
    assert run.calls.index(resize) < run.calls.index(unset) < run.calls.index(typed)
    assert any("capture-pane" in c for c in run.calls[run.calls.index(unset) + 1:run.calls.index(typed)])
    assert len(waits) >= 4
    assert [c[-1] for c in run.calls if "send-keys" in c] == ["push it", "Enter"]


@pytest.mark.parametrize("window", ["1 24", "2 24", "0 50", "0 80", "bad", "0", "0 bad", "0 0"])
def test_send_refuses_a_cut_off_box_when_no_room_can_be_made(monkeypatch, window):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    run = CutOffScreen(before=["push it"], after=[""], window=window)
    assert chat.send(KNOWN["ada"], "push it", run=run, sleep=lambda s: None,
                     appear_wait_s=.01, clear_wait_s=.01, check_s=.01) == (
        False, "Ada's input box is cut off by the bottom of its window; not typed")
    assert not [c for c in run.calls if "resize-window" in c or "set-window-option" in c or "send-keys" in c]


def test_send_refuses_a_cut_off_box_when_tmux_refuses_room(monkeypatch):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    run = CutOffScreen(before=["push it"], after=[""], refuse=True)
    ok, why = chat.send(KNOWN["ada"], "push it", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert not ok and "cut off" in why
    assert sum("resize-window" in c for c in run.calls) == 1
    assert not [c for c in run.calls if "send-keys" in c or "set-window-option" in c]


def test_send_refuses_a_box_still_cut_off_after_making_room(monkeypatch):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    run = CutOffScreen(before=["push it"], after=[""], stays_cut_off=True)
    waits = []
    ok, why = chat.send(KNOWN["ada"], "push it", run=run, sleep=waits.append,
                        appear_wait_s=.1, clear_wait_s=.1, check_s=.1)
    assert not ok and "cut off" in why
    assert .9 <= sum(waits) <= 1.1
    assert not [c for c in run.calls if "send-keys" in c]


@pytest.mark.parametrize(("start", "ours", "status", "keys"), [
    ("push it", True, "busy", ["Enter"]),
    ("earlier words", True, "idle", ["C-c", "push it", "Enter"]),
    ("earlier words", True, "busy", []),
    ("push it", False, "idle", []),
])
def test_send_checks_words_revealed_by_a_cut_off_box(monkeypatch, tmp_path, start, ours, status, keys):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    _failed(tmp_path, monkeypatch, *([start] if ours else []))
    run = CutOffScreen(before=["push it"], after=[""], start=start)
    ok, why = chat.send(KNOWN["ada"] | {"status": status}, "push it", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert ok is bool(keys), why
    assert [c[-1] for c in run.calls if "send-keys" in c] == keys
    if not keys:
        assert "already holds words" in why


def test_send_makes_room_when_typing_cuts_off_the_box(monkeypatch):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    run = CutOffScreen(before=["push it"], after=[""], only_after_typing=True, redraws=2)
    ok, why = chat.send(KNOWN["ada"], "push it", run=run, sleep=lambda s: None,
                        appear_wait_s=.1, clear_wait_s=.01, check_s=.01)
    assert ok, why
    assert sum("resize-window" in c for c in run.calls) == 1
    assert sum("set-window-option" in c for c in run.calls) == 1
    assert [c[-1] for c in run.calls if "send-keys" in c] == ["push it", "Enter"]


def test_send_keeps_the_screen_when_typed_words_stay_cut_off(monkeypatch, tmp_path):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    log = _failed(tmp_path, monkeypatch)
    run = CutOffScreen(before=["push it"], after=[""], only_after_typing=True, stays_cut_off=True)
    detail = "the input box is cut off by the bottom of its window; Enter was not pressed"
    assert chat.send(KNOWN["ada"], "push it", run=run, sleep=lambda s: None,
                     appear_wait_s=.1, clear_wait_s=.01, check_s=.01) == (False, "typed, not confirmed: " + detail)
    assert sum("resize-window" in c for c in run.calls) == 1
    assert [c[-1] for c in run.calls if "send-keys" in c] == ["push it"]
    kept = json.loads(log.with_name("send-failures.jsonl").read_text())
    assert kept["detail"] == detail and chat.input_cut_off(kept["screen"])


def test_retry_does_not_pile_words_onto_a_cut_off_box(monkeypatch, tmp_path):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    log = _failed(tmp_path, monkeypatch)
    run = CutOffScreen(before=["push it"], after=[""], only_after_typing=True, window="1 24")
    ok, why = chat.send(KNOWN["ada"], "push it", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert not ok and why.startswith("typed, not confirmed")
    chat.record({"at": time.time(), "to": "Ada", "text": "push it", "ok": ok, "detail": why}, log)
    run.window = "0 24"
    ok, why = chat.send(KNOWN["ada"], "push it", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert ok, why
    assert [c[-1] for c in run.calls if "send-keys" in c] == ["push it", "Enter"]


@pytest.mark.parametrize("words", list("0123456789&é\"'(-§è_çà") + ["２", "e\u0301"])
def test_send_protects_a_lone_card_character(monkeypatch, words):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    run = Screen(before=[" " + words], after=[""])
    assert chat.send(KNOWN["ada"], words, run=run, sleep=lambda s: None,
                     appear_wait_s=.01, clear_wait_s=.01, check_s=.01) == (True, "")
    assert [c[-1] for c in run.calls if "send-keys" in c] == [" " + words, "Enter"]


@pytest.mark.parametrize("words", ["12", "y", "2 please"])
def test_send_leaves_other_card_characters_unchanged(monkeypatch, words):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    run = Screen(before=[words], after=[""])
    assert chat.send(KNOWN["ada"], words, run=run, sleep=lambda s: None,
                     appear_wait_s=.01, clear_wait_s=.01, check_s=.01) == (True, "")
    assert [c[-1] for c in run.calls if "send-keys" in c] == [words, "Enter"]


class SwitchScreen(Screen):
    """An empty box, then typed words, then a confirm dialog after Enter."""

    def __init__(self, words, dialog):
        super().__init__([words], [None])
        self.dialog = dialog

    def __call__(self, args, **kw):
        if "capture-pane" in args and any(call[-1] == "Enter" for call in self.calls if "send-keys" in call):
            self.calls.append(args)

            class Result:
                stdout = self.dialog
            return Result()
        return super().__call__(args, **kw)


def test_send_confirms_words_show_then_clear(monkeypatch):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    run = Screen(before=[("two board", " posts")], after=[""])
    ok, why = chat.send(KNOWN["ada"], "two board posts", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert ok, why
    assert [c[-1] for c in run.calls if "send-keys" in c] == ["two board posts", "Enter"]


def test_send_does_not_enter_when_words_never_show(monkeypatch):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    run = Screen(before=[""], after=[])
    ok, why = chat.send(KNOWN["ada"], "two board posts", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert not ok and why.startswith("typed, not confirmed")
    assert not any(c[-1] == "Enter" for c in run.calls if "send-keys" in c)


def test_send_is_not_confirmed_when_box_is_unparseable_after_enter(monkeypatch):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    run = Screen(before=["two board posts"], after=[None])
    ok, why = chat.send(KNOWN["ada"], "two board posts", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert not ok and why.startswith("typed, not confirmed")
    assert sum(c[-1] == "Enter" for c in run.calls if "send-keys" in c) == 1


@pytest.mark.parametrize(("words", "dialog"), [
    ("/model claude-fable-5-2", SWITCH_MID_TURN),
    ("/effort max", EFFORT_MID_TURN),
])
def test_a_model_or_effort_pick_counts_as_sent_when_its_confirm_dialog_opens(monkeypatch, words, dialog):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    run = SwitchScreen(words, dialog)
    assert chat.send(KNOWN["ada"], words, run=run, sleep=lambda s: None,
                     appear_wait_s=.01, clear_wait_s=.01, check_s=.01) == (True, "")


@pytest.mark.parametrize(("words", "dialog"), [
    ("two board posts", SWITCH_MID_TURN),          # a plain message: no dialog is its own
    ("/effort max", SWITCH_MID_TURN),              # an effort pick under the model dialog
    ("/model claude-fable-5-2", EFFORT_MID_TURN),  # a model pick under the effort dialog
])
def test_only_a_picks_own_confirm_dialog_counts_as_sent(monkeypatch, words, dialog):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    run = SwitchScreen(words, dialog)
    ok, why = chat.send(KNOWN["ada"], words, run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert not ok and why.startswith("typed, not confirmed")


def test_send_is_not_confirmed_when_words_remain_after_enter(monkeypatch):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    run = Screen(before=["two board posts"], after=["two board posts"])
    monkeypatch.setattr(chat, "ENTER_RETRY_EVERY_S", 0)
    ok, why = chat.send(KNOWN["ada"], "two board posts", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.05, check_s=.01)
    assert not ok and why.startswith("typed, not confirmed")
    # the first Enter and its two retries, no more
    assert sum(c[-1] == "Enter" for c in run.calls if "send-keys" in c) == 1 + chat.ENTER_RETRIES


def test_send_presses_enter_again_when_the_first_became_a_newline(monkeypatch):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    monkeypatch.setattr(chat, "ENTER_RETRY_EVERY_S", 0)
    run = Screen(before=["two board posts"], after=["two board posts", ""])
    ok, why = chat.send(KNOWN["ada"], "two board posts", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.05, check_s=.01)
    assert ok, why
    assert sum(c[-1] == "Enter" for c in run.calls if "send-keys" in c) == 2


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


def test_nothing_is_typed_into_a_terminal_dialog_that_no_hook_reported(monkeypatch):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])

    class Dialog(Run):
        def __call__(self, args, **kw):
            if "capture-pane" in args:
                self.calls.append(args)

                class Result:
                    stdout = SWITCH_MID_TURN
                return Result()
            return super().__call__(args, **kw)

    run = Dialog()
    assert chat.send(KNOWN["ada"], "/effort max", run=run) == (
        False, "Ada is showing a choice in its terminal; answer that first")
    assert not any("send-keys" in call for call in run.calls)


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



def test_a_compaction_summary_is_not_the_owners_message(tmp_path):
    f = _lines(tmp_path, [
        {"type": "user", "timestamp": "2026-01-01T00:00:00Z", "uuid": "u1", "message": {"content": "do it"}},
        {"type": "user", "timestamp": "2026-01-01T00:00:05Z", "uuid": "c1", "isCompactSummary": True,
         "isVisibleInTranscriptOnly": True,
         "message": {"content": "This session is being continued from a previous conversation..."}},
        {"type": "assistant", "timestamp": "2026-01-01T00:00:06Z", "uuid": "a1", "message": {
            "model": "m", "content": [{"type": "text", "text": "Carrying on."}]}},
    ])
    assert [(m["role"], m["text"]) for m in chat.conversation(f)] == [("me", "do it"), ("agent", "Carrying on.")]


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


def test_a_window_surfaces_an_unreported_terminal_dialog_and_marks_it_waiting(client, monkeypatch):
    from agent_companion import dialogs

    terminal = dialogs.parse(SWITCH_MID_TURN)
    seen = []
    monkeypatch.setattr(dialogs, "read", lambda target: seen.append(target["name"]) or terminal)
    window = client.get("/api/chat/window/Ada", headers=KEY).json()
    assert window["dialog"] == terminal and window["waiting"] is True and seen == ["Ada"]
    busy = dict(KNOWN["ada"], status="busy")
    monkeypatch.setattr(chat, "known", lambda: {"ada": busy})
    window = client.get("/api/chat/window/Ada", headers=KEY).json()
    assert window["dialog"] == terminal and window["waiting"] is True and seen == ["Ada", "Ada"]


def test_transcript_path_names_the_folder_like_claude_code(tmp_path):
    t = {"cwd": "/home/t/my_proj/.wt", "session_id": "abc"}
    assert chat.transcript_path(t, tmp_path) == tmp_path / "-home-t-my-proj--wt" / "abc.jsonl"


def test_chat_reads_the_same_claude_home_key_as_sessions(monkeypatch, tmp_path):
    from agent_companion.server import sessions
    monkeypatch.setenv("AGENT_COMPANION_CLAUDE_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "other"))
    assert chat.claude_home() == sessions.claude_home() == tmp_path / "h"


def _failed(tmp_path, monkeypatch, *texts, to="Ada", age=60):
    """The chat log, with these sends to `to` typed but not confirmed."""
    log = tmp_path / "chat.jsonl"
    log.write_text("".join(json.dumps({"at": time.time() - age, "to": to, "text": t, "ok": False,
                                       "detail": "typed, not confirmed: the input box never showed the typed words"}) + "\n"
                           for t in texts))
    monkeypatch.setattr(chat, "log_path", lambda: log)
    return log


LONG = " ".join(["message"] * 250)      # long enough for the terminal to fold into a placeholder


def test_send_confirms_long_message_as_pasted_text(monkeypatch):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    line = LONG
    run = Screen(before=["[Pasted text #1]"], after=[""])
    ok, why = chat.send(KNOWN["ada"], line, run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert ok, why
    assert [c[-1] for c in run.calls if "send-keys" in c] == [line, "Enter"]


def test_send_confirms_pasted_text_with_line_count(monkeypatch):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    run = Screen(before=["[Pasted text #2 +9 lines]"], after=[""])
    ok, why = chat.send(KNOWN["ada"], "send the full report", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert ok, why
    assert [c[-1] for c in run.calls if "send-keys" in c] == ["send the full report", "Enter"]


def test_send_retries_enter_while_pasted_text_placeholder_remains(monkeypatch):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    monkeypatch.setattr(chat, "ENTER_RETRY_EVERY_S", 0)
    run = Screen(before=["[Pasted text #1]"], after=["[Pasted text #1]"])
    ok, why = chat.send(KNOWN["ada"], "send the full report", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.05, check_s=.01)
    enters = sum(c[-1] == "Enter" for c in run.calls if "send-keys" in c)
    assert not ok and why.startswith("typed, not confirmed")
    assert 1 <= enters <= 1 + chat.ENTER_RETRIES


def test_send_refuses_an_unclaimed_pasted_text_placeholder(monkeypatch, tmp_path):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    _failed(tmp_path, monkeypatch)
    run = Screen(before=["ignored"], after=[""], start="[Pasted text #1]")
    ok, why = chat.send(KNOWN["ada"], "send the full report", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert not ok and "already holds words not sent from here" in why
    assert not [c for c in run.calls if "send-keys" in c]


def test_send_enters_our_pasted_text_placeholder_even_when_busy(monkeypatch, tmp_path):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    _failed(tmp_path, monkeypatch, LONG)
    run = Screen(before=["ignored"], after=[""], start="[Pasted text #1]")
    ok, why = chat.send(KNOWN["ada"] | {"status": "busy"}, LONG, run=run,
                        sleep=lambda s: None, appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert ok, why
    assert [c[-1] for c in run.calls if "send-keys" in c] == ["Enter"]


def test_send_clears_our_other_pasted_text_placeholder_before_typing(monkeypatch, tmp_path):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    _failed(tmp_path, monkeypatch, "an earlier report " + LONG)
    line = "send the full report"
    run = Screen(before=[line], after=[""], start="[Pasted text #1]", cleared="")
    ok, why = chat.send(KNOWN["ada"] | {"status": "idle"}, line, run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert ok, why
    assert [c[-1] for c in run.calls if "send-keys" in c] == ["C-c", line, "Enter"]


def test_send_refuses_our_other_pasted_text_placeholder_when_busy(monkeypatch, tmp_path):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    _failed(tmp_path, monkeypatch, "an earlier report " + LONG)
    run = Screen(before=["ignored"], after=[""], start="[Pasted text #1]")
    ok, why = chat.send(KNOWN["ada"] | {"status": "busy"}, "send the full report", run=run,
                        sleep=lambda s: None, appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert not ok and "already holds words not sent from here" in why
    assert not [c for c in run.calls if "send-keys" in c]


def test_old_failures_do_not_make_a_pasted_text_placeholder_ours(monkeypatch, tmp_path):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    _failed(tmp_path, monkeypatch, LONG, age=chat.LEFTOVER_S + 1)
    run = Screen(before=["ignored"], after=[""], start="[Pasted text #1]")
    ok, why = chat.send(KNOWN["ada"], LONG, run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert not ok and "already holds words not sent from here" in why
    assert not [c for c in run.calls if "send-keys" in c]


def test_a_short_unconfirmed_send_does_not_own_a_pasted_text_placeholder(monkeypatch, tmp_path):
    """Short words are never folded, so a paste in the box after them came from someone else."""
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    _failed(tmp_path, monkeypatch, "push it")
    run = Screen(before=["ignored"], after=[""], start="[Pasted text #1]")
    ok, why = chat.send(KNOWN["ada"] | {"status": "idle"}, "push it", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert not ok and "already holds words not sent from here" in why
    assert not [c for c in run.calls if "send-keys" in c]


def test_send_is_not_confirmed_and_not_entered_again_beside_other_words(monkeypatch):
    """After Enter the paste still shows, now with words typed beside it: a second
    Enter would send those too."""
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    monkeypatch.setattr(chat, "ENTER_RETRY_EVERY_S", 0)
    run = Screen(before=["[Pasted text #1]"], after=["[Pasted text #1] and more"])
    ok, why = chat.send(KNOWN["ada"], LONG, run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.05, check_s=.01)
    assert not ok and why.startswith("typed, not confirmed")
    assert sum(c[-1] == "Enter" for c in run.calls if "send-keys" in c) == 1


def test_send_refuses_an_image_placeholder(monkeypatch, tmp_path):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    _failed(tmp_path, monkeypatch)
    run = Screen(before=["ignored"], after=[""], start="[Image #1]")
    ok, why = chat.send(KNOWN["ada"], "send the full report", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert not ok and "already holds words not sent from here" in why
    assert not [c for c in run.calls if "send-keys" in c]


def test_send_refuses_pasted_text_placeholder_with_other_words(monkeypatch, tmp_path):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    _failed(tmp_path, monkeypatch)
    run = Screen(before=["ignored"], after=[""], start="[Pasted text #1] and more")
    ok, why = chat.send(KNOWN["ada"], "send the full report", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert not ok and "already holds words not sent from here" in why
    assert not [c for c in run.calls if "send-keys" in c]


def test_send_never_types_onto_words_it_did_not_put_there(monkeypatch, tmp_path):
    """2026-11-18: a stuck word sat in the box and every retry was typed after
    it. Words in the box that this page did not type are someone's: refuse."""
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    _failed(tmp_path, monkeypatch)
    run = Screen(before=["x"], after=[""], start="half a thought")
    ok, why = chat.send(KNOWN["ada"] | {"status": "idle"}, "push it", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert not ok and "half a thought" in why and "already" in why
    assert not [c for c in run.calls if "send-keys" in c]


def test_our_own_stuck_words_are_cleared_before_typing(monkeypatch, tmp_path):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    _failed(tmp_path, monkeypatch, "Yes", "Should they be combined ?")
    run = Screen(before=["Should they be combined ?"], after=[""],
                 start=("Yes", "  Should they be combined ?Should they be combined ?"), cleared="")
    ok, why = chat.send(KNOWN["ada"] | {"status": "idle"}, "Should they be combined ?", run=run,
                        sleep=lambda s: None, appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert ok, why
    assert [c[-1] for c in run.calls if "send-keys" in c] == ["C-c", "Should they be combined ?", "Enter"]


def test_a_busy_session_is_never_sent_ctrl_c(monkeypatch, tmp_path):
    """Ctrl-C at a working session interrupts its turn: leftovers there are refused."""
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    _failed(tmp_path, monkeypatch, "Yes")
    run = Screen(before=["x"], after=[""], start="Yes")
    ok, why = chat.send(KNOWN["ada"] | {"status": "busy"}, "push it", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert not ok and "Yes" in why
    assert not [c for c in run.calls if "send-keys" in c]


def test_a_late_redraw_is_still_entered(monkeypatch):
    """2026-11-18: Kent's box showed nothing for over 3 s, then the whole
    message sat there unsent. The send waits for a slow redraw."""
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    run = Screen(before=[""] * 40 + ["push it"], after=[""])
    ok, why = chat.send(KNOWN["ada"], "push it", run=run, sleep=lambda s: None,
                        appear_wait_s=10, clear_wait_s=.01, check_s=.1)
    assert ok, why
    assert chat.INPUT_APPEAR_WAIT_S >= 10


def test_our_stuck_message_is_entered_not_retyped_even_at_a_working_session(monkeypatch, tmp_path):
    """The same words, typed earlier and never confirmed: press Enter on them.
    No Ctrl-C, so a working session's turn is never interrupted."""
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    _failed(tmp_path, monkeypatch, "push it")
    run = Screen(before=["x"], after=[""], start="push it")
    ok, why = chat.send(KNOWN["ada"] | {"status": "busy"}, "push it", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert ok, why
    assert [c[-1] for c in run.calls if "send-keys" in c] == ["Enter"]


def test_someone_elses_identical_words_are_not_entered(monkeypatch, tmp_path):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    _failed(tmp_path, monkeypatch)
    run = Screen(before=["x"], after=[""], start="push it")
    ok, why = chat.send(KNOWN["ada"] | {"status": "busy"}, "push it", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert not ok and not [c for c in run.calls if "send-keys" in c]


def test_old_failures_do_not_make_words_ours(monkeypatch, tmp_path):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    _failed(tmp_path, monkeypatch, "Yes", age=3600)
    run = Screen(before=["x"], after=[""], start="Yes")
    ok, why = chat.send(KNOWN["ada"] | {"status": "idle"}, "push it", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert not ok and not [c for c in run.calls if "send-keys" in c]


def test_a_failed_send_keeps_the_screen_for_later(monkeypatch, tmp_path):
    monkeypatch.setattr(chat, "_stat", lambda pid: ["S", "1", "11", "0", "0", "11"])
    log = _failed(tmp_path, monkeypatch)
    run = Screen(before=[""], after=[])
    ok, why = chat.send(KNOWN["ada"], "two board posts", run=run, sleep=lambda s: None,
                        appear_wait_s=.01, clear_wait_s=.01, check_s=.01)
    assert not ok
    kept = [json.loads(l) for l in log.with_name("send-failures.jsonl").read_text().splitlines()]
    assert kept[-1]["pane"] == "%3" and "❯" in kept[-1]["screen"] and kept[-1]["detail"].startswith("the input box")
    assert oct(log.with_name("send-failures.jsonl").stat().st_mode)[-3:] == "600"
