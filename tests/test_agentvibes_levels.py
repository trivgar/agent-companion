"""Levels in volume.conf, as the coordinator reads them on every clip: voice
gain in dB, background music 0 to 1, either one per speaker as <key>.<Name>.
The file is configuration the coordinator also writes, so one key changes in
place and everything else in it stays."""
import pytest

from agent_companion import agentvibes


def _tts(tmp_path, body=None):
    tts = tmp_path / "tts"
    tts.mkdir()
    if body is not None:
        (tts / "volume.conf").write_text(body)
    return tts


def test_defaults_when_there_is_no_file(tmp_path):
    assert agentvibes.levels(_tts(tmp_path)) == {"voice_gain": 8, "music_volume": 0.7, "speakers": {}}


def test_reads_the_configured_values_and_ignores_comments(tmp_path):
    levels = agentvibes.levels(_tts(tmp_path, "# voice_gain=99\n\nvoice_gain=11\nmusic_volume=0.35\n"))
    assert levels["voice_gain"] == 11 and levels["music_volume"] == 0.35


def test_a_speakers_own_levels_are_reported_separately(tmp_path):
    levels = agentvibes.levels(_tts(tmp_path, "voice_gain=8\nmusic_volume=0.7\n"
                                              "voice_gain.Bass=6\nmusic_volume.Bass=0.4\neffects.Bass=\n"))
    assert levels["voice_gain"] == 8 and levels["speakers"] == {"Bass": {"voice_gain": 6, "music_volume": 0.4}}


def test_a_junk_value_reads_as_the_default(tmp_path):
    # A fat-fingered hand edit must not take the page down.
    levels = agentvibes.levels(_tts(tmp_path, "voice_gain=loud\nmusic_volume=\n"))
    assert levels["voice_gain"] == 8 and levels["music_volume"] == 0.7


def test_setting_a_level_changes_that_line_alone(tmp_path):
    tts = _tts(tmp_path, "# how loud\nvoice_gain=8\nmusic_volume=0.35\neffects.Bass=\nvoice_boost_music=6\n")
    assert agentvibes.set_level(tts, "voice_gain", 11)["voice_gain"] == 11
    assert (tts / "volume.conf").read_text() == "# how loud\nvoice_gain=11\nmusic_volume=0.35\neffects.Bass=\nvoice_boost_music=6\n"


def test_a_speakers_own_level_is_its_own_key(tmp_path):
    tts = _tts(tmp_path, "voice_gain=8\nmusic_volume=0.7\n")
    levels = agentvibes.set_level(tts, "music_volume", 0.4, speaker="Bass")
    assert levels["music_volume"] == 0.7 and levels["speakers"]["Bass"]["music_volume"] == 0.4
    assert "music_volume.Bass=0.4" in (tts / "volume.conf").read_text().splitlines()


def test_setting_creates_the_file_when_absent(tmp_path):
    tts = _tts(tmp_path)
    agentvibes.set_level(tts, "voice_gain", -3)
    assert (tts / "volume.conf").read_text() == "voice_gain=-3\n"


@pytest.mark.parametrize("key,value", [
    ("voice_gain", 40),        # sox clips well below this
    ("voice_gain", -60),
    ("music_volume", 1.5),
    ("music_volume", -0.1),
    ("bogus_key", 1),
    ("voice_gain", "loud"),
    ("voice_gain", True),
])
def test_out_of_range_and_unknown_are_refused(tmp_path, key, value):
    tts = _tts(tmp_path, "voice_gain=8\nmusic_volume=0.7\n")
    with pytest.raises(ValueError):
        agentvibes.set_level(tts, key, value)
    assert (tts / "volume.conf").read_text() == "voice_gain=8\nmusic_volume=0.7\n"


def test_a_speaker_name_cannot_forge_a_second_key(tmp_path):
    tts = _tts(tmp_path, "voice_gain=8\n")
    with pytest.raises(ValueError):
        agentvibes.set_level(tts, "voice_gain", 6, speaker="Bad\nvoice_gain=99")
    assert agentvibes.levels(tts)["voice_gain"] == 8


def test_an_effect_write_keeps_the_levels_in_the_same_file(tmp_path):
    tts = _tts(tmp_path, "voice_gain=11\n")
    agentvibes.set_effects_override(tts, "Bass", "reverb 30")
    agentvibes.set_effects_override(tts, "Bass", "")
    assert (tts / "volume.conf").read_text() == "voice_gain=11\neffects.Bass=\n"
    assert agentvibes.levels(tts)["voice_gain"] == 11 and agentvibes.effects_overrides(tts) == {"Bass": ""}
