"""Hold keeps what was said; mute drops it.  While held, speak.sh writes each
speaker's clips to held/<Name> and quiet.sh resume speaks them.  Also the
coordinator's two other buttons, the test clip and stop, and a live call's
hold on one speaker."""
import os
import subprocess
import time

import pytest

from agent_companion import agentvibes


def _tts(tmp_path):
    tts = tmp_path / "tts"
    (tts / "held").mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "audio-effects.cfg").write_text("Alto|gain 8|one.mp3|0.70\n")
    return tts


def _script(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


# --- the hold -----------------------------------------------------------------

def test_not_held_by_default(tmp_path):
    tts = _tts(tmp_path)
    assert agentvibes.hold_state(tts) == {"until": None, "waiting": 0}
    assert agentvibes.held(tts) is False


def test_holding_until_resumed_for_a_while_and_lifting(tmp_path):
    tts = _tts(tmp_path)
    assert agentvibes.set_hold(tts, None)["until"] == 0 and agentvibes.held(tts)
    assert agentvibes.set_hold(tts, 15)["until"] > time.time() + 14 * 60
    assert agentvibes.set_hold(tts, 0)["until"] is None and not (tts / "hold").exists()


def test_an_expired_hold_reads_as_lifted(tmp_path):
    tts = _tts(tmp_path)
    (tts / "hold").write_text(f"{int(time.time()) - 5}\n")
    assert agentvibes.hold_state(tts)["until"] is None and not agentvibes.held(tts)


@pytest.mark.parametrize("minutes", [-1, 10 ** 9, "soon", 1.5])
def test_a_bad_duration_is_refused(tmp_path, minutes):
    tts = _tts(tmp_path)
    with pytest.raises(ValueError):
        agentvibes.set_hold(tts, minutes)
    assert agentvibes.hold_state(tts)["until"] is None


def test_what_waits_is_listed_oldest_first_and_parsed(tmp_path):
    tts = _tts(tmp_path)
    (tts / "held" / "Alto").write_text("1789671866\tproj/main\tTraced it to the loop.\n"
                                       "1789671885\tproj/main\tIt is fixed.\n")
    (tts / "held" / "Bass").write_text("just a bare line\n")      # an older shape is kept whole
    (tts / "held" / "Tenor").write_text("")                      # touched, nothing said yet
    (tts / "held" / "..evil").write_text("1\t-/-\tnot a speaker\n")
    old = time.time() - 300
    os.utime(tts / "held" / "Alto", (old, old))
    assert agentvibes.held_clips(tts) == [
        {"name": "Alto", "text": "Traced it to the loop.", "count": 2,
         "clips": ["Traced it to the loop.", "It is fixed."], "where": "proj/main"},
        {"name": "Bass", "text": "just a bare line", "count": 1, "clips": ["just a bare line"], "where": ""}]
    assert agentvibes.hold_state(tts)["waiting"] == 2


def test_no_spool_folder_is_nothing_waiting(tmp_path):
    tts = tmp_path / "tts"
    tts.mkdir()
    assert agentvibes.held_clips(tts) == [] and agentvibes.hold_state(tts)["waiting"] == 0


@pytest.mark.parametrize("raw,shown", [("volume", "phone is muted"), ("alarm", "alarm set"), ("call", "on a call"),
                                       ("something new", None)])
def test_an_auto_hold_says_why_and_since_when(tmp_path, raw, shown):
    tts = _tts(tmp_path)
    (tts / ".autohold").write_text(raw)
    (tts / ".hold-since").write_text(str(int(time.time()) - 600))
    agentvibes.set_hold(tts, None)
    state = agentvibes.hold_state(tts)
    assert state["reason"] == shown and state["since"] < time.time()


def test_a_hold_set_by_hand_offers_no_reason(tmp_path):
    tts = _tts(tmp_path)
    (tts / ".hold-since").write_text("not a number")
    agentvibes.set_hold(tts, None)
    assert agentvibes.hold_state(tts) == {"until": 0, "waiting": 0, "reason": None, "since": None}


def test_a_stale_latch_does_not_caption_a_live_queue(tmp_path):
    # The latch outlives the hold on purpose, so it is read only while held.
    tts = _tts(tmp_path)
    (tts / ".autohold").write_text("volume")
    (tts / ".hold-since").write_text(str(int(time.time()) - 600))
    assert agentvibes.hold_state(tts) == {"until": None, "waiting": 0}


# --- release ------------------------------------------------------------------

def test_release_says_so_when_quiet_sh_is_missing(tmp_path):
    tts = _tts(tmp_path)
    agentvibes.set_hold(tts, None)
    assert agentvibes.release_held(tts) == {"released": 0, "detail": "release is unavailable: no executable tts/quiet.sh"}
    assert agentvibes.held(tts)


def test_release_runs_quiet_sh_resume_detached_and_waits_for_the_hold_to_go(tmp_path, monkeypatch):
    tts = _tts(tmp_path)
    script = _script(tts / "quiet.sh", "exit 0\n")
    agentvibes.set_hold(tts, None)
    (tts / "held" / "Alto").write_text("1\tproj/main\twaiting\n")
    started = []

    def popen(argv, **kwargs):
        started.append((argv, kwargs.get("shell"), kwargs.get("start_new_session")))
        (tts / "hold").unlink()
    monkeypatch.setattr(subprocess, "Popen", popen)
    assert agentvibes.release_held(tts) == {"released": 1, "detail": "", "until": None, "waiting": 1}
    assert started == [([str(script), "resume"], False, True)]


def test_a_hold_that_does_not_lift_is_reported_not_claimed(tmp_path, monkeypatch):
    tts = _tts(tmp_path)
    _script(tts / "quiet.sh", "exit 0\n")
    agentvibes.set_hold(tts, None)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(agentvibes.time, "sleep", lambda s: None)
    assert agentvibes.release_held(tts) == {"released": 0,
                                            "detail": "the hold did not lift: quiet.sh may have failed to start"}


def test_release_really_runs_the_script(tmp_path):
    tts = _tts(tmp_path)
    _script(tts / "quiet.sh", '[ "$1" = resume ] && rm -f "$(dirname "$0")/hold"\n')
    agentvibes.set_hold(tts, None)
    assert agentvibes.release_held(tts)["detail"] == "" and not agentvibes.held(tts)


# --- the test clip --------------------------------------------------------------

def test_the_test_clip_says_so_when_quiet_sh_is_missing(tmp_path):
    out = agentvibes.speak_test(_tts(tmp_path))
    assert out["spoken"] is False and "unavailable" in out["detail"]


def test_the_test_clip_runs_quiet_sh_test_and_returns_its_last_line(tmp_path):
    tts = _tts(tmp_path)
    _script(tts / "quiet.sh", '[ "$1" = test ] && echo "rendering" && echo "spoke as Alto at gain 8"\n')
    assert agentvibes.speak_test(tts) == {"spoken": True, "detail": "spoke as Alto at gain 8"}


def test_a_refused_test_clip_is_not_claimed_as_heard(tmp_path):
    # A gate stopping it is no failure of the button, and no clip either.
    tts = _tts(tmp_path)
    _script(tts / "quiet.sh", 'echo "not spoken: muted."\n')
    assert agentvibes.speak_test(tts)["detail"] == "not spoken: muted."


# --- stop -----------------------------------------------------------------------

def _stop(tmp_path, with_script=True):
    tts = _tts(tmp_path)
    (tts / "busy").write_text("1789999999.000\n")
    if with_script:
        _script(tts / "stop.sh", "exit 0\n")
    return tts


def test_stop_is_available_only_with_its_script(tmp_path):
    assert agentvibes.stop_available(_stop(tmp_path / "a", with_script=False)) is False
    assert agentvibes.stop_available(_stop(tmp_path / "b")) is True
    out = agentvibes.stop_playback(_stop(tmp_path / "c", with_script=False))
    assert out["stopped"] is False and "unavailable" in out["detail"]


def test_stop_runs_its_script_with_no_arguments_and_frees_the_queue(tmp_path, monkeypatch):
    tts, seen = _stop(tmp_path), []

    def run(argv, **kwargs):
        seen.append((argv, kwargs.get("shell")))
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(subprocess, "run", run)
    assert agentvibes.stop_playback(tts) == {"stopped": True, "queue_cleared": True, "detail": "stopped"}
    assert seen == [([str(tts / "stop.sh")], False)]
    assert (tts / "busy").read_text() == "0\n"


@pytest.mark.parametrize("outcome", ["failed", "hung"])
def test_the_queue_is_freed_even_when_stopping_fails(tmp_path, monkeypatch, outcome):
    # A sleeping player must not leave every other session waiting.
    tts = _stop(tmp_path)

    def run(argv, **kwargs):
        if outcome == "hung":
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))
        return subprocess.CompletedProcess(argv, 255, "", "no route")
    monkeypatch.setattr(subprocess, "run", run)
    out = agentvibes.stop_playback(tts)
    assert out["stopped"] is False and out["queue_cleared"] is True
    assert out["detail"] == ("no route" if outcome == "failed" else "the phone timed out")
    assert (tts / "busy").read_text() == "0\n"


# --- the roundup ------------------------------------------------------------------

def _waiting(tmp_path, clips, sums=None, lead=None, hold=True):
    tts = _tts(tmp_path)
    if hold:
        (tts / "hold").write_text("0\n")
    for who, count in clips.items():
        (tts / "held" / who).write_text("".join(f"1\t-/-\tclip {i}\n" for i in range(count)))
    for who, (count, text) in (sums or {}).items():
        (tts / "held" / f".sum-{who}").write_text(f"{count}\n{text}")
    if lead is not None:
        (tts / "held" / ".lead").write_text(lead)
    return tts


SUMS = {"Alto": (2, "s"), "Bass": (1, "s")}


def test_current_summaries_and_their_lead_are_a_ready_roundup(tmp_path):
    assert agentvibes.roundup_ready(_waiting(tmp_path, {"Alto": 2, "Bass": 1}, SUMS, "Alto:2 Bass:1 \nNothing urgent."))
    # The speakers are a set: the coordinator lists them in its shell's order.
    assert agentvibes.roundup_ready(_waiting(tmp_path / "b", {"Alto": 2, "Bass": 1}, SUMS, "Bass:1 Alto:2 \nlead"))


@pytest.mark.parametrize("clips,sums,lead,hold", [
    ({"Alto": 2}, {"Alto": (2, "s")}, "Alto:2 \nlead", True),               # one speaker is never a roundup
    ({"Alto": 2, "Bass": 1}, {"Alto": (1, "s"), "Bass": (1, "s")}, "Alto:2 Bass:1 \nlead", True),   # stale summary
    ({"Alto": 2, "Bass": 1}, SUMS, "Alto:1 Bass:1 \nlead", True),           # stale lead
    ({"Alto": 2, "Bass": 1}, SUMS, None, True),                             # no lead
    ({"Alto": 2, "Bass": 1}, SUMS, "Alto=2 Bass=1\nlead", True),            # not the format
    ({"Alto": 2, "Bass": 1}, SUMS, "Alto:2 Bass:1 \nlead", False),          # not held
])
def test_anything_else_is_not_ready(tmp_path, clips, sums, lead, hold):
    assert agentvibes.roundup_ready(_waiting(tmp_path, clips, sums, lead, hold)) is False


def test_no_spool_is_not_ready(tmp_path):
    tts = tmp_path / "tts"
    tts.mkdir()
    (tts / "hold").write_text("0\n")
    assert agentvibes.roundup_ready(tts) is False


# --- a live call's hold -------------------------------------------------------------

def test_a_call_hold_is_an_expiry_file_for_one_speaker(tmp_path):
    tts = tmp_path / "tts"
    tts.mkdir()
    assert agentvibes.call_hold(tts, "Alto", minutes=5, now=1000.0) is True
    assert (tts / "callhold-Alto").read_text() == "1300\n"
    assert agentvibes.call_hold(tts, "../hold", now=1000.0) is False
    assert agentvibes.call_hold(tmp_path / "nowhere", "Alto") is False


def test_a_call_release_runs_quiet_sh_with_one_fixed_verb(tmp_path):
    tts = tmp_path / "tts"
    tts.mkdir()
    script, calls = _script(tts / "quiet.sh", "exit 0\n"), []

    def run(argv, **kwargs):
        calls.append((argv, kwargs.get("shell")))
        return subprocess.CompletedProcess(argv, 0, "Alto: call over", "")
    assert agentvibes.call_release(tts, "Alto", run=run) == {"ok": True, "detail": ""}
    assert calls == [([str(script), "callrelease", "Alto"], False)]
    assert agentvibes.call_release(tts, "Alto; rm", run=run)["ok"] is False and len(calls) == 1


def test_a_call_release_without_quiet_sh_still_lifts_the_hold(tmp_path):
    tts = tmp_path / "tts"
    tts.mkdir()
    agentvibes.call_hold(tts, "Alto", now=1000.0)
    got = agentvibes.call_release(tts, "Alto", run=lambda *a, **k: None)
    assert got["ok"] is False and "quiet.sh" in got["detail"] and not (tts / "callhold-Alto").exists()
