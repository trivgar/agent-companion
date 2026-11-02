"""Comms: the clips speak.sh renders, played by the phone page.

speak.sh writes <id>.wav and <id>.json into the clips directory; the page asks
for the oldest unplayed one, plays it, and says so. While a hold is on, nothing
is offered and the clips wait.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .. import agentvibes, auditions, commsapp, commsbusy, config, safe_paths, ttsoutbox, voices
from . import auth

TITLE = "Comms"
router = APIRouter()
ID_RE = re.compile(r"^[0-9]+-[A-Za-z0-9_]+$")


def _data() -> Path:
    return Path(os.environ.get("COMMS_DATA_DIR") or Path.home() / ".local/share/agent-companion")


def clips_dir() -> Path:
    return Path(os.environ.get("COMMS_CLIPS_DIR") or _data() / "clips")


def state_dir() -> Path:
    return Path(os.environ.get("COMMS_STATE_DIR") or _data() / "comms")


def held() -> bool:
    try:
        until = int("".join(c for c in (state_dir() / "hold").read_text() if c.isdigit()) or "0")
    except (OSError, ValueError):
        return False
    return until == 0 or time.time() < until


def _volume_of(data: dict) -> float:
    v = data.get("volume")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1 else 1.0


def _meta(clip_id: str) -> dict | None:
    d = clips_dir()
    try:
        meta = safe_paths.file(d, f"{clip_id}.json")
        wav = safe_paths.file(d, f"{clip_id}.wav")
        data = json.loads(meta.read_text()) if meta else None
    except (OSError, ValueError):
        return None
    if wav is None or not isinstance(data, dict):
        return None
    at = data.get("at") if isinstance(data.get("at"), (int, float)) else 0
    return {"id": clip_id, "speaker": str(data.get("speaker", "")), "text": str(data.get("text", "")),
            "at": at, "volume": _volume_of(data), "age": max(0, int(time.time() - at)) if at else 0}


def _unplayed() -> list[str]:
    d = clips_dir()
    try:
        names = sorted(p.stem for p in d.glob("*.json") if not p.name.startswith("."))
    except OSError:
        return []
    return [n for n in names if ID_RE.match(n) and not (d / f"{n}.played").exists()]


@router.get("/api/comms/next", dependencies=[Depends(auth.require_token)])
def next_clip():
    is_held = held()
    if not is_held:
        for cid in _unplayed():
            m = _meta(cid)
            if m:
                return {"clip": m, "held": False}
    return {"clip": None, "held": is_held}


@router.get("/api/comms/clip/{clip_id}", dependencies=[Depends(auth.require_token)])
def clip(clip_id: str):
    if not ID_RE.match(clip_id):
        raise HTTPException(400, "bad clip id")
    wav = safe_paths.file(clips_dir(), f"{clip_id}.wav")
    if wav is None:
        raise HTTPException(404, "no such clip")
    return FileResponse(wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})


class Played(BaseModel):
    id: str


@router.post("/api/comms/played", dependencies=[Depends(auth.require_write)])
def played(body: Played):
    if not ID_RE.match(body.id):
        raise HTTPException(400, "bad clip id")
    if _meta(body.id) is None:
        raise HTTPException(404, "no such clip")
    (clips_dir() / f"{body.id}.played").touch()
    return {"ok": True}


# The original compact API above remains while older dashboard assets are in
# use.  The coordinator API below is the full interface used by the moved
# comms page and mobile player.
NOSTORE = {"Cache-Control": "no-store"}
TRACK = re.compile(r"[A-Za-z0-9_.-]{1,120}\.(?:mp3|ogg|opus|wav)$")


def tts_dir() -> Path:
    """The optional speech coordinator directory.

    An unset setting deliberately points at a normal application-data path,
    rather than a user's private dot directory.  Its absence is reported by
    each TTS route as a setup problem.
    """
    raw = config.get("tts_dir")
    return Path(str(raw)).expanduser() if raw else Path.home() / ".local" / "share" / "agent-companion" / "tts"


def outbox_dir() -> Path:
    raw = config.get("tts_outbox_dir")
    return Path(str(raw)).expanduser() if raw else tts_dir() / "outbox"


def _app_slug() -> str:
    name = str(config.get("app_name") or "Companion").lower()
    return "".join(char for char in name if char.isascii() and (char.isalnum())) or "companion"


def _apk_path() -> Path | None:
    raw = config.get("android_apk_file")
    return Path(str(raw)).expanduser() if raw else None


def _tts() -> Path:
    directory = tts_dir()
    if not agentvibes.available(directory):
        raise HTTPException(503, "comms is not set up: set tts_dir to the coordinator directory")
    return directory


def _held(tts: Path) -> bool:
    """A plug point can only defer clips, never make a held queue available."""
    return commsbusy.held(tts)


def start(_app) -> None:
    """Wake a registered mobile player even when no dashboard is open."""
    tts = tts_dir()
    if not agentvibes.available(tts) or not commsapp.configured():
        return

    def watch() -> None:
        while True:
            try:
                if not _held(tts):
                    commsapp.push_pending(tts, outbox_dir())
            except Exception:
                pass
            time.sleep(0.5)

    threading.Thread(target=watch, name="agent-companion-comms", daemon=True).start()


def _station_conf(tts: Path) -> dict[str, str]:
    out = {}
    try:
        lines = (tts / "station.conf").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _rotation(conf: dict[str, str]) -> list[str]:
    return [item.strip() for item in conf.get("rotate", "").split(",") if item.strip()]


def _station_music(tts: Path, now: float | None = None) -> str:
    conf, rotation = _station_conf(tts), _rotation(_station_conf(tts))
    if rotation:
        try:
            minutes = max(1, int(conf.get("rotate_min", "20")))
        except ValueError:
            minutes = 20
        return rotation[int((time.time() if now is None else now) // (minutes * 60)) % len(rotation)]
    return conf.get("music", "")


@router.get("/api/tts", dependencies=[Depends(auth.require_token)])
def tts_state() -> dict:
    tts = _tts()
    snapshot = agentvibes.snapshot(tts)
    snapshot["held"] = _held(tts)
    snapshot["app"] = commsapp.status(tts)
    return snapshot


@router.get("/api/tts/voices", dependencies=[Depends(auth.require_token)])
def tts_voices() -> dict:
    return voices.state(_tts())


@router.get("/api/tts/phonessh", dependencies=[Depends(auth.require_token)])
def tts_phone_ssh() -> dict:
    _tts()
    return {"ok": False, "detail": "phone communication checks are not configured"}


@router.post("/api/tts/music", dependencies=[Depends(auth.require_write)])
def tts_music(body: dict = Body(...)) -> dict:
    return {"music_enabled": agentvibes.set_music_enabled(_tts(), bool(body.get("enabled")))}


@router.post("/api/tts/subtitles", dependencies=[Depends(auth.require_write)])
def tts_subtitles(body: dict = Body(...)) -> dict:
    return {"subtitles": agentvibes.set_subtitles(_tts(), bool(body.get("enabled")))}


@router.post("/api/tts/speaker", dependencies=[Depends(auth.require_write)])
def tts_speaker(body: dict = Body(...)) -> dict:
    try:
        return agentvibes.update_speaker(_tts(), str(body.get("name") or ""), body.get("preset"), body.get("track"),
                                         body.get("volume"), body.get("voice"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/tts/mute", dependencies=[Depends(auth.require_write)])
def tts_mute(body: dict = Body(...)) -> dict:
    try:
        return agentvibes.set_mute(_tts(), str(body.get("scope") or "all"), body.get("minutes"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/tts/announce", dependencies=[Depends(auth.require_write)])
def tts_announce(body: dict = Body(...)) -> dict:
    try:
        return agentvibes.set_announcement(_tts(), str(body.get("kind") or ""), bool(body.get("enabled")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/tts/level", dependencies=[Depends(auth.require_write)])
def tts_level(body: dict = Body(...)) -> dict:
    try:
        speaker = body.get("speaker")
        return agentvibes.set_level(_tts(), str(body.get("key") or ""), body.get("value"), str(speaker) if speaker else None)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/tts/phone", dependencies=[Depends(auth.require_token)])
def tts_phone() -> dict:
    _tts()
    return {"ok": False, "detail": "phone checks are not configured"}


# These remain GETs because the Android player fetches them natively and
# cannot supply same-origin headers. They still require the owner key.
@router.get("/api/tts/outbox", dependencies=[Depends(auth.require_token)])
def tts_outbox(after: int = 0, who: str = "phone") -> dict:
    if who == "web" and not agentvibes.available(tts_dir()):
        # The page asks this first on every load; a 200 keeps its console
        # clean. Other players still get the 503 they expect.
        return {"set_up": False, "clips": []}
    tts, root = _tts(), outbox_dir()
    who = who if who in ttsoutbox.PLAYERS else "phone"
    ttsoutbox.seen(root)
    if who == "phone" and _held(tts):
        return {"clips": [], "top": ttsoutbox.top(root), "held": True, "now": time.time()}
    if who == "phone" and ttsoutbox.web_player(root):
        return {"clips": [], "top": ttsoutbox.top(root), "deferred": "web player is active", "now": time.time()}
    if not _held(tts):
        commsapp.push_pending(tts, root)
    return {"clips": ttsoutbox.pending(max(0, after), root, who), "top": ttsoutbox.top(root),
            "held": _held(tts), "now": time.time()}


@router.get("/api/tts/station", dependencies=[Depends(auth.require_token)])
def tts_station() -> dict:
    tts, conf = _tts(), _station_conf(_tts())
    return {"music": _station_music(tts), "rotate": _rotation(conf) or [conf.get("music", "")], "tracks": agentvibes.tracks(tts)}


@router.get("/api/tts/track/{name}", dependencies=[Depends(auth.require_token)])
def tts_track(name: str):
    tts = _tts()
    path = safe_paths.file(tts.parent / "audio" / "tracks", name) if TRACK.fullmatch(name) else None
    if path is None:
        raise HTTPException(404, "no such track")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


@router.post("/api/tts/station", dependencies=[Depends(auth.require_write)])
def set_station(body: dict = Body(default={})) -> dict:
    tts = _tts()
    values = body.get("rotate") if isinstance(body.get("rotate"), list) else [body.get("music")]
    names = list(dict.fromkeys(str(value or "") for value in values))
    tracks = tts.parent / "audio" / "tracks"
    if not names or not all(TRACK.fullmatch(name) and safe_paths.file(tracks, name) is not None for name in names):
        raise HTTPException(400, "no such track")
    conf = _station_conf(tts)
    conf.pop("rotate", None)
    conf["music"] = names[0]
    if len(names) > 1:
        conf["rotate"] = ",".join(names)
    temporary = tts / ".station.conf.tmp"
    temporary.write_text("".join(f"{key}={value}\n" for key, value in conf.items()))
    os.replace(temporary, tts / "station.conf")
    return {"ok": True, "music": _station_music(tts), "rotate": names}


@router.get("/api/tts/bed", dependencies=[Depends(auth.require_token)])
def tts_bed():
    tts, name = _tts(), _station_music(_tts())
    path = safe_paths.file(tts.parent / "audio" / "tracks", name) if TRACK.fullmatch(name) else None
    if path is None:
        raise HTTPException(404, "no station music")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


@router.post("/api/tts/player", dependencies=[Depends(auth.require_write)])
def tts_player(body: dict = Body(default={})) -> dict:
    _tts()
    player = str(body.get("id") or "")
    if not player or len(player) > 64:
        raise HTTPException(400, "id")
    if body.get("release"):
        ttsoutbox.release(player, outbox_dir())
    else:
        ttsoutbox.claim(player, outbox_dir())
    return {"ok": True}


# This remains a GET for the same native Android fetch. It requires the owner
# key even though it cannot use the browser's same-origin write guard.
@router.get("/api/tts/clip/{seq}", dependencies=[Depends(auth.require_token)])
def tts_clip(seq: int, who: str = "phone"):
    tts, root = _tts(), outbox_dir()
    if _held(tts):
        raise HTTPException(409, "comms are on hold")
    path = ttsoutbox.clip(seq, root)
    if not path:
        raise HTTPException(404, "no such clip")
    who = who if who in ttsoutbox.PLAYERS else "phone"
    if not ttsoutbox.take(seq, who, root):
        raise HTTPException(409, "another player has this clip")
    if who == "app":
        commsapp.took(tts)
    return FileResponse(path, media_type="audio/ogg", headers=NOSTORE)


@router.post("/api/tts/app", dependencies=[Depends(auth.require_write)])
def tts_app_register(body: dict = Body(...)) -> dict:
    tts = _tts()
    if not commsapp.configured():
        raise HTTPException(503, "mobile comms is not set up: set fcm_service_account_file")
    token, ring = str(body.get("token") or ""), body.get("ring") is True
    try:
        out = commsapp.register(tts, token, notify=body.get("notify") is True, ring=ring)
    except ValueError:
        raise HTTPException(400, "token")
    if ring:
        from .. import fcm
        fcm.save_device(token, ring=True)      # the live call's ringer, the same phone
    return out


@router.get("/api/tts/app", dependencies=[Depends(auth.require_token)])
def tts_app_status() -> dict:
    return commsapp.status(_tts())


@router.get("/api/tts/note/{note_id}", dependencies=[Depends(auth.require_token)])
def tts_note(note_id: str) -> dict:
    """Notification payloads are optional, and never fabricated on a miss."""
    _tts()
    raise HTTPException(404, "no such notification")


@router.get("/app/{slug}.apk", dependencies=[Depends(auth.require_token)])
def android_apk(slug: str):
    """The configured Android build, named from the configured app name."""
    path = _apk_path()
    path = safe_paths.file(path.parent, path.name) if path else None
    if slug != _app_slug() or path is None:
        raise HTTPException(404, "Android build is not available: set android_apk_file")
    return FileResponse(path, media_type="application/vnd.android.package-archive", filename=f"{slug}.apk", headers=NOSTORE)


@router.get("/app/version", dependencies=[Depends(auth.require_token)])
def android_version() -> JSONResponse:
    path = _apk_path()
    try:
        value = json.loads((path.parent / "version.json").read_text()) if path and path.is_file() else {}
        out = {"code": int(value["code"]), "name": str(value["name"])[:20]}
    except (OSError, ValueError, KeyError, TypeError):
        out = {"code": 0, "name": ""}
    return JSONResponse(out, headers=NOSTORE)


@router.post("/api/tts/played", dependencies=[Depends(auth.require_write)])
def tts_played(body: dict = Body(...)) -> dict:
    _tts()
    try:
        sequence = int(body.get("seq"))
    except (TypeError, ValueError):
        raise HTTPException(400, "seq")
    if not ttsoutbox.mark(sequence, str(body.get("result") or ""), str(body.get("detail") or ""), outbox_dir()):
        raise HTTPException(404, "no such clip, or invalid result")
    return {"ok": True}


@router.post("/api/tts/replay", dependencies=[Depends(auth.require_write)])
def tts_replay(body: dict = Body(...)) -> dict:
    _tts()
    try:
        sequence = int(body.get("seq"))
    except (TypeError, ValueError):
        raise HTTPException(400, "seq")
    new = ttsoutbox.replay(sequence, outbox_dir())
    if new is None:
        raise HTTPException(404, "that clip is gone")
    return {"ok": True, "seq": new}


@router.get("/api/tts/transcript", dependencies=[Depends(auth.require_token)])
def tts_transcript(limit: int = 40) -> dict:
    tts, root = _tts(), outbox_dir()
    clips = agentvibes.transcript(tts, min(max(limit, 1), 200))
    index = ttsoutbox.index(root)
    # A spoken line the outbox still holds can be played again; only spoken
    # ones, so a muted or failed clip never offers a replay.
    for clip_item in clips:
        if clip_item.get("state") == "spoke":
            seq = ttsoutbox.seq_for(clip_item.get("name"), clip_item.get("body") or clip_item.get("text"),
                                    clip_item.get("at"), index)
            if seq is not None:
                clip_item["seq"] = seq
    return {"clips": clips}


@router.post("/api/tts/hold", dependencies=[Depends(auth.require_write)])
def tts_hold(body: dict = Body(...)) -> dict:
    try:
        state = agentvibes.set_hold(_tts(), body.get("minutes"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {**state, "held": _held(_tts())}


@router.post("/api/tts/release", dependencies=[Depends(auth.require_write)])
def tts_release() -> dict:
    tts = _tts()
    if commsbusy.reason():
        return {"ok": False, "detail": "comms are held by an add-on"}
    return agentvibes.release_held(tts)


@router.post("/api/tts/test", dependencies=[Depends(auth.require_write)])
def tts_test() -> dict:
    return agentvibes.speak_test(_tts())


@router.post("/api/tts/stop", dependencies=[Depends(auth.require_write)])
def tts_stop() -> dict:
    return agentvibes.stop_playback(_tts())


@router.get("/api/tts/devicecheck", dependencies=[Depends(auth.require_token)])
def tts_devicecheck() -> dict:
    _tts()
    return {"running": False, "reports": [], "detail": "device checks are not configured"}


@router.post("/api/tts/devicecheck", dependencies=[Depends(auth.require_write)])
def start_devicecheck() -> dict:
    _tts()
    return {"ok": False, "started": False, "detail": "device checks are not configured"}


@router.get("/api/voices/auditions", dependencies=[Depends(auth.require_token)])
def voice_auditions():
    return JSONResponse({"auditions": auditions.listing(_tts())}, headers=NOSTORE)


@router.get("/api/voices/auditions/{name}/{clip}", dependencies=[Depends(auth.require_token)])
def voice_audition_clip(name: str, clip: str):
    path = auditions.clip_path(_tts(), name, clip)
    if path is None:
        raise HTTPException(404, "no such clip")
    return FileResponse(path, media_type=auditions.TYPES[path.suffix[1:].lower()], headers=NOSTORE)


@router.post("/api/voices/auditions/{name}", dependencies=[Depends(auth.require_write)])
def decide_audition(name: str, body: dict = Body(...)) -> dict:
    tts, tier = _tts(), body.get("tier")
    if not auditions.NAME.fullmatch(name) or not auditions.valid_tier(tier):
        raise HTTPException(400, "a voice name and valid tier are required")
    if not auditions.exists(tts, name):
        raise HTTPException(404, "no such voice")
    try:
        auditions.decide(tts, name, tier)
    except ValueError:
        raise HTTPException(404, "no such voice") from None
    return {"ok": True}
