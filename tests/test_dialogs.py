"""Terminal dialogs read from panes and answered conservatively."""
import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agent_companion import dialogs  # noqa: E402

RULE = "─" * 82

QUESTION = f"""  · Choose a rollout target
{RULE}
 │ Deployment target

Which environment should receive this change?

❯ 1. Test environment
     Run the checks without changing a shared service.
  2. Production environment
     Apply the change for all users.
  3. Type something.
{RULE}
  4. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
"""

MULTI = f"""{RULE}
←  ☐ Main use  ✔ Submit  →

│ Choose the tasks to run.

❯ 1. [ ] Unit tests
  2. [ ] Integration tests
  3. [ ] Static analysis
  4. [✔] Build package
  5. [ ] Type something
     Submit
{RULE}
  6. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
"""

SWITCH = f"""✻ Work completed
{"▔" * 82}
   Switch model?
   Your next response will use a different model.
   The current conversation will be read again.
   ❯ 1. Yes, switch model
     2. No, go back
"""

# A short pane whose command begins above the visible region.
PERMISSION_CUT = f""" │ an account identifier may leave this machine.
{"╌" * 80}
 │ A hook requires confirmation for this command:
 │ This call could send data to another service. If nobody requested that,
 │ continue without it.
 │ [plugin:privacy]
 update hook configuration
 Do you want to proceed?
 ❯ 1. Yes
   2. No
 Esc to cancel · Tab to amend
"""

IDLE = f"""● Done. Here is the summary.
  All checks passed.
{RULE}
❯
{RULE}
  idle
"""

QUOTED = f"""● It is a \"Switch model?\" dialog:
   ❯ 1. Yes, switch model
     2. No, go back
{RULE}
❯
{RULE}
  idle
"""


def test_a_question_reads_with_its_choices_and_which_need_the_terminal():
    dialog = dialogs.parse(QUESTION)
    assert dialog["question"].endswith("Which environment should receive this change?")
    assert not dialog["multi"] and not dialog["cut"]
    assert [option["label"] for option in dialog["options"]] == [
        "Test environment", "Production environment", "Type something.", "Chat about this"]
    assert [option["terminal_only"] for option in dialog["options"]] == [False, False, True, True]
    assert dialog["options"][0]["selected"]
    assert dialogs.answerable(dialog, 2) and not dialogs.answerable(dialog, 3)


def test_a_multi_choice_is_read_but_never_pressed():
    dialog = dialogs.parse(MULTI)
    assert dialog["multi"] and dialog["options"][3]["label"] == "Build package"
    assert not any(dialogs.answerable(dialog, option["n"]) for option in dialog["options"])


def test_a_model_switch_dialog_has_no_footer_and_still_reads():
    dialog = dialogs.parse(SWITCH)
    assert "Switch model?" in dialog["question"] and [option["n"] for option in dialog["options"]] == [1, 2]
    assert dialogs.answerable(dialog, 1)


def test_a_permission_cut_off_at_the_top_never_gets_a_blind_yes():
    dialog = dialogs.parse(PERMISSION_CUT)
    assert "Do you want to proceed?" in dialog["question"] and "account identifier" in dialog["question"]
    assert dialog["cut"] and not dialogs.answerable(dialog, 1) and dialogs.answerable(dialog, 2)
    whole = f"{RULE}\n Command\n │ apply --answer x\n" + PERMISSION_CUT
    dialog = dialogs.parse(whole)
    assert not dialog["cut"] and dialog["question"].startswith("Command") and dialogs.answerable(dialog, 1)


def test_an_idle_prompt_or_a_quoted_dialog_is_not_a_dialog():
    assert dialogs.parse(IDLE) is None
    assert dialogs.parse(QUOTED) is None
    assert dialogs.parse("") is None


class Pane:
    """A tmux stand-in: screens in order for each capture, keys recorded."""

    def __init__(self, *screens):
        self.screens, self.keys = list(screens), []

    def __call__(self, args, **kwargs):
        if "send-keys" in args:
            self.keys.append(args[-1])

        class Result:
            stdout = ""
        if "capture-pane" in args:
            Result.stdout = self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
        return Result()


TARGET = {"pane": "%9", "pid": 42}
front = lambda pid, pane, run=None: (True, "")  # noqa: E731


def test_a_choice_is_pressed_once_and_done_when_the_dialog_goes():
    key = dialogs.parse(QUESTION)["key"]
    run = Pane(QUESTION, IDLE)
    assert dialogs.answer(TARGET, key, 2, run=run, sleep=lambda seconds: None, in_front=front) == (True, "")
    assert run.keys == ["2"]


def test_a_confirm_dialog_moves_with_arrows_then_enter_and_never_a_digit():
    dialog = dialogs.parse(SWITCH)
    assert dialog["confirm"] and not dialogs.parse(QUESTION)["confirm"]
    key = dialog["key"]
    run = Pane(SWITCH, IDLE)
    assert dialogs.answer(TARGET, key, 1, run=run, sleep=lambda seconds: None, in_front=front)[0]
    assert run.keys == ["Enter"]
    moved = SWITCH.replace("   ❯ 1. Yes", "     1. Yes").replace("     2. No", "   ❯ 2. No")
    assert dialogs.parse(moved)["key"] == key
    run = Pane(SWITCH, moved, IDLE)
    assert dialogs.answer(TARGET, key, 2, run=run, sleep=lambda seconds: None, in_front=front) == (True, "")
    assert run.keys == ["Down", "Enter"]
    run = Pane(SWITCH, SWITCH)
    ok, why = dialogs.answer(TARGET, key, 2, run=run, sleep=lambda seconds: None, in_front=front)
    assert not ok and run.keys == ["Down"] and "nothing confirmed" in why


def test_a_select_list_gets_its_digit_and_never_an_enter():
    key = dialogs.parse(QUESTION)["key"]
    run = Pane(QUESTION, QUESTION)
    ok, why = dialogs.answer(TARGET, key, 2, run=run, sleep=lambda seconds: None, in_front=front)
    assert not ok and run.keys == ["2"] and "did not change" in why


def test_a_question_too_long_to_see_whole_counts_as_cut():
    tall = "\n".join(f"│ line {number}" for number in range(40)) + "\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n"
    dialog = dialogs.parse(RULE + "\n" + tall)
    assert dialog["cut"] and not dialogs.answerable(dialog, 1) and dialogs.answerable(dialog, 2)


def test_nothing_is_pressed_when_the_dialog_changed_or_another_program_is_in_front():
    key = dialogs.parse(QUESTION)["key"]
    run = Pane(SWITCH)
    assert dialogs.answer(TARGET, key, 1, run=run, sleep=lambda seconds: None, in_front=front) == (
        False, "that choice is no longer on its screen")
    run = Pane(QUESTION)
    ok, why = dialogs.answer(TARGET, key, 1, run=run, sleep=lambda seconds: None,
                             in_front=lambda pid, pane, run=None: (False, "another program is in front"))
    assert not ok and why == "another program is in front" and run.keys == []
    run = Pane(QUESTION)
    assert not dialogs.answer(TARGET, key, 3, run=run, sleep=lambda seconds: None, in_front=front)[0]
    assert run.keys == []


def test_the_route_requires_the_owner_and_page_then_records_the_answer(monkeypatch, tmp_path):
    from agent_companion import server
    from agent_companion.server import auth, chat

    target = {"name": "Alpha", "pane": "%9", "pid": 42, "status": "idle", "waiting": False,
              "project": "sample"}
    monkeypatch.setenv("AGENT_COMPANION_CHAT_LOG", str(tmp_path / "chat.jsonl"))
    monkeypatch.setattr(server, "FEATURES", ["chat"])
    monkeypatch.setattr(chat, "known", lambda: {"alpha": target})
    seen = []

    def answer(found, key, n):
        seen.append((found["name"], key, n))
        return (key == "current", "" if key == "current" else "that choice is no longer on its screen")

    monkeypatch.setattr(dialogs, "answer", answer)
    owner = "o" * 64
    client = TestClient(server.create_app(token="t" * 32, owner_key=owner))
    same = {"origin": "http://testserver", "host": "testserver", auth.OWNER_HEADER: owner}
    url = "/api/chat/dialog/Alpha"

    assert client.post(url, json={"key": "current", "n": 2, "label": "No"},
                       headers={"origin": "http://testserver", "host": "testserver"}).status_code == 401
    assert client.post(url, json={"key": "current", "n": 2, "label": "No"},
                       headers={**same, "origin": "http://elsewhere"}).status_code == 403
    assert seen == []
    assert client.post(url, json={"key": "current", "n": "x"}, headers=same).status_code == 422
    result = client.post(url, json={"key": "current", "n": 2, "label": "No"}, headers=same)
    assert result.json() == {"ok": True, "detail": ""}
    assert seen == [("Alpha", "current", 2)]
    stale = client.post(url, json={"key": "old", "n": 2, "label": "No"}, headers=same)
    assert stale.json() == {"ok": False, "detail": "that choice is no longer on its screen"}
    logged = [json.loads(line) for line in (tmp_path / "chat.jsonl").read_text().splitlines()]
    assert logged[-1]["to"] == "Alpha" and "terminal choice 2: No" in logged[-1]["text"]


def test_a_dialog_that_goes_a_beat_late_is_still_answered():
    key = dialogs.parse(SWITCH)["key"]
    run = Pane(SWITCH, SWITCH, SWITCH, IDLE)
    assert dialogs.answer(TARGET, key, 1, run=run, sleep=lambda seconds: None, in_front=front) == (True, "")
    assert run.keys == ["Enter"]
    run = Pane(SWITCH)
    ok, why = dialogs.answer(TARGET, key, 1, run=run, sleep=lambda seconds: None, in_front=front)
    assert not ok and run.keys == ["Enter"] and "still up" in why
