"""What speak.log says: the transcript (what was said, held, muted or lost),
each speaker's last words for a sessions list, and whether clips are reaching
the player at all."""
import sys
import time
import types

from agent_companion import addons, agentvibes

STAMP = "2026-09-14 11:06:03"


def _log(tmp_path, *lines):
    tts = tmp_path / "tts"
    tts.mkdir(parents=True, exist_ok=True)
    (tts / "speak.log").write_text("\n".join(lines) + ("\n" if lines else ""))
    return tts


def _ago(seconds):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - seconds))


# --- the transcript -------------------------------------------------------------

def test_no_log_is_no_transcript(tmp_path):
    assert agentvibes.transcript(tmp_path / "nope") == []


def test_a_spoken_clip(tmp_path):
    (row,) = agentvibes.transcript(_log(tmp_path, f"{STAMP} SPOKE Alto 14.3s :: Alto here. The tests pass."))
    assert row == {"name": "Alto", "text": "Alto here. The tests pass.", "body": "The tests pass.", "state": "spoke",
                   "reason": None, "detail": "", "at": time.mktime(time.strptime(STAMP, "%Y-%m-%d %H:%M:%S"))}


def test_newest_first_and_limited(tmp_path):
    tts = _log(tmp_path, *[f"2026-09-14 11:0{i}:00 SPOKE Alto 5s :: line {i}" for i in range(6)])
    assert [row["text"] for row in agentvibes.transcript(tts, limit=3)] == ["line 5", "line 4", "line 3"]


def test_a_mute_names_its_speaker_or_its_reason(tmp_path):
    rows = agentvibes.transcript(_log(tmp_path, f"{STAMP} MUTED(Alto) :: you cannot hear this",
                                      f"{STAMP} MUTED(all) :: nor this"))
    assert [(row["name"], row["state"], row["reason"]) for row in rows] == [(None, "muted", "all"), ("Alto", "muted", None)]


def test_a_held_clip_carries_why(tmp_path):
    (row,) = agentvibes.transcript(_log(tmp_path, f"{STAMP} HELD(call) Bass :: waiting for you"))
    assert (row["name"], row["state"], row["reason"]) == ("Bass", "held", "call")


def test_a_play_failure_keeps_the_text_and_its_error(tmp_path):
    (row,) = agentvibes.transcript(_log(tmp_path, f"{STAMP} PLAY-FAIL Bass rc=0 :: no player :: the thing Bass said"))
    assert (row["state"], row["text"], row["detail"]) == ("failed", "the thing Bass said", "no player")


def test_a_delivery_failure_never_passes_its_error_off_as_speech(tmp_path):
    (row,) = agentvibes.transcript(_log(tmp_path, f"{STAMP} SCP-FAIL Bass rc=255 :: ssh: Could not resolve hostname"))
    assert row["state"] == "failed" and row["text"] is None and row["body"] is None
    assert "Could not resolve" in row["detail"]


def test_bookkeeping_and_malformed_lines_are_not_speech(tmp_path):
    tts = _log(tmp_path, "garbage with no timestamp", f"{STAMP} QUEUE Bass waiting 12.4s",
               f"{STAMP} OVERFLOW Bass dropped", f"{STAMP} SPOKE Bass 5s :: the only real line")
    assert [row["text"] for row in agentvibes.transcript(tts)] == ["the only real line"]


# --- the generated opener ---------------------------------------------------------

def _body(tmp_path, name, text):
    (row,) = agentvibes.transcript(_log(tmp_path, f"{STAMP} SPOKE {name} 9s :: {text}"))
    return row["body"]


def test_a_one_sentence_opener_goes(tmp_path):
    assert _body(tmp_path, "Alto", "Alto here, on the shop. Task four is green.") == "Task four is green."


def test_a_bare_name_opener_takes_its_project_sentence_with_it(tmp_path):
    assert _body(tmp_path, "Bass", "Bass. On the ledger. The spine is finished.") == "The spine is finished."


def test_a_second_sentence_the_speaker_said_stays(tmp_path):
    assert _body(tmp_path, "Bass", "Bass, on the shop. Let's get this over with. Merged.") == \
        "Let's get this over with. Merged."


def test_text_without_an_opener_is_left_alone(tmp_path):
    assert _body(tmp_path, "Bass", "On second thought, that is wrong. Reverting.") == \
        "On second thought, that is wrong. Reverting."


def test_a_clip_that_is_only_its_opener_keeps_it(tmp_path):
    assert _body(tmp_path, "Alto", "Alto here, on the shop.") == "Alto here, on the shop."


def test_an_ellipsis_is_not_a_sentence_end(tmp_path):
    assert _body(tmp_path, "Tenor", "Tenor here... on scratch. The suite is green.") == "The suite is green."


# --- last said ----------------------------------------------------------------------

def test_each_speakers_newest_clip_with_words_wins(tmp_path):
    tts = _log(tmp_path,
               "2026-09-22 10:00:00 SPOKE Bass 3.1s :: Bass. On the shop. Older line.",
               "2026-09-22 10:05:00 QUEUE Bass waiting 2s",
               "2026-09-22 10:06:00 SPOKE Bass 4.0s :: Bass. On the shop. Newest line.",
               "2026-09-22 10:07:00 HELD(paused) Alto :: A question, when convenient.",
               "2026-09-22 10:08:00 SCP-FAIL Bass :: ssh: connect to host phone port 8022: timed out")
    said = agentvibes.last_said(tts)
    assert said["Bass"]["text"] == "Newest line." and said["Bass"]["state"] == "spoke"
    # A held clip was still said.
    assert said["Alto"] == {"text": "A question, when convenient.", "state": "held",
                            "at": time.mktime(time.strptime("2026-09-22 10:07:00", "%Y-%m-%d %H:%M:%S"))}


def test_no_log_is_nothing_said(tmp_path):
    assert agentvibes.last_said(tmp_path / "tts") == {}


def test_a_changed_log_is_read_again(tmp_path):
    tts = _log(tmp_path, "2026-09-22 10:00:00 SPOKE Bass 1s :: Bass. On scratch. First.")
    assert agentvibes.last_said(tts)["Bass"]["text"] == "First."
    _log(tmp_path, "2026-09-22 10:00:00 SPOKE Bass 1s :: Bass. On scratch. First.",
         "2026-09-22 10:01:00 SPOKE Bass 1s :: Bass. On scratch. Second one.")
    assert agentvibes.last_said(tts)["Bass"]["text"] == "Second one."


# --- delivery health ------------------------------------------------------------------

def test_unknown_without_a_log_or_an_attempt(tmp_path):
    assert agentvibes.tts_health(tmp_path / "nope")["state"] == "unknown"
    assert agentvibes.tts_health(_log(tmp_path, f"{_ago(60)} QUEUE Alto waiting 2.0s"))["state"] == "unknown"


def test_up_after_a_clip_played_however_long_ago(tmp_path):
    health = agentvibes.tts_health(_log(tmp_path, f"{_ago(7200)} SPOKE Alto 5.0s :: hours ago"))
    assert health["state"] == "up" and health["age_seconds"] > 3600
    assert health["kind"] is None and health["fix"] is None and health["consecutive_failures"] == 0


def test_down_after_a_fresh_failure_names_its_kind(tmp_path):
    health = agentvibes.tts_health(_log(tmp_path / "a", f"{_ago(120)} SPOKE Alto 5.0s :: fine",
                                        f"{_ago(10)} SCP-FAIL Alto rc=255 :: Bad packet length 99."))
    assert (health["state"], health["kind"], health["detail"]) == ("down", "undelivered", "Bad packet length 99.")
    health = agentvibes.tts_health(_log(tmp_path / "b", f"{_ago(20)} PLAY-FAIL Alto rc=0 :: no player :: hello"))
    assert (health["state"], health["kind"], health["detail"]) == ("down", "silent", "no player")


def test_the_newest_attempt_wins_and_a_mute_is_no_attempt(tmp_path):
    tts = _log(tmp_path, f"{_ago(120)} SCP-FAIL Alto rc=255 :: boom", f"{_ago(60)} SPOKE Alto 5.0s :: recovered",
               f"{_ago(5)} MUTED(all) :: quiet please", "not a log line at all")
    assert agentvibes.tts_health(tts)["state"] == "up"


def test_failures_in_a_row_are_counted(tmp_path):
    tts = _log(tmp_path, f"{_ago(300)} SPOKE Alto 5.0s :: fine", f"{_ago(60)} SCP-FAIL Alto rc=255 :: gone",
               f"{_ago(40)} PLAY-FAIL Bass rc=0 :: silent :: hi", f"{_ago(20)} PLAY-FAIL Alto rc=0 :: silent :: again")
    health = agentvibes.tts_health(tts)
    assert health["consecutive_failures"] == 3 and health["kind"] == "silent"


def test_a_hold_after_a_failure_means_nothing_is_known(tmp_path):
    # A held clip is no delivery attempt: it is proof that none was made.
    tts = _log(tmp_path, f"{_ago(4200)} SPOKE Bass 3.0s :: hello",
               f"{_ago(3900)} SCP-FAIL Bass rc=255 :: Bad packet length 123.",
               f"{_ago(3840)} SCP-FAIL Alto rc=255 :: Bad packet length 456.",
               f"{_ago(3000)} HELD(paused) Tenor :: something", f"{_ago(300)} HELD(paused) Alto :: something else")
    health = agentvibes.tts_health(tts)
    assert (health["state"], health["kind"], health["fix"]) == ("unknown", None, None)
    assert "on hold" in health["detail"] and health["consecutive_failures"] == 2


def test_an_old_failure_with_nothing_since_is_the_past(tmp_path):
    health = agentvibes.tts_health(_log(tmp_path, f"{_ago(5400)} SPOKE Bass 3.0s :: hello",
                                        f"{_ago(2400)} SCP-FAIL Bass rc=255 :: Bad packet length 123."))
    assert health["state"] == "unknown" and "40 minutes" in health["detail"]


def test_a_hold_before_a_failure_does_not_excuse_it(tmp_path):
    health = agentvibes.tts_health(_log(tmp_path, f"{_ago(1800)} HELD(paused) Tenor :: waiting",
                                        f"{_ago(240)} SCP-FAIL Bass rc=255 :: Bad packet length 123."))
    assert (health["state"], health["kind"]) == ("down", "undelivered")


def test_only_the_tail_is_read(tmp_path):
    tts = _log(tmp_path, *([f"{_ago(9999)} QUEUE Alto waiting 1s"] * 5000), f"{_ago(5)} SPOKE Alto 5.0s :: ok")
    assert agentvibes.tts_health(tts)["state"] == "up"


def _plug(monkeypatch, **fns):
    mod = types.ModuleType("fake_addons")
    for key, value in fns.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, "fake_addons", mod)
    monkeypatch.setenv("AGENT_COMPANION_ADDONS", "fake_addons")
    addons.load.cache_clear()


def test_a_failure_carries_no_repair_steps_without_an_add_on(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_COMPANION_ADDONS", raising=False)
    monkeypatch.setenv("AGENT_COMPANION_CONFIG", "/nonexistent")
    addons.load.cache_clear()
    health = agentvibes.tts_health(_log(tmp_path, f"{_ago(10)} SCP-FAIL Alto rc=255 :: boom"))
    assert health["state"] == "down" and health["fix"] is None
    addons.load.cache_clear()


def test_a_failure_carries_the_add_ons_repair_steps_for_its_kind(tmp_path, monkeypatch):
    asked = []

    def fix(kind):
        asked.append(kind)
        return {"what": "The player is wedged.", "where": "On the phone:", "cmd": "restart-player", "extra": "x"}
    _plug(monkeypatch, tts_fix=fix)
    health = agentvibes.tts_health(_log(tmp_path, f"{_ago(10)} SCP-FAIL Alto rc=255 :: boom"))
    assert health["fix"] == {"what": "The player is wedged.", "where": "On the phone:", "cmd": "restart-player", "after": ""}
    assert asked == ["undelivered"]
    assert agentvibes.tts_health(_log(tmp_path / "b", f"{_ago(10)} SPOKE Alto 1s :: fine"))["fix"] is None
    assert asked == ["undelivered"]
    addons.load.cache_clear()
