"""Optional controls for a speech coordinator.

The coordinator directory is supplied by the server setting.  This module
parses or rewrites its documented text files, and runs two of its scripts,
stop.sh and quiet.sh, as fixed executables with fixed arguments and no shell.
It never imports, executes or trusts a coordinator configuration as code.
Field 2 of audio-effects.cfg is a sox chain the audio pipeline passes
unquoted, so effects are a fixed set of presets: input names a preset and
never supplies the chain.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from . import addons

NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,31}$")
VOICE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}(?::[0-9]{1,3})?$")
TRACK = re.compile(r"^[A-Za-z0-9_.-]{1,120}\.(?:mp3|ogg|opus|wav)$")
# Chains without the level gain: the coordinator composes "gain <voice_gain>
# <chain>". `distant`'s trailing gain is part of the effect, not the level.
PRESETS = {"plain": "", "warm": "reverb 30", "bright": "treble 4", "deep": "pitch -150", "distant": "reverb 60 gain -2"}
ANNOUNCEMENTS = ("prompt", "subagent", "complete")
MAX_MUTE_MINUTES = 24 * 60


def _tts(tts: Path) -> Path:
    return Path(tts)


def _base(tts: Path) -> Path:
    return _tts(tts).parent


def _config(tts: Path) -> Path:
    return _base(tts) / "config"


def available(tts: Path) -> bool:
    """Whether the coordinator has the control files this overlay needs."""
    return _tts(tts).is_dir() and (_config(tts) / "audio-effects.cfg").is_file()


def _atomic(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(body)
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def _effects_path(tts: Path) -> Path:
    return _config(tts) / "audio-effects.cfg"


def _override_path(tts: Path) -> Path:
    return _tts(tts) / "voices.override.tsv"


def _rows(path: Path) -> dict[str, str]:
    out = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#") or "\t" not in line:
            continue
        key, value = line.split("\t", 1)
        if NAME.fullmatch(key.strip()):
            out[key.strip()] = value.strip()
    return out


def _write_rows(path: Path, values: dict[str, str]) -> None:
    _atomic(path, "".join(f"{name}\t{value}\n" for name, value in sorted(values.items())))


def voices(tts: Path) -> list[dict]:
    """Installed voice models, never a package's list of downloadable ones.
    A model whose sidecar is missing or not a JSON object is skipped: without
    it the speaker count is unknowable."""
    out = []
    try:
        models = sorted((_base(tts) / "piper-voices").glob("*.onnx"))
    except OSError:
        return out
    for model_path in models:
        try:
            meta = json.loads((model_path.parent / f"{model_path.name}.json").read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        try:
            count = int(meta.get("num_speakers") or 1)
        except (TypeError, ValueError):
            count = 1
        labels = {}
        if isinstance(meta.get("speaker_id_map"), dict):
            for label, number in meta["speaker_id_map"].items():
                try:
                    labels[int(number)] = str(label)
                except (TypeError, ValueError):
                    pass
        model = model_path.name[:-5]
        for number in range(max(1, count)):
            multi = count > 1
            out.append({"id": f"{model}:{number}" if multi else model, "model": model,
                        "speaker": number if multi else None,
                        "label": labels.get(number, f"{model} #{number}") if multi else model,
                        "multi": multi})
    return out


def tracks(tts: Path) -> list[str]:
    try:
        return sorted(path.name for path in (_base(tts) / "audio" / "tracks").glob("*.mp3"))
    except OSError:
        return []


_LEADING_GAIN = re.compile(r"^\s*gain\s+-?\d+(?:\.\d+)?\s*")


def strip_leading_gain(chain: str) -> str:
    """The effect without the level: only the first gain token is the level."""
    return _LEADING_GAIN.sub("", chain or "", count=1).strip()


def _preset(effects: str) -> str:
    bare = strip_leading_gain(effects)
    return next((key for key, value in PRESETS.items() if value == bare), "custom")


def speakers(tts: Path) -> list[dict]:
    """audio-effects.cfg's rows. A preset is read from volume.conf's
    effects.<Name> first: that is the store, and a row can lag it until the
    speaker's next clip."""
    overrides, effects = _rows(_override_path(tts)), effects_overrides(tts)
    out = []
    try:
        lines = _effects_path(tts).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 4 or not NAME.fullmatch(parts[0]):
            continue
        try:
            volume = float(parts[3])
        except ValueError:
            volume = 0.7
        source = effects.get(parts[0], parts[1])
        out.append({"name": parts[0], "effects": parts[1], "preset": _preset(source),
                    "track": parts[2], "volume": volume, "voice": overrides.get(parts[0], "")})
    return out


def music_enabled(tts: Path) -> bool:
    try:
        return (_config(tts) / "background-music-enabled.txt").read_text().strip().lower() == "true"
    except OSError:
        return False


def set_music_enabled(tts: Path, enabled: bool) -> bool:
    _atomic(_config(tts) / "background-music-enabled.txt", "true\n" if enabled else "false\n")
    return bool(enabled)


def update_speaker(tts: Path, name: str, preset: str | None = None, track: str | None = None,
                   volume: float | None = None, voice: str | None = None) -> dict:
    if not NAME.fullmatch(name or ""):
        raise ValueError("invalid speaker name")
    rows = speakers(tts)
    if name not in {row["name"] for row in rows}:
        raise ValueError("unknown speaker")
    if preset is not None and preset not in PRESETS:
        raise ValueError("unknown preset")
    if track is not None and track not in tracks(tts):
        raise ValueError("unknown track")
    if volume is not None:
        try:
            volume = float(volume)
        except (TypeError, ValueError):
            raise ValueError("volume must be a number") from None
        if not 0 <= volume <= 1:
            raise ValueError("volume must be between 0 and 1")
    if voice is not None:
        voice = str(voice)
        if voice and (not VOICE.fullmatch(voice) or voice not in {item["id"] for item in voices(tts)}):
            raise ValueError("voice is not installed")
        values = _rows(_override_path(tts))
        if voice:
            values[name] = voice
        else:
            values.pop(name, None)
        _write_rows(_override_path(tts), values)
    if preset is not None:
        # The store, which the coordinator composes the row from on the
        # speaker's next clip; the row written below only catches up early.
        set_effects_override(tts, name, PRESETS[preset])
    updated, lines = {}, []
    for row in rows:
        item = dict(row)
        if item["name"] == name:
            if preset is not None:
                item["effects"] = (f"gain {levels(tts)['voice_gain']} {PRESETS[preset]}").strip()
                item["preset"] = preset
            if track is not None:
                item["track"] = track
            if volume is not None:
                item["volume"] = volume
            if voice is not None:
                item["voice"] = voice
            updated = item
        lines.append(f"{item['name']}|{item['effects']}|{item['track']}|{item['volume']:.2f}")
    _atomic(_effects_path(tts), "\n".join(lines) + "\n")
    return updated


def stop_available(tts: Path) -> bool:
    script = _tts(tts) / "stop.sh"
    return script.is_file() and os.access(script, os.X_OK)


def stop_playback(tts: Path) -> dict:
    """Cut the current clip short and let everyone waiting go.

    One fixed executable with no arguments and no shell.  The queue marker is
    cleared whatever the player does: speak.sh keeps every other session
    waiting until `busy` says the player is free, so a stop that ended the
    audio and left the marker would strand them behind a clip that is gone.
    """
    if not stop_available(tts):
        return {"stopped": False, "queue_cleared": False, "detail": "stop is unavailable: no executable tts/stop.sh"}
    stopped, detail = False, ""
    try:
        result = subprocess.run([str(_tts(tts) / "stop.sh")], shell=False, timeout=12, capture_output=True, text=True)
        stopped = result.returncode == 0
        if not stopped:
            detail = (result.stderr or result.stdout or "").strip()[:200] or f"stop.sh exited {result.returncode}"
    except subprocess.TimeoutExpired:
        detail = "the phone timed out"
    except OSError as exc:
        detail = f"could not run stop.sh: {exc}"
    try:
        _atomic(_tts(tts) / "busy", "0\n")
        cleared = True
    except OSError:
        cleared = False
    return {"stopped": stopped, "queue_cleared": cleared, "detail": detail or ("stopped" if stopped else "")}


def _values(tts: Path) -> dict[str, str]:
    out = {}
    try:
        lines = (_tts(tts) / "announce.conf").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _write_values(tts: Path, values: dict[str, str]) -> None:
    _atomic(_tts(tts) / "announce.conf", "".join(f"{key}={value}\n" for key, value in values.items()))


def subtitles(tts: Path) -> bool:
    value = _values(tts).get("subtitles")
    return value is None or value.lower() in ("on", "true", "yes", "1")


def set_subtitles(tts: Path, enabled: bool) -> bool:
    values = _values(tts)
    values["subtitles"] = "on" if enabled else "off"
    _write_values(tts, values)
    return bool(enabled)


def subtitles_available(tts: Path) -> bool:
    """Whether the coordinator honours the setting (its speak.sh reads the
    key), so a page never shows a switch that nothing reads."""
    try:
        return "subtitles" in (_tts(tts) / "speak.sh").read_text()
    except OSError:
        return False


def announcements(tts: Path) -> dict[str, bool]:
    values = _values(tts)
    return {kind: values.get(kind, "on").lower() in ("on", "true", "yes", "1") for kind in ANNOUNCEMENTS}


def set_announcement(tts: Path, kind: str, enabled: bool) -> dict[str, bool]:
    if kind not in ANNOUNCEMENTS:
        raise ValueError("unknown announcement")
    values, current = _values(tts), announcements(tts)
    current[kind] = bool(enabled)
    for key, value in current.items():
        values[key] = "on" if value else "off"
    _write_values(tts, values)
    return current


def _expiry(path: Path) -> int | None:
    try:
        digits = "".join(char for char in path.read_text() if char.isdigit())
        value = int(digits or "0")
    except (OSError, ValueError):
        return None
    return value if value == 0 or value > time.time() else None


def mute_state(tts: Path) -> dict:
    directory = _tts(tts)
    muted = {}
    for path in directory.glob("mute-*") if directory.is_dir() else []:
        name, until = path.name[5:], _expiry(path)
        if NAME.fullmatch(name) and until is not None:
            muted[name] = until
    return {"all": _expiry(directory / "mute"), "speakers": muted}


def set_mute(tts: Path, scope: str, minutes: int | None) -> dict:
    if scope != "all" and scope not in {row["name"] for row in speakers(tts)}:
        raise ValueError("unknown speaker")
    if minutes is not None and (type(minutes) is not int or minutes < 0 or minutes > MAX_MUTE_MINUTES):
        raise ValueError(f"minutes must be between 0 and {MAX_MUTE_MINUTES}")
    path = _tts(tts) / ("mute" if scope == "all" else f"mute-{scope}")
    if minutes == 0:
        path.unlink(missing_ok=True)
    else:
        _atomic(path, f"{0 if minutes is None else int(time.time()) + minutes * 60}\n")
    return mute_state(tts)


# --- delivery health ------------------------------------------------------

_LOG_TAIL_BYTES = 64 * 1024
_STAMP = "%Y-%m-%d %H:%M:%S"
# Past this, a failure with nothing after it describes the past rather than
# the present: long enough that a quiet stretch is not mistaken for a fault,
# short enough that a real one is not asserted for an hour.
_HEALTH_STALE_AFTER = 900


def _parse_stamp(line: str) -> float | None:
    try:
        return time.mktime(time.strptime(line[:19], _STAMP))
    except (ValueError, OverflowError):
        return None


def _log_tail(tts: Path, want: int) -> str | None:
    """The log's last `want` bytes, from the first whole line, or None without one."""
    path = _tts(tts) / "speak.log"
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > want:
                handle.seek(size - want)
                handle.readline()
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return None


def tts_health(tts: Path) -> dict:
    """Whether clips are reaching the player, read from speak.log.

    A port check would answer the wrong question: a port can accept a
    connection and never deliver.  SPOKE means a clip played.  SCP-FAIL means
    one never arrived (kind "undelivered"), PLAY-FAIL that one arrived and
    would not play ("silent"); the newest of the three is the state.  MUTED
    lines are no delivery attempt, and a HELD line means nothing was tried.
    A failure stops being current once a hold followed it, or after fifteen
    minutes with nothing since: the state is then unknown, never down.  A
    down state carries the add-on's repair steps for its kind as `fix`.
    """
    tail = _log_tail(tts, _LOG_TAIL_BYTES)
    if tail is None:
        return {"state": "unknown", "detail": "no speak.log", "age_seconds": None,
                "consecutive_failures": 0, "at": None, "kind": None, "fix": None}
    last, consecutive, held_since = None, 0, None
    for line in tail.splitlines():
        if " SPOKE " in line:
            last, consecutive, held_since = ("up", line, "", None), 0, None
            continue
        if " HELD" in line:
            if held_since is None:
                held_since = _parse_stamp(line)
            continue
        # PLAY-FAIL carries a trailing ":: <text>" that SCP-FAIL does not, so
        # the segment after the first "::" is the detail for both.
        for marker, kind in ((" SCP-FAIL ", "undelivered"), (" PLAY-FAIL ", "silent")):
            if marker in line:
                parts = line.split("::")
                last = ("down", line, parts[1].strip() if len(parts) > 1 else "", kind)
                consecutive += 1
                break
    if last is None:
        return {"state": "unknown", "detail": "no delivery attempts logged", "age_seconds": None,
                "consecutive_failures": 0, "at": None, "kind": None, "fix": None}
    state, line, detail, kind = last
    at = _parse_stamp(line)
    age = None if at is None else max(0, int(time.time() - at))
    if state == "down":
        why = ""
        if held_since is not None and (at is None or held_since >= at):
            why = "comms went on hold after it, so nothing has been attempted since"
        elif age is not None and age > _HEALTH_STALE_AFTER:
            why = f"nothing has been attempted for {age // 60} minutes"
        if why:
            return {"state": "unknown", "detail": f"last attempt failed, but {why}", "age_seconds": age,
                    "consecutive_failures": consecutive, "at": at, "kind": None, "fix": None}
    return {"state": state, "detail": detail, "age_seconds": age, "consecutive_failures": consecutive,
            "at": at, "kind": kind, "fix": addons.tts_fix(kind) if kind else None}


# --- levels and effects: volume.conf --------------------------------------
# Configuration the coordinator reads on every clip; audio-effects.cfg is the
# output it rebuilds from it, so nothing is kept there.  voice_gain is in dB
# (sox clips somewhere above 14), music_volume is 0 to 1, and either may carry
# a speaker's own value as <key>.<Name>.  effects.<Name> is a speaker's chain
# without the level gain; an empty value is plain, which is not an absent key
# (absent leaves the row's chain as it is).

LEVEL_BOUNDS = {"voice_gain": (-20, 14, int), "music_volume": (0.0, 1.0, float)}
_LEVEL_DEFAULTS = {"voice_gain": 8, "music_volume": 0.7}
_LEVEL_SPEAKER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


def _volume_path(tts: Path) -> Path:
    return _tts(tts) / "volume.conf"


def _volume_lines(tts: Path) -> list[str]:
    try:
        return _volume_path(tts).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _volume_pairs(tts: Path):
    for line in _volume_lines(tts):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, raw = line.partition("=")
            yield key.strip(), raw.strip()


def _coerce(key: str, raw: str):
    """A hand-edited value that is not a number reads as the default."""
    try:
        return LEVEL_BOUNDS[key][2](raw)
    except (TypeError, ValueError):
        return _LEVEL_DEFAULTS[key]


def _write_volume_key(tts: Path, key: str, value) -> None:
    """Rewrite one key in place, so the file's comments and every other key
    stay; a key that is not there yet is appended."""
    lines = _volume_lines(tts)
    for index, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key and not line.lstrip().startswith("#"):
            lines[index] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    _atomic(_volume_path(tts), "\n".join(lines) + "\n")


def levels(tts: Path) -> dict:
    out, by_speaker = dict(_LEVEL_DEFAULTS), {}
    for key, raw in _volume_pairs(tts):
        base, _, who = key.partition(".")
        if base not in LEVEL_BOUNDS:
            continue
        if who:
            by_speaker.setdefault(who, {})[base] = _coerce(base, raw)
        else:
            out[base] = _coerce(base, raw)
    return {**out, "speakers": by_speaker}


def set_level(tts: Path, key: str, value, speaker: str | None = None) -> dict:
    """Write one level, or a speaker's own; refuse anything unusable."""
    if key not in LEVEL_BOUNDS:
        raise ValueError(f"unknown level: {key}")
    low, high, cast = LEVEL_BOUNDS[key]
    if isinstance(value, bool):
        raise ValueError("level must be a number")
    try:
        value = cast(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number") from None
    if not low <= value <= high:
        raise ValueError(f"{key} must be between {low} and {high}")
    # The name becomes part of a key: a newline or "=" in it would forge one.
    if speaker is not None and not _LEVEL_SPEAKER.match(speaker):
        raise ValueError("speaker must be a single plain word")
    _write_volume_key(tts, f"{key}.{speaker}" if speaker else key, value)
    return levels(tts)


def effects_overrides(tts: Path) -> dict[str, str]:
    """Speaker name to effect chain, without the level gain."""
    out = {}
    for key, raw in _volume_pairs(tts):
        base, _, who = key.partition(".")
        if base == "effects" and who:
            out[who] = raw
    return out


def set_effects_override(tts: Path, name: str, chain: str) -> None:
    _write_volume_key(tts, f"effects.{name}", chain)


# --- hold -----------------------------------------------------------------
# Pause without losing anything: while a hold is in force speak.sh writes each
# speaker's clips to held/<Name>, and quiet.sh resume speaks them.  Mute drops;
# hold keeps.

# What speak.sh writes to .autohold when it holds by itself, in words.  A hold
# set by hand has no reason to show, and shows none.
AUTOHOLD_REASONS = {"volume": "phone is muted", "alarm": "alarm set", "call": "on a call"}


def _held_dir(tts: Path) -> Path:
    return _tts(tts) / "held"


def _autohold_reason(tts: Path) -> str | None:
    try:
        raw = (_tts(tts) / ".autohold").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return AUTOHOLD_REASONS.get(raw)


def _held_since(tts: Path) -> int | None:
    """The epoch the pause started, which speak.sh and quiet.sh stamp. Not
    the hold file's time: quiet.sh removes that file first on resume."""
    try:
        return int((_tts(tts) / ".hold-since").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def held(tts: Path) -> bool:
    """Whether a hold stands, without reading what waits: a watcher asks
    this twice a second."""
    return _expiry(_tts(tts) / "hold") is not None


def hold_state(tts: Path) -> dict:
    """until: the epoch the hold ends, 0 for until resumed, None when not held.
    waiting: how many speakers have clips kept.  While held, also `reason`
    (why the coordinator held by itself, None for a hold set by hand) and
    `since`.  The .autohold latch outlives a hold on purpose, so it is read
    only while one stands."""
    until = _expiry(_tts(tts) / "hold")
    state = {"until": until, "waiting": len(held_clips(tts))}
    if until is not None:
        state["reason"] = _autohold_reason(tts)
        state["since"] = _held_since(tts)
    return state


def held_clips(tts: Path) -> list[dict]:
    """Who is waiting and what they will say, oldest first, which is the
    order quiet.sh speaks them in.  Each line of held/<Name> is
    "<epoch>\\t<project>/<branch>\\t<text>"; a line in another shape is kept
    whole rather than dropped."""
    directory = _held_dir(tts)
    try:
        entries = [path for path in directory.iterdir() if path.is_file()]
    except OSError:
        return []
    out = []
    for path in sorted(entries, key=lambda item: item.stat().st_mtime):
        # The filename is a speaker name and reaches a command line on release.
        if not NAME.fullmatch(path.name):
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        clips, where = [], ""
        for line in raw.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                clips.append(parts[2].strip())
                if parts[1] and parts[1] != "-/-":
                    where = parts[1]
            else:
                clips.append(line.strip())
        clips = [clip for clip in clips if clip]
        if clips:
            out.append({"name": path.name, "text": clips[0], "count": len(clips), "clips": clips, "where": where})
    return out


def set_hold(tts: Path, minutes: int | None) -> dict:
    """minutes: None holds until resumed, 0 lifts.  Lifting does not speak
    what waited; that is release_held()."""
    if minutes is not None and (type(minutes) is not int or minutes < 0 or minutes > MAX_MUTE_MINUTES):
        raise ValueError(f"minutes must be between 0 and {MAX_MUTE_MINUTES}")
    path = _tts(tts) / "hold"
    if minutes == 0:
        path.unlink(missing_ok=True)
    else:
        _atomic(path, f"{0 if minutes is None else int(time.time()) + minutes * 60}\n")
    return hold_state(tts)


def release_held(tts: Path) -> dict:
    """Lift the hold and let quiet.sh speak what waited.

    quiet.sh owns the flush and runs detached, one fixed executable with one
    fixed argument and no shell: speaking a backlog takes minutes.  The answer
    comes once the hold file is actually gone, quiet.sh's first act, rather
    than once a process merely started.
    """
    waiting = len(held_clips(tts))
    script = _tts(tts) / "quiet.sh"
    if not (script.is_file() and os.access(script, os.X_OK)):
        return {"released": 0, "detail": "release is unavailable: no executable tts/quiet.sh"}
    try:
        subprocess.Popen([str(script), "resume"], shell=False, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        return {"released": 0, "detail": f"could not run quiet.sh: {exc}"}
    hold = _tts(tts) / "hold"
    for _ in range(60):                 # about 3 s
        if not hold.exists():
            break
        time.sleep(0.05)
    else:
        return {"released": 0, "detail": "the hold did not lift: quiet.sh may have failed to start"}
    return {"released": waiting, "detail": "", **hold_state(tts)}


def roundup_ready(tts: Path) -> bool:
    """Whether the coordinator has written its roundup of what waits.

    Its cache: every held/<who> has a .sum-<who> whose first line is the
    count of non-empty lines in held/<who>, and .lead's first line is
    "<who>:<n> " per speaker, compared as a set.  One speaker is never a
    roundup, and anything unreadable is not ready.
    """
    try:
        if _expiry(_tts(tts) / "hold") is None:
            return False
        directory, signature = _held_dir(tts), []
        for path in directory.iterdir():
            if path.name.startswith(".") or not path.is_file():
                continue
            count = sum(1 for line in path.read_text(encoding="utf-8", errors="replace").split("\n") if line)
            summary = (directory / f".sum-{path.name}").read_text(encoding="utf-8", errors="replace")
            if not summary or summary.split("\n", 1)[0] != str(count):
                return False
            signature.append(f"{path.name}:{count}")
        if len(signature) < 2:
            return False
        lead = (directory / ".lead").read_text(encoding="utf-8", errors="replace")
        first = lead.split("\n", 1)[0]
        return bool(lead) and first.endswith(" ") and sorted(first.split(" ")[:-1]) == sorted(signature)
    except (OSError, ValueError):
        return False


# --- a live call's hold ---------------------------------------------------
# A call holds the clips of the sessions it talks to, one speaker at a time:
# callhold-<Name> is an expiry each poll renews, and speak.sh keeps that
# speaker's clips in held-call/<Name> while it stands.  Hold keeps; it never
# drops.

CALL_HOLD_MINUTES = 5
_CALL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")


def call_hold(tts: Path, name: str, minutes: int = CALL_HOLD_MINUTES, now: float | None = None) -> bool:
    if not _CALL_NAME.fullmatch(str(name or "")) or not _tts(tts).is_dir():
        return False
    until = int((time.time() if now is None else now) + minutes * 60)
    _atomic(_tts(tts) / f"callhold-{name}", f"{until}\n")
    return True


def call_release(tts: Path, name: str, run=subprocess.run) -> dict:
    """End one speaker's call hold: quiet.sh callrelease, which owns the
    spool.  One fixed executable, a fixed verb and a checked name, no shell."""
    if not _CALL_NAME.fullmatch(str(name or "")):
        return {"ok": False, "detail": "not a speaker name"}
    script = _tts(tts) / "quiet.sh"
    if not (script.is_file() and os.access(script, os.X_OK)):
        try:
            (_tts(tts) / f"callhold-{name}").unlink()
        except OSError:
            pass
        return {"ok": False, "detail": "no executable tts/quiet.sh: the hold is lifted, any clips stay in tts/held-call"}
    try:
        result = run([str(script), "callrelease", name], shell=False, stdin=subprocess.DEVNULL,
                     capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "detail": f"could not run quiet.sh: {str(exc)[:80]}"}
    ok = result.returncode == 0
    return {"ok": ok, "detail": "" if ok else (result.stderr or result.stdout or "").strip()[:200]}


def speak_test(tts: Path) -> dict:
    """Speak one sample line at the current levels, through the coordinator's
    own gates (quiet.sh test), so "not spoken: muted" comes back as the detail
    rather than as a clip nobody heard.  One fixed executable, one fixed
    argument, no shell."""
    script = _tts(tts) / "quiet.sh"
    if not (script.is_file() and os.access(script, os.X_OK)):
        return {"spoken": False, "detail": "test is unavailable: no executable tts/quiet.sh"}
    try:
        result = subprocess.run([str(script), "test"], shell=False, timeout=120, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return {"spoken": False, "detail": "the phone timed out"}
    except OSError as exc:
        return {"spoken": False, "detail": f"could not run quiet.sh: {exc}"}
    lines = (result.stdout or result.stderr or "").strip().splitlines()
    return {"spoken": result.returncode == 0, "detail": lines[-1] if lines else ""}


# --- what was said --------------------------------------------------------

_TRANSCRIPT_LINE = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
    r"(?P<kind>SPOKE|MUTED|HELD|SCP-FAIL|PLAY-FAIL)"
    r"(?:\((?P<paren>[^)]*)\))?"
    r"(?P<rest>.*)$")
_SENTENCE_END = re.compile(r"(?<!\.)\.(?!\.)\s+")


def _sentences(text: str) -> list[str]:
    """Split on sentence ends, keeping an ellipsis whole."""
    out, last = [], 0
    for match in _SENTENCE_END.finditer(text):
        out.append(text[last:match.end()].strip())
        last = match.end()
    if text[last:].strip():
        out.append(text[last:].strip())
    return out


def strip_opener(text: str | None, name: str | None) -> str | None:
    """`text` without the coordinator's generated opener ("This is <Name>,
    over in <project>."), or unchanged when there is none.

    Deliberately timid: one sentence goes if it names the speaker, and a
    second only when the first was the bare name, since then the second is
    the project.  A clip that is nothing but its opener keeps it all.
    """
    if not text or not name:
        return text
    parts = _sentences(text)
    if len(parts) < 2 or not re.search(rf"\b{re.escape(name)}\b", parts[0], re.I):
        return text
    drop = 2 if re.fullmatch(rf"{re.escape(name)}[.,]?", parts[0].strip(), re.I) and len(parts) > 2 else 1
    return " ".join(parts[drop:]).strip() or text


def transcript(tts: Path, limit: int = 40, tail_bytes: int | None = None) -> list[dict]:
    """Recent clips, newest first, from the log's tail.

    Only lines that are a clip: QUEUE, OVERFLOW and DROP are bookkeeping.
    MUTED(<Name>) names the speaker, while MUTED(all) and MUTED(agentvibes)
    are reasons.  A PLAY-FAIL keeps what was said and its error as `detail`;
    an SCP-FAIL's payload is the delivery error, so its text is absent.
    """
    tail = _log_tail(tts, tail_bytes or _LOG_TAIL_BYTES)
    if tail is None:
        return []
    out = []
    for line in tail.splitlines():
        match = _TRANSCRIPT_LINE.match(line.strip())
        if not match:
            continue
        kind, paren, rest = match["kind"], match["paren"], match["rest"]
        head, separator, payload = rest.partition(" :: ")
        if not separator:
            continue
        words = head.split()
        name = words[0] if words and NAME.fullmatch(words[0]) else None
        text, detail, reason = payload.strip(), "", None
        if kind == "SPOKE":
            state = "spoke"
        elif kind == "MUTED":
            state = "muted"
            if paren and paren not in ("all", "agentvibes") and NAME.fullmatch(paren):
                name = paren
            else:
                reason = paren or None
        elif kind == "HELD":
            state, reason = "held", paren or None
        elif kind == "PLAY-FAIL":
            state = "failed"
            why, separator2, said = payload.partition(" :: ")
            detail, text = why.strip(), (said.strip() if separator2 else None)
        else:
            state, detail, text = "failed", payload.strip(), None
        out.append({"name": name, "text": text or None, "body": strip_opener(text or None, name),
                    "state": state, "reason": reason, "detail": detail, "at": _parse_stamp(line)})
    out.reverse()
    return out[:max(0, int(limit))]


# Wider than the transcript's tail: an idle speaker's last clip can be hours
# back behind bookkeeping lines.  Cached on the log's size and mtime, since a
# sessions list polls this every few seconds.
_SAID_TAIL_BYTES = 512 * 1024
_said_cache: dict = {}


def last_said(tts: Path) -> dict[str, dict]:
    """{speaker: {"text", "at", "state"}} for each speaker's newest clip with
    words, its opener stripped."""
    path = _tts(tts) / "speak.log"
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if _said_cache.get("key") == key:
        return _said_cache["data"]
    out = {}
    for clip in transcript(tts, limit=100_000, tail_bytes=_SAID_TAIL_BYTES):
        name, body = clip.get("name"), clip.get("body")
        if name and body and name not in out:
            out[name] = {"text": body, "at": clip.get("at"), "state": clip.get("state")}
    _said_cache.update(key=key, data=out)
    return out


def quiet_snapshot(tts: Path) -> dict:
    return {"announcements": announcements(tts), "mutes": mute_state(tts), "kinds": list(ANNOUNCEMENTS),
            "levels": levels(tts), "subtitles": subtitles(tts), "subtitles_available": subtitles_available(tts),
            "hold": hold_state(tts), "held": held_clips(tts), "roundup_ready": roundup_ready(tts)}


def snapshot(tts: Path) -> dict:
    return {"speakers": speakers(tts), "tracks": tracks(tts), "presets": sorted(PRESETS), "voices": voices(tts),
            "music_enabled": music_enabled(tts), "stop_available": stop_available(tts),
            "quiet": quiet_snapshot(tts), "health": tts_health(tts)}
