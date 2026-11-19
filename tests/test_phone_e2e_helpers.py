"""The small pure readers in bin/phone-e2e."""
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest


def _e2e():
    path = Path(__file__).parent.parent / "bin" / "phone-e2e"
    loader = importlib.machinery.SourceFileLoader("phone_e2e", str(path))
    spec = importlib.util.spec_from_loader("phone_e2e", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    loader.exec_module(mod)
    return mod


E = _e2e()


LOOK = '''app: Chrome
12 button "Pay $12.40"
13 field "Search products" focused
14 button (no label)
15 button [mystery]
16 button "Send SMS"
'''


def test_look_items_keep_numbers_kinds_and_only_accessible_labels():
    assert E.look_items(LOOK) == [
        E.LookItem(12, "button", "Pay $12.40"),
        E.LookItem(13, "field", "Search products"),
        E.LookItem(14, "button", None),
        E.LookItem(15, "button", None),
        E.LookItem(16, "button", "Send SMS"),
    ]


def test_find_label_prefers_an_exact_label_then_one_unambiguous_phrase():
    assert E.find_label(LOOK, "Pay $12.40", "button") == 12
    assert E.find_label(LOOK, "Send", "button") == 16
    with pytest.raises(ValueError, match="expected one"):
        E.find_label(LOOK, "Missing")


def test_find_unlabeled_button_needs_exactly_one():
    assert E.find_unlabeled_button('7 button (no label)') == 7
    with pytest.raises(ValueError, match="expected one unlabeled"):
        E.find_unlabeled_button(LOOK)


def test_stopped_outcome_reads_phone_status_only():
    assert E.stopped_outcome("ended: j1 on the emulator, test: task (stopped: banner)")
    assert not E.stopped_outcome("live: j1 on the emulator, test: task (running: driving)")


STATUS = """live: j1 on the emulator, test: task (paused: paused: the device holder is using the phone); last: look
live: j2 on the emulator, test: task (waiting: the device holder's phone is locked: waiting for him to unlock it (until 12:34))
ended: j3 on the emulator, test: task (stopped: the device holder stopped it (notification))
"""


def test_status_lines_keep_the_public_scope_state_and_detail_words():
    assert E.status_lines(STATUS) == [
        E.StatusLine("live", "paused", "paused: the device holder is using the phone"),
        E.StatusLine("live", "waiting", "the device holder's phone is locked: waiting for him to unlock it (until 12:34)"),
        E.StatusLine("ended", "stopped", "the device holder stopped it (notification)"),
    ]


def test_status_is_matches_the_documented_state_and_detail_prefix():
    assert E.status_is(STATUS, "live", "paused", "paused: the device holder is using the phone")
    assert E.status_is(STATUS, "live", "waiting", "the device holder's phone is locked")
    assert E.status_is(STATUS, "ended", "stopped", "the device holder stopped it (notification)")
    assert not E.status_is(STATUS, "live", "paused", "paused: the device holder is in a call")
    assert not E.status_is(STATUS, "live", "stopped")


HANDING_OVER_STATUS = """live: j1 on the emulator, test: task (running: driving the emulator)
live: j2 on the emulator, test: task (paused: paused: the device holder is using the phone)
live: j3 on the emulator, test: task (paused: paused: the device holder is in a call)
live: j4 on the emulator, test: task (waiting: the device holder's phone is locked: waiting for him to unlock it (until 12:34))
live: j5 on the emulator, test: task (waiting: the device holder is in a call: the job starts when it ends)
ended: j6 on the emulator, test: task (stopped: the device holder stopped it (notification))
ended: j7 on the emulator, test: task (stopped: the device holder stopped it from agent_companion)
ended: j8 on the emulator, test: task (stopped: the device holder stopped it (power))
"""


@pytest.mark.parametrize(("scope", "state", "detail"), [
    ("live", "running", "driving the emulator"),
    ("live", "paused", "paused: the device holder is using the phone"),
    ("live", "paused", "paused: the device holder is in a call"),
    ("live", "waiting", "the device holder's phone is locked"),
    ("live", "waiting", "the device holder is in a call"),
    ("ended", "stopped", "the device holder stopped it (notification)"),
    ("ended", "stopped", "the device holder stopped it from agent_companion"),
    ("ended", "stopped", "the device holder stopped it (power)"),
])
def test_status_is_reads_every_handing_over_status_word(scope, state, detail):
    assert E.status_is(HANDING_OVER_STATUS, scope, state, detail)


def test_has_code_reads_deploy_phones_refusal_suffix():
    assert E.has_code("the device holder has the phone [paused]", "paused")
    assert E.has_code("the job is over [ended]", "ended")
    assert not E.has_code("the device holder has the phone [waiting]", "paused")
