"""The voice lab's routes: the summary, a build's decision, a cast voice's
tier, a test line and a build's takes (agent_companion.voicelab), and the
voicelab/ pipeline's clips for an install with no coordinator."""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .. import addons, auditions, safe_paths, voicelab, voices
from . import auth

TITLE = "Voice lab"
router = APIRouter()
NOSTORE = {"Cache-Control": "no-store"}


def _need_tts() -> Path:
    if not voicelab.configured():
        raise HTTPException(503, "comms is not set up: set tts_dir to the coordinator directory")
    return voicelab.tts_dir()


def _from_page(request: Request) -> None:
    # The origin is checked here as well as in require_write: a host app may
    # stand its own write check in for that guard.
    if not auth.same_origin(request.headers):
        raise HTTPException(status_code=403, detail="writes come from this page only")


@router.get("/api/voicelab", dependencies=[Depends(auth.require_token)])
def get_summary():
    """Read only. The voice state is asked only where a cast is configured:
    without remote-voices.conf it would say ready without asking anyone, and
    the page would call that free."""
    if not voicelab.configured():
        return JSONResponse(voicelab.pipeline_summary(), headers=NOSTORE)
    tts = voicelab.tts_dir()
    vs = None
    if (tts / "remote-voices.conf").is_file():
        try:
            vs = voices.state(tts)
        except Exception:
            vs = None
    return JSONResponse(voicelab.summary(tts, voicelab.lab_root(), vs, time.time()), headers=NOSTORE)


@router.post("/api/voicelab/tier/{name}", dependencies=[Depends(auth.require_write)])
def set_tier(name: str, request: Request, body: dict = Body(...)):
    """A cast voice's tier button: tiers.conf, through the writer an audition's pick uses."""
    _from_page(request)
    tts, tier = _need_tts(), body.get("tier")
    if not auditions.NAME.fullmatch(name) or type(tier) is not int or tier not in (1, 2, 3):
        raise HTTPException(400, "a voice name and a tier of 1, 2 or 3")
    cast = voicelab.cast_name(tts, name)
    if cast is None:
        raise HTTPException(404, "no such voice in the cast")
    try:
        auditions.set_tier(tts, cast, tier)
    except OSError as e:
        raise HTTPException(500, f"could not save: {e.strerror or e}")
    return JSONResponse({"ok": True}, headers=NOSTORE)


@router.post("/api/voicelab/decide/{dir}", dependencies=[Depends(auth.require_write)])
def decide_build(dir: str, request: Request, body: dict = Body(...)):
    """Want it / Not this one on a build. A newly wanted build goes to the addons
    module's voicelab_decided, which may file the work to make it live: the page
    never changes comms itself."""
    _from_page(request)
    tts = _need_tts()
    want = body.get("want")
    if not isinstance(want, bool):
        raise HTTPException(400, "want must be true or false")
    lab = voicelab.lab_root()
    build = safe_paths.directory(lab, dir) if voicelab.is_build_dir(dir) else None
    if build is None or not (build / "voice.json").is_file():
        raise HTTPException(404, "no such build")
    before = voicelab.decision_of(build)
    try:
        voicelab.decide(lab, dir, want)
    except ValueError:
        raise HTTPException(404, "no such build") from None
    except OSError as e:
        raise HTTPException(500, f"could not save: {e.strerror or e}")
    filed, detail = False, ""
    if want and before and before.get("want") is True:
        detail = "already filed"
    elif want:
        try:  # the decision is saved; a cast read that fails only costs the name
            cast = voicelab.cast(tts, time.time()).get("voices", [])
            vid = voicelab.voice_of(dir, cast)
            name = next((v["name"] for v in cast if v["voice"] == vid), vid or dir)
        except Exception:
            name = dir
        got = addons.voicelab_decided(name, dir, lab / dir, want)
        filed, detail = got["filed"], got["detail"]
    return JSONResponse({"ok": True, "filed": filed, "detail": detail}, headers=NOSTORE)


@router.post("/api/voicelab/test/{name}", dependencies=[Depends(auth.require_write)])
def render_test(name: str, request: Request, body: dict = Body(...)):
    """Say one line as a cast voice and return the wav, unplayed. One render at a
    time; the text is never logged. X-Rendered-By says which host made it."""
    _from_page(request)
    tts = _need_tts()
    if not auditions.NAME.fullmatch(name):
        raise HTTPException(400, "not a voice name")
    try:
        text = voicelab.test_text(body.get("text"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    cast = voicelab.cast_name(tts, name)
    if cast is None:
        raise HTTPException(404, "no such voice in the cast")
    if not voicelab._test_lock.acquire(blocking=False):
        raise HTTPException(409, "another test line is rendering")
    try:
        wav, by = voicelab.test_line(tts, cast, text)
    except voicelab.TestLineError as e:
        raise HTTPException(502, str(e))
    finally:
        voicelab._test_lock.release()
    # A header carries latin-1 only, and a fallback's reason is speak.log's own words.
    by = by.encode("latin-1", "replace").decode("latin-1")
    return Response(wav, media_type="audio/wav", headers={**NOSTORE, "X-Rendered-By": by})


@router.get("/api/voicelab/clip/{dir}/{clip}", dependencies=[Depends(auth.require_token)])
def get_clip(dir: str, clip: str):
    """One audition take of a build (new-N.mp3 or live-N.mp3). Both names are matched
    whole before they touch a path; the build must be one (it has a voice.json)."""
    if not voicelab.is_build_dir(dir) or not voicelab.CLIP.fullmatch(clip):
        raise HTTPException(404, "no such clip")
    lab = voicelab.lab_root()
    root = safe_paths.directory(lab, dir)
    f = safe_paths.file(lab, Path(dir) / "audition" / clip) if root else None
    if root is None or not (root / "voice.json").is_file() or f is None:
        raise HTTPException(404, "no such clip")
    return FileResponse(f, media_type="audio/mpeg", headers=NOSTORE)


@router.get("/api/voicelab/audition/{name}/{file:path}", dependencies=[Depends(auth.require_token)])
def pipeline_audition(name: str, file: str):
    path = safe_paths.file(voicelab.voices_dir() / name / "audition", file) if voicelab.VOICE_NAME.match(name) else None
    if path is None or path.suffix.lower() not in voicelab.AUDIO:
        raise HTTPException(404, "no such clip")
    return FileResponse(path, media_type=voicelab.AUDIO[path.suffix.lower()], headers=NOSTORE)
