"""The voice lab's reader and routes: agent_companion/voicelab.py, server/voicelab.py and
the three add-on hooks they call (addons.voicelab_now, voicelab_notes, voicelab_decided).
Every test builds its own coordinator folder, lab and settings in tmp_path; none reads
a real one."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from agent_companion import addons, auditions, voices
from agent_companion import voicelab as vl

NOW = 1792440000.0
STAGES = ["fetch", "sep", "diarize", "cut", "embed", "transcribe", "pick", "train", "sweep", "score", "audition"]
IDENTITY = ("#!/usr/bin/env bash\n"
            "declare -A VOICE_TIER=([ada]=1 [lin]=3 [kit]=3 [sam]=2)\n"
            "tier_of() { :; }\n")
REMOTE = ("# Speakers on gpu1.\n# name=voice\n"
          "Ada=ada\nLin=lin\n# a comment\nKit=kit\nMax=max\nDot=dot\n")
SERVER = ("VOICES = {\n"
          "    'ada':   dict(gpt=f'{H_}/gsv-train/ada/GPT_weights/ada-e10.ckpt',\n"
          "                 sovits='x'),\n"
          "    'max': dict(kokoro='bf_emma+bf_isabella', speed=1.0, rvc=False),\n"
          "    'kit':  dict(gpt='k.ckpt'),\n"
          "    'lin':  dict(gpt='l.ckpt'),\n"
          "}\n")
COMMITS = ([("Queue: a caller is not introduced twice", NOW - 9000)]
           + [(f"Kit voice tweak {i}", NOW - 8000 + i) for i in range(12)]
           + [("Lin joins the cast: voice tier 3 until the audition", NOW - 5000),
              ("A headless job is never an agent", NOW - 4500),
              ("A headless job is never an agent: claude -p runs claim no voice the sessions tab showed. " + "x " * 20
               + "Dot, Max, Ada, Lin, Kit for the mail job", NOW - 4600),
              ("Queue: Dot and Max share a worker", NOW - 4400),
              ("Kit: the voicelab rebuild goes live", NOW - 4000),
              ("Lexicon: Adagio voice is said right", NOW - 3000)])

TOKEN = "t" * 32
OWNER = "o" * 64
KEY = {"x-agent-companion-key": OWNER}
SAME = {"origin": "http://testserver", "host": "testserver"}
WRITE = {**KEY, **SAME}


def _git(repo, *args, env=None):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   check=True, capture_output=True, env=env)


@pytest.fixture(scope="module")
def _git_log(tmp_path_factory):
    """A repository holding COMMITS, made once; each test copies its .git."""
    repo = tmp_path_factory.mktemp("tts-git")
    _git(repo, "init", "-q")
    for subject, at in COMMITS:
        env = dict(os.environ, GIT_AUTHOR_DATE=f"@{int(at)} +0000", GIT_COMMITTER_DATE=f"@{int(at)} +0000")
        _git(repo, "commit", "-q", "--allow-empty", "-m", subject, env=env)
    return repo / ".git"


@pytest.fixture(autouse=True)
def _fresh():
    """No add-on, and no cached git history or voice state, before and after each test."""
    vl._git_cache.clear()
    voices._cache.clear()
    addons.load.cache_clear()
    yield
    vl._git_cache.clear()
    voices._cache.clear()
    addons.load.cache_clear()


@pytest.fixture
def tts(tmp_path, _git_log):
    """A coordinator folder with the lab script, the cast, tiers, the voice server and a git log.
    Its parent holds config/audio-effects.cfg, which is what makes comms set up."""
    t = tmp_path / "tts"
    (t / "voicelab").mkdir(parents=True)
    (t / "voice-server").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "audio-effects.cfg").write_text("Ada|gain 8||0.7\n")
    (t / "voicelab" / "voicelab.py").write_text(
        "import json\nSTAGES = " + json.dumps(STAGES).replace('"', "'") + "\nHOSTS = ['ssh']\n")
    (t / "identity.sh").write_text(IDENTITY)
    (t / "tiers.conf").write_text("lin=1\nbogus=7\n")
    (t / "remote-voices.conf").write_text(REMOTE)
    (t / "voice-server" / "voice_server.py").write_text(SERVER)
    shutil.copytree(_git_log, t / ".git")
    return t


def _settings(tmp_path, monkeypatch, text):
    """A settings file with these lines, in place of the one conftest points at."""
    path = tmp_path / "settings.toml"
    path.write_text(text)
    monkeypatch.setenv("AGENT_COMPANION_CONFIG", str(path))


def _plug(monkeypatch, **fns):
    """An add-on module with these functions, named by the `addons` setting."""
    module = types.ModuleType("fake_voicelab_addons")
    for name, fn in fns.items():
        setattr(module, name, fn)
    monkeypatch.setitem(sys.modules, "fake_voicelab_addons", module)
    monkeypatch.setenv("AGENT_COMPANION_ADDONS", "fake_voicelab_addons")
    addons.load.cache_clear()


def _build(lab, d, name, done, at=NOW - 60, log=None, scores=None):
    b = lab / d
    b.mkdir(parents=True)
    (b / "voice.json").write_text(json.dumps({"name": d, "Name": name, "run": f"{d}-vl", "ids": [], "texts": {}}))
    for s in done:
        f = b / f".done-{s}"
        f.write_text(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at)) + "\n")
        os.utime(f, (at, at))
    if log is not None:
        (b / "run.log").write_text(log)
        os.utime(b / "run.log", (at, at))
    if scores is not None:
        (b / "scores.json").write_text(json.dumps(scores))
    return b


def _zsb(lab, d, at=NOW - 60, audition=True):
    b = lab / d
    b.mkdir(parents=True)
    (b / "voice.json").write_text(json.dumps({"name": d, "engine": "IndexTTS2 zero-shot"}))
    (b / "audition").mkdir()
    (b / "audition" / "new-1.mp3").write_bytes(b"x")
    if audition:
        (b / ".done-audition").write_text("x")
        os.utime(b / ".done-audition", (at, at))
    return b


RANK = {"ceiling": [0.77, 0.88], "renders": [],
        "rank": [{"tag": "g10s8_D_cut0", "ecapa": 0.7818, "rz": 0.8866, "wer": 0.0656, "mos": 0.0, "score": 1.64},
                 {"tag": "g15s8_D_cut0", "ecapa": 0.75, "rz": 0.88, "wer": 0.05, "mos": 0.0, "score": 1.62},
                 {"tag": "live", "ecapa": 0.63, "rz": 0.87, "wer": 0.11, "mos": 0.0, "score": 1.47}]}


# --- the reader: builds ----------------------------------------------------------------

def test_the_stage_list_is_read_from_the_lab_script(tts, tmp_path):
    assert vl.stages(tts) == STAGES
    assert vl.stages(tmp_path / "nowhere") is None
    (tts / "voicelab" / "voicelab.py").write_text("STAGES = ['fetch', __import__('os')]\n")
    assert vl.stages(tts) is None, "never evaluated, only read"


@pytest.mark.parametrize("text", [
    "nothing here\n", "STAGES = []\n", "STAGES = [1, 2]\n", "STAGES = [!]\n",
    "STAGES = ['a b']\n", "STAGES = ['../x']\n", "STAGES = ['" + "x" * 65 + "']\n"])
def test_a_stage_list_that_is_not_plain_names_is_not_one(tts, text):
    (tts / "voicelab" / "voicelab.py").write_text(text)
    assert vl.stages(tts) is None


def test_a_build_part_way_through(tts, tmp_path):
    lab = tmp_path / "lab"
    _build(lab, "kit", "Kit", STAGES[:7], log="x" * 9000 + "\n10:47:01 == kit: train\n")
    got = vl.builds(tts, lab, NOW)
    assert got["ok"] and [b["name"] for b in got["builds"]] == ["Kit"]
    b = got["builds"][0]
    assert (b["next"], b["step"], b["of"]) == ("train", 8, 11)
    assert [s["done"] for s in b["stages"]] == [True] * 7 + [False] * 4
    assert b["stages"][0]["at"] == time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - 60))
    assert b["stages"][7]["at"] is None
    assert b["line"] == "10:47:01 == kit: train" and b["line_at"] == NOW - 60
    assert b["scores"] is None, "no figures before the score stage is done"


def test_run_log_is_read_from_its_last_4_kb_only(tts, tmp_path):
    lab = tmp_path / "lab"
    _build(lab, "lin", "Lin", STAGES[:2], log="start\n" + "y" * 6000 + "\n")
    line = vl.builds(tts, lab, NOW)["builds"][0]["line"]
    # The last 4096 bytes are 4095 y's and the newline: the start of the file is never read.
    assert line == "y" * (vl.LOG_TAIL - 1)


def test_a_build_with_no_run_log_has_no_line(tts, tmp_path):
    lab = tmp_path / "lab"
    _build(lab, "kit", "Kit", STAGES[:2])
    (b,) = vl.builds(tts, lab, NOW)["builds"]
    assert b["line"] is None and b["line_at"] is None and b["active_at"] == NOW - 60
    _build(lab, "lin", "Lin", [], log="\n   \n")
    lin = {x["dir"]: x for x in vl.builds(tts, lab, NOW)["builds"]}["lin"]
    assert lin["line"] is None and lin["line_at"] == NOW - 60, "a log of blank lines says nothing"


def test_a_build_is_named_by_its_voice_json(tts, tmp_path):
    lab = tmp_path / "lab"
    for d, v in (("one", {"Name": "Kit", "name": "x"}), ("two", {"name": "Lin"}), ("three", {}),
                 ("four", {"Name": "../out"}), ("five", {"Name": "N" * 33})):
        (lab / d).mkdir(parents=True)
        (lab / d / "voice.json").write_text(json.dumps(v))
    (lab / "list").mkdir()
    (lab / "list" / "voice.json").write_text("[1]")
    (lab / "junk").mkdir()
    (lab / "junk" / "voice.json").write_text("{nope")
    (lab / "bad name!").mkdir()
    (lab / "bad name!" / "voice.json").write_text("{}")
    got = {b["dir"]: b["name"] for b in vl.builds(tts, lab, NOW)["builds"]}
    assert got == {"one": "Kit", "two": "Lin", "three": "three", "four": "four", "five": "five"}


def test_the_best_take_and_the_live_baseline(tts, tmp_path):
    lab = tmp_path / "lab"
    _build(lab, "kit", "Kit", STAGES[:10], scores=RANK)
    no_base = {"rank": [{"tag": "g10s6_C_cut0", "ecapa": 0.7, "rz": 0.88}]}
    _build(lab, "dot", "Dot", STAGES[:10], at=NOW - 120, scores=no_base)
    by = {b["name"]: b for b in vl.builds(tts, lab, NOW)["builds"]}
    assert by["Kit"]["scores"] == {"best": {"tag": "g10s8_D_cut0", "ecapa": 0.782, "rz": 0.887, "wer": 0.066},
                                   "baseline": {"tag": "live", "ecapa": 0.63, "rz": 0.87, "wer": 0.11}}
    assert by["Dot"]["scores"] == {"best": {"tag": "g10s6_C_cut0", "ecapa": 0.7, "rz": 0.88}, "baseline": None}, \
        "a figure scores.json does not have is absent, never 0"


def test_the_baseline_tags_are_a_setting(tts, tmp_path, monkeypatch):
    lab = tmp_path / "lab"
    rank = {"rank": [{"tag": "stock", "ecapa": 0.9}, {"tag": "g10s8_D_cut0", "ecapa": 0.78},
                     {"tag": "live", "ecapa": 0.63}]}
    _build(lab, "kit", "Kit", STAGES[:10], scores=rank)

    def scores():
        return vl.builds(tts, lab, NOW)["builds"][0]["scores"]
    assert (scores()["best"]["tag"], scores()["baseline"]["tag"]) == ("stock", "live"), "the default is live"
    _settings(tmp_path, monkeypatch, 'voicelab_baseline_tags = ["stock", "old"]\n')
    assert (scores()["best"]["tag"], scores()["baseline"]["tag"]) == ("g10s8_D_cut0", "stock")
    _settings(tmp_path, monkeypatch, 'voicelab_baseline_tags = ["none"]\n')
    assert (scores()["best"]["tag"], scores()["baseline"]) == ("stock", None), "live is an ordinary take now"


def test_the_baseline_tags_default_and_what_cannot_replace_it(tmp_path, monkeypatch):
    assert vl.baseline_tags() == ("live",) == vl.BASE_TAGS
    _settings(tmp_path, monkeypatch, 'voicelab_baseline_tags = ["stock", 5, ""]\n')
    assert vl.baseline_tags() == ("stock",), "only words count"
    for bad in ("[]", '["", 5]', '"live"', "7", "[[1]]"):
        _settings(tmp_path, monkeypatch, f"voicelab_baseline_tags = {bad}\n")
        assert vl.baseline_tags() == ("live",), bad


def test_a_scores_file_that_cannot_be_read_says_so(tts, tmp_path):
    lab = tmp_path / "lab"
    b = _build(lab, "kit", "Kit", STAGES[:10], scores={"rank": "nope"})

    def scores():
        return vl.builds(tts, lab, NOW)["builds"][0]["scores"]
    assert scores() == {"best": None, "baseline": None, "why": "scores.json has no ranking"}
    for text in ("{nope", json.dumps({"other": 1}), "[1]"):
        (b / "scores.json").write_text(text)
        assert scores() == {"best": None, "baseline": None, "why": "scores.json could not be read"}, text
    (b / "scores.json").unlink()
    assert scores()["why"] == "scores.json could not be read"
    (b / "scores.json").write_text(json.dumps({"rank": ["x", 5, {"tag": "live", "ecapa": True}]}))
    assert scores() == {"best": None, "baseline": {"tag": "live"}}, "a non-take is skipped; a bool is no figure"


def test_an_old_finished_build_is_history(tts, tmp_path):
    lab = tmp_path / "lab"
    old = NOW - 20 * 86400
    _build(lab, "dot", "Dot", STAGES, at=old)
    _build(lab, "lin", "Lin", STAGES, at=old)
    _build(lab, "max", "Max", STAGES, at=old)
    _build(lab, "kit", "Kit", STAGES, at=NOW - 5 * 86400)
    _build(lab, "notes", "Notes", [])
    (lab / "notes" / "voice.json").unlink()                       # a folder with no voice.json
    aud = tts / "auditions"
    (aud / "Lin").mkdir(parents=True)                              # still waiting on a decision
    (aud / "Max").mkdir()
    (aud / "Max" / "decision.json").write_text('{"tier": 1, "at": 1}')
    names = [b["name"] for b in vl.builds(tts, lab, NOW)["builds"]]
    assert sorted(names) == ["Kit", "Lin"], names


def test_an_old_zero_shot_build_is_history_by_the_same_rule(tts, tmp_path):
    lab = tmp_path / "lab"
    old = NOW - 20 * 86400
    _zsb(lab, "dot-index", at=old)
    _zsb(lab, "lin-index", at=old)
    _zsb(lab, "kit-index", at=NOW - 5 * 86400)
    (tts / "auditions" / "lin-index").mkdir(parents=True)          # named by its folder: still waiting
    assert sorted(b["dir"] for b in vl.builds(tts, lab, NOW)["builds"]) == ["kit-index", "lin-index"]


def test_a_source_that_cannot_be_read_says_which(tts, tmp_path):
    assert vl.builds(tts, tmp_path / "no-lab", NOW) == {"ok": False, "why": f"could not read {tmp_path / 'no-lab'}"}
    (tts / "voicelab" / "voicelab.py").unlink()
    assert vl.builds(tts, tmp_path, NOW)["why"] == "could not read the stage list in tts/voicelab/voicelab.py"
    (tts / "remote-voices.conf").unlink()
    assert vl.cast(tts, NOW) == {"ok": False, "why": "could not read tts/remote-voices.conf"}
    assert addons.voicelab_notes(["Kit"]) == []


# --- the right-now line: the default, and the add-on's -----------------------------------

KIT_BUILD = {"dir": "kit", "name": "Kit", "step": 8, "of": 11, "active_at": NOW - 300}
STALE_BUILD = {"dir": "dot", "name": "Dot", "step": 3, "of": 11, "active_at": NOW - 3600}


@pytest.mark.parametrize("state,kind,text", [
    ({"real": True, "why": "", "note": ""}, "free", "free"),
    ({"real": True, "why": "", "note": "Voices on desk only: gpu1 is away"}, "free", "free"),
    ({"real": False, "why": "gpu1 is off or out of reach", "note": ""}, "busy", "gpu1 is off or out of reach"),
    ({"real": False, "why": "  gpu1's graphics card is busy \n", "note": ""}, "busy", "gpu1's graphics card is busy"),
    ({"real": False, "why": "voice lab is training", "note": ""}, "busy", "voice lab is training"),
    ({"real": False, "why": "", "note": ""}, "busy", "the voice host is not free"),
    ({"real": False}, "busy", "the voice host is not free"),
])
def test_right_now_reads_what_voices_py_knows(state, kind, text):
    got = vl.right_now(state, [STALE_BUILD, KIT_BUILD], NOW)
    assert got == {"kind": kind, "text": text}, "builds change nothing in the core's own line"


def test_right_now_when_the_voice_state_is_not_known():
    assert vl.right_now(None, [], NOW) == {"kind": "unknown", "text": "the voice host's state is not known here"}
    assert vl.right_now(None, [KIT_BUILD], NOW)["kind"] == "unknown"


def test_the_add_on_gives_the_right_now_line(monkeypatch):
    seen = []

    def line(state, builds, now):
        seen.append((state, [b["dir"] for b in builds], now))
        return {"kind": "training", "text": "training Kit, stage 8 of 11", "build": "kit", "extra": "dropped"}
    _plug(monkeypatch, voicelab_now=line)
    state = {"real": False, "why": "gpu1 is busy"}
    got = vl.right_now(state, [KIT_BUILD, STALE_BUILD], NOW)
    assert got == {"kind": "training", "text": "training Kit, stage 8 of 11", "build": "kit"}
    assert seen == [(state, ["kit", "dot"], NOW)]
    assert vl.right_now(None, [], NOW)["kind"] == "training", "asked even when nothing is known"


def test_the_add_on_may_give_less_than_all_three_words(monkeypatch):
    _plug(monkeypatch, voicelab_now=lambda *a: {"text": "words only", "kind": 5, "build": None})
    assert vl.right_now({"real": True}, [], NOW) == {"text": "words only"}


@pytest.mark.parametrize("answer", [None, "training", 5, ["text"], {}, {"kind": "busy"}, {"text": 5, "kind": "busy"}])
def test_a_malformed_right_now_answer_is_ignored(monkeypatch, answer):
    _plug(monkeypatch, voicelab_now=lambda *a: answer)
    assert vl.right_now({"real": True}, [], NOW) == {"kind": "free", "text": "free"}


def test_a_raising_add_on_keeps_the_default_right_now_line(monkeypatch):
    def boom(*a):
        raise RuntimeError("broken")
    _plug(monkeypatch, voicelab_now=boom)
    assert vl.right_now(None, [], NOW)["kind"] == "unknown"
    assert vl.right_now({"real": False, "why": "gpu1 is busy"}, [], NOW) == {"kind": "busy", "text": "gpu1 is busy"}


# --- the cast ---------------------------------------------------------------------------

def test_the_cast_with_tiers_engines_and_last_change(tts):
    got = vl.cast(tts, NOW)
    assert got["ok"] and "note" not in got
    by = {v["name"]: v for v in got["voices"]}
    assert list(by) == ["Ada", "Lin", "Kit", "Max", "Dot"]
    assert (by["Ada"]["tier"], by["Ada"]["tier_from"]) == (1, "identity.sh")
    assert (by["Lin"]["tier"], by["Lin"]["tier_from"]) == (1, "tiers.conf"), "a tapped tier wins"
    assert (by["Max"]["tier"], by["Max"]["tier_from"]) == (2, "default")
    assert [by[n]["engine"] for n in ("Ada", "Max", "Dot")] == ["GPT-SoVITS", "Kokoro", "unknown"]
    assert by["Kit"]["changes"][0]["subject"] == "Kit: the voicelab rebuild goes live"
    assert len(by["Kit"]["changes"]) == 10, "the last 10 only"
    assert by["Lin"]["changes"][0]["at"] == int(NOW - 5000)
    assert by["Ada"]["changes"] == [], "Adagio is not Ada: whole words only"
    assert by["Dot"]["changes"] == [], "a name without a voice word in the subject is no change"
    assert all("headless" not in c["subject"] for v in by.values() for c in v["changes"])


def test_a_subject_needs_a_voice_word(tts):
    by = {v["name"]: v for v in vl.cast(tts, NOW)["voices"]}
    assert "Lin joins the cast: voice tier 3 until the audition" in [c["subject"] for c in by["Lin"]["changes"]]
    assert not any(len(c["subject"]) > 121 for v in by.values() for c in v["changes"])
    assert by["Dot"]["changes"] == [] and by["Lin"]["changes"][0]["subject"].startswith("Lin")
    assert not any("Queue" in c["subject"] for v in by.values() for c in v["changes"])


def test_git_is_read_once_a_minute_from_the_tts_repo_only(tts):
    calls = []
    real = subprocess.run

    def run(args, **kw):
        calls.append(args)
        return real(args, **kw)
    assert vl.commits(tts, NOW, run=run)
    vl.commits(tts, NOW + 30, run=run)
    assert len(calls) == 1
    vl.commits(tts, NOW + 61, run=run)
    assert len(calls) == 2
    assert calls[0][:6] == ["git", "-C", str(tts), "log", "-n", "400"]


def test_a_git_failure_is_cached_too_so_a_hung_git_is_not_rerun_on_every_poll(tts):
    calls = []

    def run(args, **kw):
        calls.append(args)
        raise subprocess.TimeoutExpired(args, 10)
    assert vl.commits(tts, NOW, run=run) is None
    assert vl.commits(tts, NOW + 30, run=run) is None
    assert len(calls) == 1
    vl.commits(tts, NOW + 61, run=run)
    assert len(calls) == 2


def test_git_output_is_read_line_by_line_and_a_failed_run_is_none(tts):
    def run_with(code, out):
        return lambda args, **kw: subprocess.CompletedProcess(args, code, out, "")
    out = ("a" * 40 + "\t100\tfirst\n" + "junk line\n" + "b" * 40 + "\tnot-a-time\tx\n"
           + "c" * 40 + "\t200\t" + "s" * 300 + "\n" + "d" * 40 + "\t300\twith\ttabs\n")
    got = vl.commits(tts, NOW, run=run_with(0, out))
    assert [(c["sha"], c["at"]) for c in got] == [("a" * 10, 100), ("c" * 10, 200), ("d" * 10, 300)]
    assert got[1]["subject"] == "s" * 119 + "…", "a subject is its first 120 characters"
    assert got[2]["subject"] == "with\ttabs"
    vl._git_cache.clear()
    assert vl.commits(tts, NOW, run=run_with(128, "")) is None


def test_without_git_history_the_cast_still_shows(tts):
    shutil.rmtree(tts / ".git")
    got = vl.cast(tts, NOW)
    assert got["ok"] and got["note"] == "the tts repo's history could not be read"
    assert all(v["changes"] is None for v in got["voices"])


def test_tiers_are_read_as_the_coordinators_script_reads_them(tts):
    (tts / "tiers.conf").write_text("# a comment\nLIN = 1\nKIT=2\nkit=3\nsam=9\nbad\n=2\nada=\n")
    got = vl.tiers(tts)
    assert got["lin"] == (1, "tiers.conf"), "spaces and case do not matter"
    assert got["kit"] == (3, "tiers.conf"), "the last line wins"
    assert got["sam"] == (2, "identity.sh") and got["ada"] == (1, "identity.sh"), "a bad line changes nothing"
    assert sorted(got) == ["ada", "kit", "lin", "sam"]
    (tts / "identity.sh").unlink()
    assert vl.tiers(tts) == {"lin": (1, "tiers.conf"), "kit": (3, "tiers.conf")}
    (tts / "tiers.conf").unlink()
    assert vl.tiers(tts) == {}


def test_the_engines_are_read_from_the_voice_server_by_regex(tts):
    assert vl.engines(tts) == {"ada": "GPT-SoVITS", "max": "Kokoro", "kit": "GPT-SoVITS", "lin": "GPT-SoVITS"}
    (tts / "voice-server" / "voice_server.py").unlink()
    assert vl.engines(tts) == {}
    assert [v["engine"] for v in vl.cast(tts, NOW)["voices"]] == ["unknown"] * 5


# --- a build's folder: clips, live voice, ceiling ---------------------------------------

def test_a_voice_json_name_cannot_walk_out_of_the_auditions_root(tts, tmp_path):
    lab = tmp_path / "lab"
    old = NOW - 20 * 86400
    _build(lab, "evil", "../x", STAGES, at=old)
    outside = tts / "x"                                # what auditions/../x would be
    outside.mkdir()
    (outside / "decision.json").write_text('{"tier": 1, "at": 1}')
    # Followed, "../x" holds a decision and the old build drops as history; kept to its folder
    # name, auditions/evil waits on a decision and the build stays.
    (tts / "auditions" / "evil").mkdir(parents=True)
    got = vl.builds(tts, lab, NOW)["builds"]
    assert [x["name"] for x in got] == ["evil"], got


def _aud(b, files=("new-1.mp3", "new-2.mp3", "live-1.mp3"), result=None):
    a = b / "audition"
    a.mkdir()
    for f in files:
        (a / f).write_bytes(b"ID3x")
    if result is not None:
        (a / "result.json").write_text(json.dumps(result))
    return a


def test_clips_are_listed_by_number_and_other_files_ignored(tts, tmp_path):
    lab = tmp_path / "lab"
    b = _build(lab, "kit", "Kit", STAGES[:7])
    _aud(b, files=("new-10.mp3", "new-2.mp3", "new-1.mp3", "live-1.mp3", "new-1.wav", "new-123.mp3",
                   "old-1.mp3", "xnew-1.mp3", "result.mp3"))
    got = vl.builds(tts, lab, NOW)["builds"][0]
    assert got["clips"] == {"new": ["new-1.mp3", "new-2.mp3", "new-10.mp3"], "live": ["live-1.mp3"]}


def test_a_build_with_no_audition_folder_has_empty_clip_lists(tts, tmp_path):
    lab = tmp_path / "lab"
    _build(lab, "kit", "Kit", STAGES[:7])
    assert vl.builds(tts, lab, NOW)["builds"][0]["clips"] == {"new": [], "live": []}


def test_live_now_reads_the_voice_server_once_per_summary(tts, tmp_path, monkeypatch):
    lab = tmp_path / "lab"
    _build(lab, "kit", "Kit", STAGES[:7])
    _build(lab, "lin", "Lin", STAGES[:7], at=NOW - 120)
    (lab / "lin" / "voice.json").write_text(json.dumps({"Name": "Lin", "run": "lin-vl"}))
    (lab / "kit" / "voice.json").write_text(json.dumps({"Name": "Kit", "run": "kit-vl"}))
    (tts / "voice-server" / "voice_server.py").write_text("x = f'{H}/gsv-train/kit-vl/GPT/a.ckpt'\n")
    reads = []
    real = vl._live_text
    monkeypatch.setattr(vl, "_live_text", lambda t: reads.append(t) or real(t))
    got = {b["name"]: b for b in vl.builds(tts, lab, NOW)["builds"]}
    assert got["Kit"]["live_now"] is True and got["Lin"]["live_now"] is False
    assert len(reads) == 1


def test_live_now_is_false_when_unreadable_or_the_run_is_not_a_name(tts, tmp_path):
    lab = tmp_path / "lab"
    b = _build(lab, "kit", "Kit", STAGES[:7])
    (tts / "voice-server" / "voice_server.py").unlink()
    assert vl.builds(tts, lab, NOW)["builds"][0]["live_now"] is False
    (tts / "voice-server" / "voice_server.py").write_text("'/gsv-train/kit-vl/' '/gsv-train/12/'")
    (b / "voice.json").write_text(json.dumps({"Name": "Kit", "run": 12}))
    assert vl.builds(tts, lab, NOW)["builds"][0]["live_now"] is False
    (b / "voice.json").write_text(json.dumps({"Name": "Kit"}))
    assert vl.builds(tts, lab, NOW)["builds"][0]["live_now"] is False
    (b / "voice.json").write_text(json.dumps({"Name": "Kit", "run": "../kit-vl"}))
    assert vl.builds(tts, lab, NOW)["builds"][0]["live_now"] is False


def test_the_ceiling_is_present_only_when_a_number(tts, tmp_path):
    lab = tmp_path / "lab"
    b = _build(lab, "kit", "Kit", STAGES[:10], scores=RANK)
    _aud(b, result={"ceiling": [0.75, 0.9]})
    assert vl.builds(tts, lab, NOW)["builds"][0]["scores"]["ceiling"] == 0.75
    for bad in ({"ceiling": ["x", 1]}, {"ceiling": []}, {"ceiling": 0.7}, {"ceiling": [True, 1]}, {}):
        (b / "audition" / "result.json").write_text(json.dumps(bad))
        assert "ceiling" not in vl.builds(tts, lab, NOW)["builds"][0]["scores"], bad
    (b / "audition" / "result.json").write_text("{nope")
    assert "ceiling" not in vl.builds(tts, lab, NOW)["builds"][0]["scores"]
    (b / "audition" / "result.json").unlink()
    assert "ceiling" not in vl.builds(tts, lab, NOW)["builds"][0]["scores"]


def _zero_shot(lab, d, audition=True, at=NOW - 60, engine="IndexTTS2 zero-shot"):
    """Every stage marked 'skipped', the audition stamped."""
    b = lab / d
    (b / "audition").mkdir(parents=True)
    (b / "voice.json").write_text(json.dumps({"name": d, "engine": engine, "note": "no training"}))
    for s in STAGES:
        if s == "audition" and not audition:
            continue
        f = b / f".done-{s}"
        f.write_text("skipped: IndexTTS2 zero-shot, nothing to train\n" if s != "audition"
                     else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at)) + "\n")
        os.utime(f, (at, at))
    for n in ("new-1.mp3", "new-2.mp3", "live-1.mp3"):
        (b / "audition" / n).write_bytes(b"ID3")
    return b


def test_a_zero_shot_build_is_one_step_and_names_its_engine(tts, tmp_path):
    """An IndexTTS2 copy has nothing trained, so no stage chips claim a train or sweep, and the
    engine is named apart from the trained build of the same voice."""
    lab = tmp_path / "lab"
    _zero_shot(lab, "ada-index")
    _build(lab, "ada", "Ada", STAGES[:7], at=NOW - 120)
    by = {b["dir"]: b for b in vl.builds(tts, lab, NOW)["builds"]}
    z = by["ada-index"]
    assert z["zero_shot"] is True and z["engine"] == "IndexTTS2"
    assert z["stages"] == [] and z["next"] is None and z["of"] == 1 and z["scores"] is None
    assert z["clips"] == {"new": ["new-1.mp3", "new-2.mp3"], "live": ["live-1.mp3"]}
    assert "engine" not in by["ada"] and by["ada"]["next"] == "train", "a trained build is untouched"


def test_a_zero_shot_build_waits_only_on_its_audition(tts, tmp_path):
    lab = tmp_path / "lab"
    _zero_shot(lab, "dot-index", audition=False)
    z = vl.builds(tts, lab, NOW)["builds"][0]
    assert (z["next"], z["step"], z["of"]) == ("audition", 1, 1)


def test_only_a_zero_shot_engine_is_one_step():
    assert vl.zero_shot({"engine": "IndexTTS2 zero-shot"}) == "IndexTTS2"
    assert vl.zero_shot({"engine": "Zero-shot"}) == "zero-shot"
    assert vl.zero_shot({"engine": "GPT-SoVITS"}) is None
    assert vl.zero_shot({"engine": ["zero-shot"]}) is None and vl.zero_shot({}) is None


def test_a_voice_a_host_renders_its_own_way_names_both_engines(tts, tmp_path, monkeypatch):
    # 'kit+' in remote-hosts.conf is desk's own (IndexTTS2) kit; gpu1's copy backs it.
    (tts / "remote-hosts.conf").write_text("gpu1=http://g\ndesk=http://d max,kit+,lin+\n")
    (tts / "voice-server" / "index_voices.py").write_text("VOICES = {v: v for v in ('kit', 'ada')}\n")
    by = {v["name"]: v for v in vl.cast(tts, NOW)["voices"]}
    assert by["Kit"]["engine"] == "IndexTTS2 on desk, GPT-SoVITS on gpu1", "no name setting: the host's own name"
    _settings(tmp_path, monkeypatch, '[voice_host_names.desk]\nplace = "this PC"\n')
    by = {v["name"]: v for v in vl.cast(tts, NOW)["voices"]}
    assert by["Kit"]["engine"] == "IndexTTS2 on this PC, GPT-SoVITS on gpu1"
    assert by["Lin"]["engine"] == "own version on this PC, GPT-SoVITS on gpu1", "not in index_voices.py"
    assert (by["Ada"]["engine"], by["Max"]["engine"]) == ("GPT-SoVITS", "Kokoro"), "no + is unchanged"
    (tts / "voice-server" / "index_voices.py").unlink()
    by = {v["name"]: v for v in vl.cast(tts, NOW)["voices"]}
    assert by["Kit"]["engine"] == "own version on this PC, GPT-SoVITS on gpu1", "no index list, no IndexTTS2"


# --- the cast's controls: a tier button and a test line per voice ------------------------

def test_set_tier_replaces_its_line_and_keeps_the_others(tts):
    tiers = tts / "tiers.conf"
    tiers.write_text("lin=1\nAda=3\nbogus=7\n")
    auditions.set_tier(tts, "Ada", 1)
    assert tiers.read_text().splitlines() == ["lin=1", "ada=1", "bogus=7"]
    auditions.set_tier(tts, "Kit", 2)
    assert tiers.read_text().splitlines() == ["lin=1", "ada=1", "bogus=7", "kit=2"], "appended when new"
    assert sorted(p.name for p in tts.iterdir() if p.name.startswith("tiers")) == ["tiers.conf"]
    tiers.unlink()
    auditions.set_tier(tts, "Lin", 3)
    assert tiers.read_text() == "lin=3\n", "a missing file is made"


def test_decide_still_writes_tiers_conf_through_set_tier(tts, monkeypatch):
    seen = []
    real = auditions.set_tier
    monkeypatch.setattr(auditions, "set_tier", lambda *a: seen.append(a) or real(*a))
    (tts / "auditions" / "Lin").mkdir(parents=True)
    auditions.decide(tts, "Lin", 2)
    assert seen == [(tts, "Lin", 2)]
    assert (tts / "tiers.conf").read_text().splitlines() == ["lin=2", "bogus=7"]
    auditions.decide(tts, "Lin", "no")
    assert len(seen) == 1, "no tier, no tiers.conf write"


def _fake_run(tts, log_line=None, wav=b"RIFFxxxxWAVE", rc=0, seen=None):
    def run(argv, **kw):
        if seen is not None:
            seen.append((argv, kw))
        if wav is not None:
            Path(kw["env"]["TTS_RENDER_TO"]).write_bytes(wav)
        if log_line:
            with (tts / "speak.log").open("a") as f:
                f.write(log_line + "\n")
        return subprocess.CompletedProcess(argv, rc, "", "")
    return run


def _speak_log(tts, text="2026-10-21 13:00:00 SPOKE Old 1s :: " + "x" * 5000 + "\n"):
    (tts / "speak.log").write_text(text)


def test_a_host_woken_for_the_line_is_still_named(tts):
    # a host woken from its idle sleep writes "woke" first.
    _speak_log(tts)
    run = _fake_run(tts, "2026-10-21 14:32:59 REMOTE(kit@desk) woke 32.9s Kit")
    assert vl.test_line(tts, "Kit", "x", run=run)[1] == "desk (woken first)"


def test_a_test_line_renders_with_the_env_and_names_the_host(tts):
    _speak_log(tts)
    seen = []
    run = _fake_run(tts, "2026-10-21 13:41:06 REMOTE(lin+@gpu1) 2.7s Lin", seen=seen)
    wav, by = vl.test_line(tts, "Lin", "Hello there.", run=run)
    assert (wav, by) == (b"RIFFxxxxWAVE", "gpu1")
    argv, kw = seen[0]
    assert argv == [str(tts / "speak.sh"), "Hello there."] and kw["timeout"] == 120
    env = kw["env"]
    assert env["TTS_SPEAKER"] == "Lin" and env["TTS_FLUSHING"] == "1" and env["TTS_NO_OPENER"] == "1"
    assert env["TTS_RENDER_TO"].endswith("/clip.wav")
    assert not os.path.exists(os.path.dirname(env["TTS_RENDER_TO"])), "the temp dir is gone"


def test_a_real_speak_script_is_run_with_the_line_and_the_render_target(tts):
    """The default runner: a real process, the line as its one argument, the target in its environment."""
    if os.statvfs(tts).f_flag & os.ST_NOEXEC:
        pytest.skip("the temp folder cannot run scripts")
    script = tts / "speak.sh"
    script.write_text("#!/bin/sh\n"
                      'printf RIFFxxxxWAVE > "$TTS_RENDER_TO"\n'
                      'printf "2026-10-21 13:41:06 REMOTE(kit@gpu1) 2.7s %s\\n" "$TTS_SPEAKER" '
                      '>> "${0%/*}/speak.log"\n'
                      'printf "%s|%s|%s" "$1" "$TTS_FLUSHING" "$TTS_NO_OPENER" > "${0%/*}/seen.txt"\n')
    script.chmod(0o755)
    _speak_log(tts)
    assert vl.test_line(tts, "Kit", "Hello there.") == (b"RIFFxxxxWAVE", "gpu1")
    assert (tts / "seen.txt").read_text() == "Hello there.|1|1"


def test_a_test_line_reads_only_what_the_render_appended(tts):
    _speak_log(tts, "2026-10-21 12:00:00 REMOTE(lin@desk) 1s Lin\n")
    wav, by = vl.test_line(tts, "Lin", "x", run=_fake_run(tts, "2026-10-21 13:41:06 REMOTE(lin@gpu1) 2s Lin"))
    assert by == "gpu1", "the older line for the same voice is not this clip's"
    wav, by = vl.test_line(tts, "Lin", "x", run=_fake_run(tts, "2026-10-21 13:41:08 SPOKE Lin 3s"))
    assert by == "unknown"


def test_a_test_line_picks_its_own_speakers_line(tts):
    _speak_log(tts)
    run = _fake_run(tts, "2026-10-21 13:41:06 REMOTE(ada@desk) 1.1s Ada\n"
                         "2026-10-21 13:41:07 REMOTE(lin@gpu1) 2.7s Lin")
    assert vl.test_line(tts, "Ada", "x", run=run)[1] == "desk"


def test_a_fallback_says_piper_and_why(tts):
    _speak_log(tts)
    line = "2026-10-21 13:41:06 REMOTE(lin) FALLBACK router=failed Lin :: gpu1 busy :: text=secret words"
    wav, by = vl.test_line(tts, "Lin", "secret words", run=_fake_run(tts, line))
    assert wav and by == "Piper (fallback: router=failed)"
    assert "secret" not in by
    _speak_log(tts)
    by = vl.test_line(tts, "Lin", "x", run=_fake_run(tts, "2026-10-21 13:41:09 REMOTE(lin) FALLBACK  Lin"))[1]
    assert by == "Piper (fallback: no reason given)"


def test_no_speak_log_is_unknown_not_an_error(tts):
    assert not (tts / "speak.log").exists()
    assert vl.test_line(tts, "Lin", "x", run=_fake_run(tts))[1] == "unknown"


def test_a_speak_log_that_cannot_be_read_is_unknown_not_an_error(tts):
    (tts / "speak.log").mkdir()
    assert vl.test_line(tts, "Lin", "x", run=_fake_run(tts))[1] == "unknown"


def test_a_render_that_fails_or_writes_nothing_is_an_error_and_cleans_up(tts):
    _speak_log(tts)
    seen = []
    with pytest.raises(vl.TestLineError, match="no sound"):
        vl.test_line(tts, "Lin", "x", run=_fake_run(tts, wav=None, seen=seen))
    with pytest.raises(vl.TestLineError, match="exited 3"):
        vl.test_line(tts, "Lin", "x", run=_fake_run(tts, rc=3, seen=seen))
    with pytest.raises(vl.TestLineError, match="no sound"):
        vl.test_line(tts, "Lin", "x", run=_fake_run(tts, wav=b"", seen=seen))
    for _, kw in seen:
        assert not os.path.exists(os.path.dirname(kw["env"]["TTS_RENDER_TO"]))


def test_a_render_that_times_out_is_an_error(tts):
    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 120)
    with pytest.raises(vl.TestLineError, match="too long"):
        vl.test_line(tts, "Lin", "x", run=run)


def test_a_speak_script_that_cannot_run_is_an_error_in_words(tts):
    def run(argv, **kw):
        raise FileNotFoundError(2, "No such file or directory")
    with pytest.raises(vl.TestLineError, match="could not run speak.sh: No such file or directory"):
        vl.test_line(tts, "Lin", "x", run=run)


def test_the_test_text_is_stripped_defaulted_and_capped():
    assert vl.test_text("  hi  ") == "hi"
    assert vl.test_text("") == vl.TEST_STOCK == vl.test_text("   ") == vl.test_text(None)
    assert vl.test_text("x" * 200) == "x" * 200
    with pytest.raises(ValueError):
        vl.test_text("x" * 201)
    with pytest.raises(ValueError):
        vl.test_text(5)


def test_the_cast_name_matches_any_case_and_only_the_cast(tts):
    assert vl.cast_name(tts, "lin") == "Lin"
    assert vl.cast_name(tts, "MAX") == "Max"
    assert vl.cast_name(tts, "Nobody") is None and vl.cast_name(tts, "../x") is None
    (tts / "remote-voices.conf").unlink()
    assert vl.cast_name(tts, "Lin") is None


# --- builds belong to voices ------------------------------------------------------------

CASTV = [{"name": "Ada", "voice": "ada"}, {"name": "Max", "voice": "ada"},
         {"name": "Kit", "voice": "kit47"}, {"name": "Lin", "voice": "lin"},
         {"name": "Dot", "voice": "dot"}]


def test_a_build_folder_belongs_to_its_voice():
    got = {d: vl.voice_of(d, CASTV) for d in
           ("ada", "ada-index", "lin-gsv", "ada2-index", "kit47-index", "lin-lol-index",
            "dot-index", "Max", "comms-index-test", "elsewhere", "")}
    assert got == {"ada": "ada", "ada-index": "ada", "lin-gsv": "lin", "ada2-index": "ada", "kit47-index": "kit47",
                   "lin-lol-index": "lin", "dot-index": "dot", "Max": "ada",
                   "comms-index-test": None, "elsewhere": None, "": None}


def test_an_alias_folds_into_its_voice():
    rows = [dict(r, tier=3) for r in CASTV[:3]]
    got = vl.fold(rows)
    assert [r["name"] for r in got] == ["Ada", "Kit"]
    assert got[0]["aliases"] == ["Max"] and got[1]["aliases"] == []


def test_the_row_named_after_the_voice_leads_whatever_the_order():
    got = vl.fold([{"name": "Max", "voice": "ada"}, {"name": "Ada", "voice": "ada"}, {"name": "Sol", "voice": "ada"}])
    assert [(r["name"], r["aliases"]) for r in got] == [("Ada", ["Max", "Sol"])]


def test_the_engine_in_words(tts):
    (tts / "remote-hosts.conf").write_text("gpu1=http://g\ndesk=http://d max,ada,kit+\n")
    (tts / "voice-server" / "index_voices.py").write_text(
        "VOICES = {v: v for v in ('kit', 'sam')}\n")
    words = {v["name"]: v["words"] for v in vl.cast(tts, NOW)["voices"]}
    assert words["Kit"] == "IndexTTS2, gpu1 backup"
    assert words["Ada"] == "Trained, both PCs"
    assert words["Lin"] == "Trained, gpu1 only", "the hosts file's first host is the reference"
    assert words["Max"] == "Kokoro"
    assert words["Dot"] == "Engine not known"
    assert vl.index_voices(tts) == {"kit", "sam"}


def test_the_engine_words_name_whichever_host_comes_first(tts):
    (tts / "remote-hosts.conf").write_text("desk=http://d\ngpu1=http://g kit+\n")
    words = {v["name"]: v["words"] for v in vl.cast(tts, NOW)["voices"]}
    assert (words["Lin"], words["Kit"]) == ("Trained, desk only", "Own version, desk backup")
    assert vl.cast(tts, NOW)["voices"][2]["engine"] == "own version on gpu1, GPT-SoVITS on desk"


def test_an_own_version_with_no_engine_of_the_first_host_stands_alone(tts):
    (tts / "remote-hosts.conf").write_text("gpu1=http://g\ndesk=http://d dot+\n")
    by = {v["name"]: v for v in vl.cast(tts, NOW)["voices"]}
    assert by["Dot"]["words"] == "Own version only"


def test_without_a_hosts_file_the_engine_words_say_one_host(tts):
    words = {v["name"]: v["words"] for v in vl.cast(tts, NOW)["voices"]}
    assert words == {"Ada": "Trained, one host only", "Lin": "Trained, one host only",
                     "Kit": "Trained, one host only", "Max": "Kokoro", "Dot": "Engine not known"}


def test_the_voices_an_index_script_hands_over_are_read_by_regex(tts):
    assert vl.index_voices(tts) == set()
    path = tts / "voice-server" / "index_voices.py"
    path.write_text("VOICES = {v: v for v in ('kit', 'ada-2')}\nOTHER = ('x',)\n")
    assert vl.index_voices(tts) == {"kit", "ada-2"}
    path.write_text("VOICES = {}\n")
    assert vl.index_voices(tts) == set()
    assert vl.index_engine(tts, "kit") == "own version"
    path.write_text("VOICES = {v: v for v in ('kit',)}\n")
    assert (vl.index_engine(tts, "kit"), vl.index_engine(tts, "ki")) == ("IndexTTS2", "own version")


# --- decisions, phases, verdicts ----------------------------------------------------------

def test_a_decision_is_read_and_written(tts, tmp_path):
    lab = tmp_path / "lab"
    _build(lab, "kit", "Kit", STAGES, scores=RANK)
    assert vl.builds(tts, lab, NOW)["builds"][0]["decision"] is None
    vl.decide(lab, "kit", False, now=NOW)
    assert json.loads((lab / "kit" / "decision.json").read_text()) == {"want": False, "at": NOW}
    assert vl.builds(tts, lab, NOW)["builds"][0]["decision"] == {"want": False, "at": NOW}
    (lab / "kit" / "decision.json").write_text("[1]")
    assert vl.builds(tts, lab, NOW)["builds"][0]["decision"] is None, "a bad file is no decision"


def test_a_decision_is_a_true_or_false_and_a_time(tmp_path):
    d = tmp_path / "kit"
    d.mkdir()
    for text, want in (('{"want": true, "at": 5}', {"want": True, "at": 5.0}),
                       ('{"want": false}', {"want": False, "at": None}),
                       ('{"want": true, "at": true}', {"want": True, "at": None}),
                       ('{"want": true, "at": "5"}', {"want": True, "at": None}),
                       ('{"want": 1}', None), ('{"want": "yes"}', None), ('{"at": 5}', None), ("{nope", None)):
        (d / "decision.json").write_text(text)
        assert vl.decision_of(d) == want, text
    (d / "decision.json").unlink()
    assert vl.decision_of(d) is None


def test_finished_running_and_stopped(tts, tmp_path):
    lab = tmp_path / "lab"
    a = _build(lab, "a", "A", STAGES, at=NOW - 60)
    (a / "audition").mkdir()
    (a / "audition" / "new-1.mp3").write_bytes(b"x")  # finished means something to hear
    _build(lab, "b", "B", STAGES[:7], at=NOW - 60)
    _build(lab, "c", "C", STAGES[:7], at=NOW - vl.running_s() - 1)
    _build(lab, "f", "F", STAGES, at=NOW - 60)         # through its last stage with nothing to hear
    _zsb(lab, "d-index")
    _zsb(lab, "e-index", audition=False)
    got = {b["dir"]: vl.phase(b, NOW) for b in vl.builds(tts, lab, NOW)["builds"]}
    assert got == {"a": "finished", "b": "running", "c": "stopped", "f": "stopped", "d-index": "finished",
                   "e-index": "stopped"}


def test_the_running_window_is_fifteen_minutes_unless_the_setting_says_otherwise(tts, tmp_path, monkeypatch):
    assert vl.running_s() == 15 * 60 == vl.RUNNING_MIN * 60
    lab = tmp_path / "lab"
    _build(lab, "ada", "Ada", STAGES[:7], at=NOW - 14 * 60)
    _build(lab, "lin", "Lin", STAGES[:7], at=NOW - 16 * 60)
    _build(lab, "kit", "Kit", STAGES[:7], at=NOW - 2 * 3600)

    def phases():
        return {b["dir"]: vl.phase(b, NOW) for b in vl.builds(tts, lab, NOW)["builds"]}
    assert phases() == {"ada": "running", "lin": "stopped", "kit": "stopped"}
    _settings(tmp_path, monkeypatch, "voicelab_running_minutes = 360\n")
    assert vl.running_s() == 360 * 60
    assert phases() == {"ada": "running", "lin": "running", "kit": "running"}
    _settings(tmp_path, monkeypatch, "voicelab_running_minutes = 1\n")
    assert phases() == {"ada": "stopped", "lin": "stopped", "kit": "stopped"}


def test_the_running_minutes_are_a_positive_number_or_the_default(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "voicelab_running_minutes = 0.5\n")
    assert vl.running_s() == 30.0, "a fraction of a minute is a number"
    _settings(tmp_path, monkeypatch, 'voicelab_running_minutes = "30"\n')
    assert vl.running_s() == 30 * 60, "a number in words is a number, as the environment gives it"
    for bad in ("0", "-5", "true", '"soon"', '"nan"', '"inf"', "[30]", "{}"):
        _settings(tmp_path, monkeypatch, f"voicelab_running_minutes = {bad}\n")
        assert vl.running_s() == 15 * 60, bad


def test_the_environment_sets_the_running_minutes_and_the_baseline_tags(monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_VOICELAB_RUNNING_MINUTES", "360")
    monkeypatch.setenv("AGENT_COMPANION_VOICELAB_BASELINE_TAGS", f"stock{os.pathsep}old")
    assert (vl.running_s(), vl.baseline_tags()) == (360 * 60, ("stock", "old"))
    monkeypatch.setenv("AGENT_COMPANION_VOICELAB_RUNNING_MINUTES", "soon")
    monkeypatch.setenv("AGENT_COMPANION_VOICELAB_BASELINE_TAGS", "")
    assert (vl.running_s(), vl.baseline_tags()) == (15 * 60, ("live",))


def test_the_verdict_in_words(tts, tmp_path):
    lab = tmp_path / "lab"
    _build(lab, "kit", "Kit", STAGES, scores=RANK)
    b = vl.builds(tts, lab, NOW)["builds"][0]
    assert vl.verdict(b, "Kit") == "New sounds more like Kit: 78 against live 63 · words misheard 7%"
    wash = json.loads(json.dumps(RANK))
    wash["rank"][2]["ecapa"] = 0.76
    (lab / "kit" / "scores.json").write_text(json.dumps(wash))
    b = vl.builds(tts, lab, NOW)["builds"][0]
    assert vl.verdict(b, "Kit").startswith("A wash: new 78, live 76")
    _zsb(lab, "ada-index")
    zs = next(x for x in vl.builds(tts, lab, NOW)["builds"] if x["dir"] == "ada-index")
    assert vl.verdict(zs, "Ada") == "IndexTTS2 copy of the comms clip, not scored"


def test_the_verdict_names_the_closer_take_and_what_it_cannot_say():
    base = {"scores": {"best": {"tag": "n", "ecapa": 0.60, "wer": 0.2}, "baseline": {"tag": "live", "ecapa": 0.70}}}
    assert vl.verdict(base, "Ada") == "Live sounds more like Ada: 70 against new 60 · words misheard 20%"
    assert vl.verdict(dict(base, live_now=True), "Ada") == "Sounds like Ada 60 · words misheard 20%"
    alone = {"scores": {"best": {"tag": "n", "ecapa": 0.6}, "baseline": None}}
    assert vl.verdict(alone, "Ada") == "Sounds like Ada 60"
    assert vl.verdict({"scores": None}, "Ada") == "Not scored yet"
    unread = {"scores": {"best": None, "baseline": None, "why": "scores.json could not be read"}}
    assert vl.verdict(unread, "Ada") == "scores.json could not be read"
    assert vl.verdict({"zero_shot": True}, "Ada") == "Zero-shot copy of the comms clip, not scored"


def test_the_verdict_calls_five_points_a_difference_and_less_a_wash():
    def gap(new, live):
        return {"scores": {"best": {"tag": "n", "ecapa": new / 100},
                           "baseline": {"tag": "live", "ecapa": live / 100}}}
    assert vl.verdict(gap(65, 60), "Ada") == "New sounds more like Ada: 65 against live 60"
    assert vl.verdict(gap(60, 65), "Ada") == "Live sounds more like Ada: 65 against new 60"
    assert vl.verdict(gap(64, 60), "Ada") == "A wash: new 64, live 60"
    assert vl.verdict(gap(60, 64), "Ada") == "A wash: new 60, live 64"


# --- the summary ------------------------------------------------------------------------

def _summary_lab(tts, tmp_path):
    lab = tmp_path / "lab"
    (tts / "voice-server" / "index_voices.py").write_text("VOICES = {v: v for v in ('ada',)}\n")
    (tts / "remote-hosts.conf").write_text("gpu1=http://g\ndesk=http://d ada+\n")
    for d, days in (("kit", 3), ("kit-old", 9)):                               # finished; the older never waits
        b = _build(lab, d, "Kit", STAGES, scores=RANK, at=NOW - days * 86400)
        (b / "audition").mkdir()
        (b / "audition" / "new-1.mp3").write_bytes(b"x")
    _build(lab, "lin", "Lin", STAGES[:7], at=NOW - 60)                         # running
    _zsb(lab, "ada-index")                                                     # live: ada is IndexTTS2
    _zsb(lab, "comms-index-test")                                              # nobody's
    _build(lab, "elsewhere", "Elsewhere", STAGES[:7], at=NOW - 3 * 86400)      # stopped, nobody's
    return lab


def test_summary_needs_running_voices_other(tts, tmp_path):
    lab = _summary_lab(tts, tmp_path)
    got = vl.summary(tts, lab, None, NOW)
    assert set(got) == {"now", "needs", "running", "voices", "other"}
    assert got["now"] == {"kind": "unknown", "text": "the voice host's state is not known here"}
    assert [(i["kind"], i["dir"]) for i in got["needs"]["items"]] == [("build", "kit")]
    assert got["needs"]["items"][0]["verdict"].startswith("New sounds more like Kit")
    assert got["needs"]["items"][0]["engine"] == "Trained" and got["needs"]["items"][0]["voice"] == "kit"
    assert [b["dir"] for b in got["running"]["builds"]] == ["lin"]
    assert got["running"]["builds"][0]["voice_name"] == "Lin"
    assert {b["dir"] for b in got["other"]["builds"]} == {"comms-index-test", "elsewhere"}
    v = {x["name"]: x for x in got["voices"]["voices"]}
    assert [x["name"] for x in got["voices"]["voices"]][0] == "Kit", "waiting first"
    assert v["Kit"]["waiting"] and not v["Ada"]["waiting"]
    assert [b["dir"] for b in v["Kit"]["builds"]] == ["kit", "kit-old"], "latest first"
    assert v["Ada"]["builds"][0]["live_now"] is True
    assert len(v["Kit"]["changes"]) == 3
    assert v["Kit"]["notes"] == [], "no add-on, no notes"
    assert v["Lin"]["builds"][0]["phase"] == "running"


def test_the_voices_come_waiting_first_then_by_tier_then_by_name(tts, tmp_path):
    lab = _summary_lab(tts, tmp_path)
    order = [v["name"] for v in vl.summary(tts, lab, None, NOW)["voices"]["voices"]]
    # Kit waits, so it leads though it is tier 3; then tier 1 (Ada from identity.sh, Lin from tiers.conf);
    # then the tier 2 default (Dot, Max), each tier by name.
    assert order == ["Kit", "Ada", "Lin", "Dot", "Max"]


def test_a_decided_build_stops_waiting(tts, tmp_path):
    lab = _summary_lab(tts, tmp_path)
    vl.decide(lab, "kit", True, now=NOW)
    got = vl.summary(tts, lab, None, NOW)
    assert got["needs"]["items"] == []
    assert not {x["name"]: x for x in got["voices"]["voices"]}["Kit"]["waiting"]


def test_a_live_build_does_not_wait(tts, tmp_path):
    lab = tmp_path / "lab"
    (tts / "voice-server" / "voice_server.py").write_text("x = f'{H}/gsv-train/kit-vl/GPT/a.ckpt'\n")
    b = _build(lab, "kit", "Kit", STAGES, scores=RANK)
    (b / "audition").mkdir()
    (b / "audition" / "new-1.mp3").write_bytes(b"x")
    got = vl.summary(tts, lab, None, NOW)
    assert got["needs"]["items"] == []
    kit = {x["name"]: x for x in got["voices"]["voices"]}["Kit"]
    assert not kit["waiting"] and kit["builds"][0]["live_now"] is True
    assert kit["builds"][0]["verdict"] == "Sounds like Kit 78 · words misheard 7%", "no new-against-live for it"


def test_an_audition_without_a_pick_waits(tts, tmp_path):
    lab = tmp_path / "lab"
    lab.mkdir()
    d = tts / "auditions" / "Trial"
    d.mkdir(parents=True)
    (d / "one.mp3").write_bytes(b"x")
    items = vl.summary(tts, lab, None, NOW)["needs"]["items"]
    assert [(i["kind"], i["name"]) for i in items] == [("audition", "Trial")]


def test_without_the_stage_list_the_cast_still_shows(tts, tmp_path):
    (tts / "voicelab" / "voicelab.py").write_text("nothing here\n")
    got = vl.summary(tts, tmp_path / "lab", None, NOW)
    assert got["running"]["ok"] is False and got["voices"]["ok"] is True
    assert got["running"]["why"] == "could not read the stage list in tts/voicelab/voicelab.py"
    assert got["other"] == {"ok": False, "builds": []}
    assert all(v["builds"] == [] for v in got["voices"]["voices"])


def test_without_a_cast_the_builds_still_show(tts, tmp_path):
    lab = _summary_lab(tts, tmp_path)
    (tts / "remote-voices.conf").unlink()
    got = vl.summary(tts, lab, None, NOW)
    assert got["voices"] == {"ok": False, "voices": [], "why": "could not read tts/remote-voices.conf"}
    assert {b["dir"] for b in got["other"]["builds"]} == {"kit", "kit-old", "lin", "ada-index", "comms-index-test",
                                                            "elsewhere"}, "with no cast every build is nobody's"


def test_the_summary_says_where_the_cast_history_could_not_be_read(tts, tmp_path):
    shutil.rmtree(tts / ".git")
    got = vl.summary(tts, tmp_path / "lab", None, NOW)
    assert got["voices"]["ok"] and got["voices"]["note"] == "the tts repo's history could not be read"


# --- the add-on: right now, notes, decided -------------------------------------------------

def test_the_summary_asks_the_add_on_for_the_right_now_line(tts, tmp_path, monkeypatch):
    lab = _summary_lab(tts, tmp_path)
    seen = []

    def line(state, builds, now):
        seen.append((state, [b["dir"] for b in builds], now))
        return {"kind": "training", "text": "training Lin, stage 8 of 11", "build": "lin"}
    _plug(monkeypatch, voicelab_now=line)
    state = {"real": False, "why": "gpu1 is busy"}
    got = vl.summary(tts, lab, state, NOW)
    assert got["now"] == {"kind": "training", "text": "training Lin, stage 8 of 11", "build": "lin"}
    assert seen[0][0] == state and seen[0][2] == NOW
    assert seen[0][1] == ["ada-index", "comms-index-test", "lin", "elsewhere", "kit", "kit-old"], "newest first"


def test_a_voices_notes_come_from_the_add_on_and_default_to_none(tts, tmp_path, monkeypatch):
    (tts / "remote-voices.conf").write_text("Ada=ada\nMax=ada\nKit=kit\n")
    lab = tmp_path / "lab"
    lab.mkdir()
    assert all(v["notes"] == [] for v in vl.summary(tts, lab, None, NOW)["voices"]["voices"])
    asked = []

    def notes(names):
        asked.append(list(names))
        return [{"text": "re-level the takes", "at": 1.5}] if "Ada" in names else []
    _plug(monkeypatch, voicelab_notes=notes)
    v = {x["name"]: x for x in vl.summary(tts, lab, None, NOW)["voices"]["voices"]}
    assert v["Ada"]["notes"] == [{"text": "re-level the takes", "at": 1.5}]
    assert v["Kit"]["notes"] == []
    assert sorted(asked) == [["Ada", "Max"], ["Kit"]], "a voice's name, then its aliases"


def test_the_notes_hook_is_cleaned_before_the_page_sees_it(monkeypatch):
    assert addons.voicelab_notes(["Ada"]) == []
    _plug(monkeypatch, voicelab_notes=lambda names: [
        {"text": "x" * 300, "at": 5}, {"text": "a bool is no time", "at": True},
        {"text": "nor is text", "at": "5"}, {"text": "no time given"},
        {"text": 7, "at": 1.0}, "junk", None, {"at": 1}])
    assert addons.voicelab_notes(["Ada"]) == [
        {"text": "x" * 200, "at": 5.0}, {"text": "a bool is no time", "at": None},
        {"text": "nor is text", "at": None}, {"text": "no time given", "at": None}]
    _plug(monkeypatch, voicelab_notes=lambda names: "not a list")
    assert addons.voicelab_notes(["Ada"]) == []

    def boom(names):
        raise RuntimeError("broken")
    _plug(monkeypatch, voicelab_notes=boom)
    assert addons.voicelab_notes(["Ada"]) == []


def test_a_raising_notes_add_on_costs_the_notes_only(tts, tmp_path, monkeypatch):
    def boom(names):
        raise RuntimeError("broken")
    _plug(monkeypatch, voicelab_notes=boom)
    lab = tmp_path / "lab"
    lab.mkdir()
    got = vl.summary(tts, lab, None, NOW)
    assert got["voices"]["ok"] and all(v["notes"] == [] for v in got["voices"]["voices"])


def test_the_decided_hook_answers_in_words_whatever_the_add_on_says(monkeypatch, tmp_path):
    assert addons.voicelab_decided("Ada", "ada", tmp_path / "ada", True) == {"filed": False, "detail": ""}
    for said, want in (({"filed": True, "detail": "filed #3"}, {"filed": True, "detail": "filed #3"}),
                       ({"filed": 1, "detail": None}, {"filed": True, "detail": ""}),
                       ({"filed": False, "detail": 7}, {"filed": False, "detail": "7"}),
                       ({}, {"filed": False, "detail": ""}), ("filed", {"filed": False, "detail": ""}),
                       (None, {"filed": False, "detail": ""})):
        _plug(monkeypatch, voicelab_decided=lambda *a, said=said: said)
        assert addons.voicelab_decided("Ada", "ada", tmp_path / "ada", True) == want, said

    def boom(*a):
        raise RuntimeError("broken")
    _plug(monkeypatch, voicelab_decided=boom)
    assert addons.voicelab_decided("Ada", "ada", tmp_path / "ada", True) == {"filed": False, "detail": "not filed"}


# --- decide(): a name that is not a build -------------------------------------------------

def test_decide_writes_a_numeric_at(tmp_path):
    (tmp_path / "kit").mkdir()
    vl.decide(tmp_path, "kit", True)
    at = json.loads((tmp_path / "kit" / "decision.json").read_text())["at"]
    assert isinstance(at, (int, float)) and not isinstance(at, bool)


def test_decide_refuses_a_name_that_is_not_a_build(tmp_path):
    with pytest.raises(ValueError):
        vl.decide(tmp_path, "..", True)


def test_decide_refuses_a_name_that_is_not_a_directory_inside_the_lab(tmp_path):
    lab, outside = tmp_path / "lab", tmp_path / "outside"
    lab.mkdir()
    outside.mkdir()
    (lab / "a-file").write_text("not a folder")
    (lab / "linked").symlink_to(outside, target_is_directory=True)
    for name in (".", "..", "../outside", "a/b", "nobody", "a-file", "linked", "x" * 65, "bad name", ""):
        with pytest.raises(ValueError, match="not a build"):
            vl.decide(lab, name, True)
    assert sorted(p.name for p in lab.iterdir()) == ["a-file", "linked"], "nothing was made"
    assert list(outside.iterdir()) == [] and list(tmp_path.glob("decision.json")) == []


# --- settings: where the coordinator and the lab are -----------------------------------------

def test_the_coordinator_and_lab_folders_are_settings(tts, tmp_path, monkeypatch):
    assert vl.tts_dir() == Path.home() / ".local" / "share" / "agent-companion" / "tts"
    assert vl.lab_root() == vl.tts_dir() / "voicelab" / "builds"
    assert not vl.configured(), "a folder that is not there is no coordinator"
    monkeypatch.setenv("AGENT_COMPANION_TTS_DIR", str(tts))
    assert vl.tts_dir() == tts and vl.lab_root() == tts / "voicelab" / "builds" and vl.configured()
    monkeypatch.setenv("AGENT_COMPANION_VOICELAB_DIR", str(tmp_path / "lab"))
    assert vl.lab_root() == tmp_path / "lab"
    monkeypatch.setenv("AGENT_COMPANION_VOICELAB_DIR", "~/lab")
    assert vl.lab_root() == Path.home() / "lab", "a ~ is the home folder"
    (tmp_path / "config" / "audio-effects.cfg").unlink()
    assert not vl.configured(), "a folder without the control files is not set up"


def test_the_folders_can_be_set_in_the_settings_file(tts, tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, f'tts_dir = "{tts}"\nvoicelab_dir = "{tmp_path / "builds"}"\n')
    assert vl.tts_dir() == tts and vl.configured()
    assert vl.lab_root() == tmp_path / "builds"


# --- the pipeline, for an install with no coordinator -----------------------------------------

def test_the_pipeline_lists_what_the_scripts_left_and_says_where_it_looked(tmp_path, monkeypatch):
    voices_dir = tmp_path / "voices"
    monkeypatch.setenv("AGENT_COMPANION_VOICES_DIR", str(voices_dir))
    assert vl.voices_dir() == voices_dir
    got = vl.pipeline_summary()
    assert got["now"] == {"kind": "pipeline", "text": f"No voices yet: voicelab/README.md says how to make one "
                                                      f"(they land in {voices_dir})."}
    assert got["pipeline"] == {"root": str(voices_dir), "voices": []}
    (voices_dir / "one" / "audition").mkdir(parents=True)
    (voices_dir / "one" / "audition" / "a.WAV").write_bytes(b"RIFF")
    (voices_dir / "one" / "audition" / "b.txt").write_bytes(b"x")
    (voices_dir / "two").mkdir()
    (voices_dir / "two" / "model.ckpt").write_bytes(b"12345")
    (voices_dir / "three").mkdir()
    (voices_dir / "three" / "voice.json").write_text("{}")
    (voices_dir / "three" / "link.bin").symlink_to(tmp_path)
    (voices_dir / "bad name").mkdir()
    (voices_dir / "linked").symlink_to(tmp_path, target_is_directory=True)
    (voices_dir / "a-file").write_text("x")
    got = vl.pipeline_summary()
    rows = {r["name"]: r for r in got["pipeline"]["voices"]}
    assert sorted(rows) == ["one", "three", "two"]
    assert (rows["one"]["stage"], rows["two"]["stage"], rows["three"]["stage"]) == ("prepped", "trained", "served")
    assert rows["one"]["audition"] == ["a.WAV"] and rows["two"]["bytes"] == 5
    assert rows["three"]["bytes"] == 2, "a link is not counted"
    assert got["now"]["text"] == f"3 voices in {voices_dir}"
    (voices_dir / "two").rename(voices_dir / "moved")
    for gone in ("one", "three"):
        shutil.rmtree(voices_dir / gone)
    assert vl.pipeline_summary()["now"]["text"] == f"1 voice in {voices_dir}", "one is not 1 voices"


def test_a_file_that_cannot_be_measured_is_left_out_of_a_voices_size(tmp_path, monkeypatch):
    d = tmp_path / "voice"
    d.mkdir()
    (d / "a.bin").write_bytes(b"123")
    (d / "b.bin").write_bytes(b"45")
    real = Path.is_file

    def is_file(self):
        if self.name == "b.bin":
            raise PermissionError(13, "denied")
        return real(self)
    monkeypatch.setattr(Path, "is_file", is_file)
    assert vl._size(d) == 3


def test_the_pipeline_folder_is_a_setting(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, f'voices_dir = "{tmp_path / "set"}"\n')
    assert vl.voices_dir() == tmp_path / "set"
    monkeypatch.setenv("AGENT_COMPANION_VOICES_DIR", str(tmp_path / "env"))
    assert vl.voices_dir() == tmp_path / "env", "the environment first"
    monkeypatch.delenv("AGENT_COMPANION_VOICES_DIR")
    _settings(tmp_path, monkeypatch, "")
    assert vl.voices_dir() == Path.home() / ".local/share/agent-companion/voices"
