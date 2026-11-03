"""External coordinator paths stay inside their configured roots."""
from pathlib import Path

import pytest
from fastapi import HTTPException

from agent_companion import auditions, safe_paths
from agent_companion import voicelab
from agent_companion.server import comms, voicelab as voicelab_routes


def test_files_and_directories_refuse_escapes_and_symlinks(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "inside.txt").write_text("inside")
    (outside / "outside.txt").write_text("outside")
    (root / "file-link.txt").symlink_to(outside / "outside.txt")
    (root / "folder-link").symlink_to(outside, target_is_directory=True)

    assert safe_paths.file(root, "inside.txt") == (root / "inside.txt").resolve()
    assert safe_paths.file(root, "file-link.txt") is None
    assert safe_paths.file(root, Path("folder-link") / "outside.txt") is None
    assert safe_paths.file(root, Path("..") / "outside" / "outside.txt") is None
    assert safe_paths.file(root, outside / "outside.txt") is None
    assert safe_paths.directory(root, "folder-link") is None


def test_decisions_do_not_write_through_symlinked_directories(tmp_path):
    tts, outside = tmp_path / "tts", tmp_path / "outside"
    (tts / "auditions").mkdir(parents=True)
    outside.mkdir()
    (tts / "auditions" / "Trial").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="no such audition"):
        auditions.decide(tts, "Trial", 2)
    assert not (outside / "decision.json").exists()
    assert not (tts / "tiers.conf").exists()

    lab = tmp_path / "lab"
    lab.mkdir()
    (lab / "build").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="not a build"):
        voicelab.decide(lab, "build", True)
    assert not (outside / "decision.json").exists()


def test_file_routes_reject_linked_files_without_starting_a_server(tmp_path, monkeypatch):
    tts, outside = tmp_path / "tts", tmp_path / "outside.mp3"
    tts.mkdir()
    outside.write_bytes(b"outside")
    config = tmp_path / "config"
    config.mkdir()
    (config / "audio-effects.cfg").write_text("Speaker|gain 8||0.7\n")
    tracks = tmp_path / "audio" / "tracks"
    tracks.mkdir(parents=True)
    (tracks / "linked.mp3").symlink_to(outside)
    (tts / "station.conf").write_text("music=linked.mp3\n")
    outbox = tts / "outbox"
    outbox.mkdir()
    (outbox / "1.opus").symlink_to(outside)
    audition = tts / "auditions" / "Trial"
    audition.mkdir(parents=True)
    (audition / "linked.mp3").symlink_to(outside)
    lab = tmp_path / "lab"
    build = lab / "build"
    (build / "audition").mkdir(parents=True)
    (build / "voice.json").write_text("{}")
    (build / "audition" / "new-1.mp3").symlink_to(outside)
    clips = tmp_path / "clips"
    clips.mkdir()
    (clips / "100-Speaker.wav").symlink_to(outside)
    monkeypatch.setenv("AGENT_COMPANION_TTS_DIR", str(tts))
    monkeypatch.setenv("AGENT_COMPANION_VOICELAB_DIR", str(lab))
    monkeypatch.setenv("COMMS_CLIPS_DIR", str(clips))

    for call in (
        lambda: comms.clip("100-Speaker"),
        lambda: comms.tts_clip(1, "web"),
        lambda: comms.tts_track("linked.mp3"),
        comms.tts_bed,
        lambda: comms.voice_audition_clip("Trial", "linked.mp3"),
        lambda: voicelab_routes.get_clip("build", "new-1.mp3"),
    ):
        with pytest.raises(HTTPException) as error:
            call()
        assert error.value.status_code == 404
