"""Speech coordinator controls use a temporary coordinator only: speakers and
their presets, installed voices, music, announcements, subtitles and mutes."""
import json
import time

import pytest

from agent_companion import addons, agentvibes, commsbusy


def _tts(tmp_path, with_dir=True):
    base = tmp_path / "home"
    (base / "config").mkdir(parents=True)
    (base / "audio" / "tracks").mkdir(parents=True)
    for name in ("one.mp3", "two.mp3"):
        (base / "audio" / "tracks" / name).write_bytes(b"")
    (base / "config" / "audio-effects.cfg").write_text(
        "default|gain 8|one.mp3|0.70\n"
        "Alto|gain 8|one.mp3|0.70\n"
        "Bass|gain 8 reverb 30|two.mp3|0.50\n")
    (base / "config" / "background-music-enabled.txt").write_text("true\n")
    tts = base / "tts"
    if with_dir:
        tts.mkdir()
    return tts


def _rows(tts):
    return {row["name"]: row for row in agentvibes.speakers(tts)}


# --- the overlay is optional ------------------------------------------------

def test_controls_are_unavailable_until_the_coordinator_layout_exists(tmp_path):
    assert not agentvibes.available(_tts(tmp_path / "a", with_dir=False))
    assert agentvibes.available(_tts(tmp_path / "b"))


def test_reading_a_missing_config_is_empty_not_fatal(tmp_path):
    assert agentvibes.speakers(tmp_path / "nope") == []
    assert agentvibes.tracks(tmp_path / "nope") == []


def test_no_addon_busy_reason_is_a_safe_default(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_COMPANION_ADDONS", raising=False)
    addons.load.cache_clear()
    assert commsbusy.reason() is None


def test_a_manual_hold_holds_comms(tmp_path):
    tts = _tts(tmp_path)
    assert not commsbusy.held(tts)
    agentvibes.set_hold(tts, None)
    assert commsbusy.held(tts) and agentvibes.held(tts)


# --- speakers and presets ---------------------------------------------------

def test_parses_rows(tmp_path):
    rows = _rows(_tts(tmp_path))
    assert rows["Bass"]["track"] == "two.mp3" and rows["Bass"]["volume"] == 0.5
    assert rows["Alto"]["preset"] == "plain" and rows["Bass"]["preset"] == "warm"


def test_an_unrecognised_chain_reports_as_custom(tmp_path):
    tts = _tts(tmp_path)
    (tts.parent / "config" / "audio-effects.cfg").write_text("Alto|gain 3 flanger|one.mp3|0.5\n")
    assert agentvibes.speakers(tts)[0]["preset"] == "custom"


def test_a_malformed_line_is_skipped(tmp_path):
    tts = _tts(tmp_path)
    cfg = tts.parent / "config" / "audio-effects.cfg"
    cfg.write_text(cfg.read_text() + "garbage-without-fields\n")
    assert [row["name"] for row in agentvibes.speakers(tts)] == ["default", "Alto", "Bass"]


def test_a_preset_matches_at_any_voice_gain():
    assert agentvibes._preset("gain 8 reverb 30") == "warm"
    assert agentvibes._preset("gain 12 reverb 30") == "warm"
    assert agentvibes._preset("gain -3 treble 4") == "bright"


def test_only_the_leading_gain_is_the_level():
    # distant's second gain is part of the effect.
    assert agentvibes.strip_leading_gain("gain 12 reverb 60 gain -2") == "reverb 60 gain -2"
    assert agentvibes._preset("gain 12 reverb 60 gain -2") == "distant"


def test_a_preset_is_stored_in_volume_conf_where_the_coordinator_reads_it(tmp_path):
    tts = _tts(tmp_path)
    row = agentvibes.update_speaker(tts, "Alto", preset="warm", track="two.mp3", volume=0.4)
    assert row["preset"] == "warm" and row["volume"] == 0.4 and row["track"] == "two.mp3"
    assert "effects.Alto=reverb 30" in (tts / "volume.conf").read_text().splitlines()
    assert agentvibes.effects_overrides(tts) == {"Alto": "reverb 30"}
    assert not (tts / "effects.override.tsv").exists()


def test_the_stored_chain_carries_no_level_gain(tmp_path):
    tts = _tts(tmp_path)
    agentvibes.update_speaker(tts, "Alto", preset="deep")
    assert "gain" not in agentvibes.effects_overrides(tts)["Alto"]


def test_plain_stores_an_empty_chain_not_an_absent_key(tmp_path):
    # Absent leaves the row's chain as it is; empty means no effects.
    tts = _tts(tmp_path)
    agentvibes.update_speaker(tts, "Bass", preset="plain")
    assert agentvibes.effects_overrides(tts)["Bass"] == ""
    assert _rows(tts)["Bass"]["preset"] == "plain"


def test_the_stored_preset_wins_over_a_row_that_has_not_caught_up(tmp_path):
    tts = _tts(tmp_path)
    (tts / "volume.conf").write_text("voice_gain=3\neffects.Bass=\n")
    assert _rows(tts)["Bass"]["preset"] == "plain"
    agentvibes.update_speaker(tts, "Alto", preset="distant")
    assert _rows(tts)["Alto"]["preset"] == "distant"


def test_a_preset_change_composes_the_row_at_the_configured_gain(tmp_path):
    tts = _tts(tmp_path)
    (tts / "volume.conf").write_text("# levels\nvoice_gain=3\nmusic_volume.Bass=0.5\n")
    agentvibes.update_speaker(tts, "Alto", preset="deep")
    rows = _rows(tts)
    assert rows["Alto"]["effects"] == "gain 3 pitch -150" and rows["Alto"]["preset"] == "deep"
    assert rows["Bass"]["effects"] == "gain 8 reverb 30" and rows["default"]["effects"] == "gain 8"
    assert (tts / "volume.conf").read_text().splitlines() == [
        "# levels", "voice_gain=3", "music_volume.Bass=0.5", "effects.Alto=pitch -150"]


def test_no_temp_file_is_left_behind(tmp_path):
    tts = _tts(tmp_path)
    agentvibes.update_speaker(tts, "Alto", preset="deep")
    assert sorted(p.name for p in (tts.parent / "config").iterdir()) == [
        "audio-effects.cfg", "background-music-enabled.txt"]
    assert sorted(p.name for p in tts.iterdir()) == ["volume.conf"]


def test_the_music_toggle_round_trips(tmp_path):
    tts = _tts(tmp_path)
    assert agentvibes.music_enabled(tts) is True
    agentvibes.set_music_enabled(tts, False)
    assert agentvibes.music_enabled(tts) is False


# --- hostile input ------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"name": "Nobody", "preset": "plain"},
    {"name": "Alto; rm -rf /", "preset": "plain"},
    {"name": "Alto", "preset": "gain 8; rm -rf /"},     # the chain is never supplied
    {"name": "Alto", "track": "../../../etc/passwd"},
    {"name": "Alto", "volume": -1},
    {"name": "Alto", "volume": 2},
    {"name": "Alto", "volume": "loud"},
])
def test_a_speaker_write_is_whitelisted(tmp_path, kwargs):
    tts = _tts(tmp_path)
    cfg = tts.parent / "config" / "audio-effects.cfg"
    before = cfg.read_text()
    with pytest.raises(ValueError):
        agentvibes.update_speaker(tts, **kwargs)
    assert cfg.read_text() == before and not (tts / "volume.conf").exists()


# --- voices -------------------------------------------------------------------

def _voiced(tmp_path):
    tts = _tts(tmp_path)
    models = tts.parent / "piper-voices"
    models.mkdir()
    for model in ("voice-a", "voice-b", "multi", "listed", "nosidecar"):
        (models / f"{model}.onnx").write_bytes(b"")
    (models / "voice-a.onnx.json").write_text(json.dumps({"num_speakers": 1}))
    (models / "voice-b.onnx.json").write_text(json.dumps({"num_speakers": 1}))
    (models / "multi.onnx.json").write_text(json.dumps(
        {"num_speakers": 3, "speaker_id_map": {"First": 0, "Second": 1, "Third": 2}}))
    (models / "listed.onnx.json").write_text(json.dumps([1, 2, 3]))
    return tts


def test_installed_models_are_the_voices(tmp_path):
    got = {voice["id"]: voice["label"] for voice in agentvibes.voices(_voiced(tmp_path))}
    assert got == {"multi:0": "First", "multi:1": "Second", "multi:2": "Third", "voice-a": "voice-a", "voice-b": "voice-b"}


def test_a_model_without_a_readable_sidecar_is_skipped(tmp_path):
    # Its speaker count is unknowable, and a guess offers silence.
    assert not {"nosidecar", "listed"} & {voice["model"] for voice in agentvibes.voices(_voiced(tmp_path))}


def test_no_models_is_no_voices(tmp_path):
    assert agentvibes.voices(tmp_path / "nope") == []


def test_a_voice_override_round_trips_and_survives_a_preset_change(tmp_path):
    tts = _voiced(tmp_path)
    agentvibes.update_speaker(tts, "Alto", voice="multi:1")
    agentvibes.update_speaker(tts, "Alto", preset="warm")
    assert _rows(tts)["Alto"]["voice"] == "multi:1" and _rows(tts)["Bass"]["voice"] == ""


def test_an_empty_voice_clears_only_that_override(tmp_path):
    tts = _voiced(tmp_path)
    agentvibes.update_speaker(tts, "Alto", voice="voice-a")
    agentvibes.update_speaker(tts, "Bass", voice="multi:2")
    agentvibes.update_speaker(tts, "Alto", voice="")
    assert {name: row["voice"] for name, row in _rows(tts).items()} == {"default": "", "Alto": "", "Bass": "multi:2"}


@pytest.mark.parametrize("voice", ["voice-c", "multi:99", "voice-a; rm -rf /", "../../etc/passwd",
                                   "voice-a\tBass\tevil", "voice-a\nBass\tevil"])
def test_a_voice_must_be_installed_and_plain(tmp_path, voice):
    with pytest.raises(ValueError):
        agentvibes.update_speaker(_voiced(tmp_path), "Alto", voice=voice)


# --- announcements, subtitles and mutes ----------------------------------------

def test_all_announcements_default_on(tmp_path):
    assert agentvibes.announcements(_tts(tmp_path)) == {kind: True for kind in agentvibes.ANNOUNCEMENTS}


def test_an_announcement_toggles_alone_in_the_format_the_hooks_parse(tmp_path):
    tts = _tts(tmp_path)
    agentvibes.set_announcement(tts, "prompt", False)
    assert agentvibes.announcements(tts) == {"prompt": False, "subagent": True, "complete": True}
    assert "prompt=off" in (tts / "announce.conf").read_text().splitlines()
    agentvibes.set_announcement(tts, "prompt", True)
    assert agentvibes.announcements(tts)["prompt"] is True


@pytest.mark.parametrize("kind", ["everything", "prompt=on\nevil"])
def test_an_unknown_announcement_is_refused(tmp_path, kind):
    with pytest.raises(ValueError):
        agentvibes.set_announcement(_tts(tmp_path), kind, False)


def test_subtitles_default_on_and_accept_the_shell_spellings(tmp_path):
    tts = _tts(tmp_path)
    assert agentvibes.subtitles(tts) is True
    for raw, expected in (("on", True), ("true", True), ("1", True), ("off", False), ("no", False), ("0", False)):
        (tts / "announce.conf").write_text(f"subtitles={raw}\n")
        assert agentvibes.subtitles(tts) is expected, raw


def test_announce_conf_writes_keep_each_others_keys_and_the_coordinators(tmp_path):
    tts = _tts(tmp_path)
    (tts / "announce.conf").write_text("something_new=42\n")
    agentvibes.set_subtitles(tts, False)
    agentvibes.set_announcement(tts, "complete", False)
    assert agentvibes.subtitles(tts) is False and agentvibes.announcements(tts)["complete"] is False
    assert "something_new=42" in (tts / "announce.conf").read_text().splitlines()


def test_subtitles_are_offered_only_when_the_coordinator_reads_them(tmp_path):
    tts = _tts(tmp_path)
    assert agentvibes.subtitles_available(tts) is False
    (tts / "speak.sh").write_text("#!/bin/sh\n# nothing\n")
    assert agentvibes.subtitles_available(tts) is False
    (tts / "speak.sh").write_text("#!/bin/sh\n# honours subtitles=on\n")
    assert agentvibes.subtitles_available(tts) is True


def test_mutes_expire_and_name_known_speakers(tmp_path):
    tts = _tts(tmp_path)
    assert agentvibes.mute_state(tts) == {"all": None, "speakers": {}}
    agentvibes.set_mute(tts, "all", None)
    assert agentvibes.mute_state(tts)["all"] == 0 and (tts / "mute").read_text().strip() == "0"
    until = agentvibes.set_mute(tts, "all", 30)["all"]
    assert time.time() + 29 * 60 < until < time.time() + 31 * 60
    agentvibes.set_mute(tts, "Bass", 5)
    assert "Bass" in agentvibes.mute_state(tts)["speakers"]
    agentvibes.set_mute(tts, "Bass", 0)
    assert not (tts / "mute-Bass").exists() and agentvibes.mute_state(tts)["speakers"] == {}
    (tts / "mute").write_text(f"{int(time.time()) - 10}\n")
    assert agentvibes.mute_state(tts)["all"] is None


@pytest.mark.parametrize("scope,minutes", [("Nobody", 5), ("Alto; rm -rf /", 5), ("../../etc/passwd", 5),
                                           ("all", 10 ** 9), ("all", -5), ("all", "5")])
def test_a_bad_mute_is_refused(tmp_path, scope, minutes):
    with pytest.raises(ValueError):
        agentvibes.set_mute(_tts(tmp_path), scope, minutes)


# --- the snapshot -------------------------------------------------------------

def test_the_snapshot_is_the_sound_pages_shape(tmp_path):
    tts = _voiced(tmp_path)
    agentvibes.set_mute(tts, "Alto", 10)
    agentvibes.set_announcement(tts, "prompt", False)
    snap = agentvibes.snapshot(tts)
    assert set(snap) == {"speakers", "tracks", "presets", "voices", "music_enabled", "stop_available", "quiet", "health"}
    assert snap["presets"] == sorted(agentvibes.PRESETS) and snap["tracks"] == ["one.mp3", "two.mp3"]
    assert any(voice["id"] == "multi:1" for voice in snap["voices"])
    assert snap["stop_available"] is False and snap["health"]["state"] == "unknown"
    quiet = snap["quiet"]
    assert set(quiet) == {"announcements", "mutes", "kinds", "levels", "subtitles", "subtitles_available",
                          "hold", "held", "roundup_ready"}
    assert quiet["announcements"]["prompt"] is False and "Alto" in quiet["mutes"]["speakers"]
    assert quiet["kinds"] == list(agentvibes.ANNOUNCEMENTS)
    assert quiet["hold"] == {"until": None, "waiting": 0} and quiet["held"] == [] and quiet["roundup_ready"] is False
