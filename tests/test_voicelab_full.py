"""Voice-lab parsing uses only temporary coordinator fixtures."""
import json

from agent_companion import voicelab


def _tts(tmp_path):
    tts = tmp_path / "tts"
    (tts / "voicelab").mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "audio-effects.cfg").write_text("Speaker|gain 8||0.7\n")
    (tts / "voicelab" / "voicelab.py").write_text("STAGES = ['prepare', 'train', 'audition']\n")
    (tts / "remote-voices.conf").write_text("Speaker=speaker\n")
    return tts


def test_builds_have_stages_clips_and_safe_directory_names(tmp_path):
    tts, lab = _tts(tmp_path), tmp_path / "lab"
    build = lab / "speaker-build"
    (build / "audition").mkdir(parents=True)
    (build / "voice.json").write_text(json.dumps({"name": "Speaker"}))
    (build / ".done-prepare").write_text("done")
    (build / "audition" / "new-2.mp3").write_bytes(b"ID3")
    assert voicelab.is_build_dir("speaker-build") and not voicelab.is_build_dir("..")
    row = voicelab.builds(tts, lab, 100)["builds"][0]
    assert row["next"] == "train" and row["clips"] == {"new": ["new-2.mp3"], "live": []}


def test_cast_tier_and_decision_are_written_without_running_coordinator(tmp_path):
    tts, lab = _tts(tmp_path), tmp_path / "lab"
    build = lab / "speaker"
    build.mkdir(parents=True)
    (build / "voice.json").write_text("{}")
    voicelab.auditions.set_tier(tts, "Speaker", 3)
    voicelab.decide(lab, "speaker", True, now=100)
    assert voicelab.cast_name(tts, "speaker") == "Speaker"
    assert voicelab.decision_of(build) == {"want": True, "at": 100.0}
