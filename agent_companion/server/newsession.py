"""Owner-only routes for starting a configured Claude Code session.

The browser sends a project's key, never a path. Every start attempt is
written to the chat log with the first message's length, never its words.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import addons, newsession
from . import auth, chat, sessions

router = APIRouter()
# The session rows the listing reads. None is the sessions view's own reader;
# a host app with a reader of its own sets it here, so one process never
# builds a second (each re-reads every transcript once).
_reader = None
_NOSTORE = {"Cache-Control": "no-store"}


def _rows() -> list[dict]:
    try:
        reader = _reader if _reader is not None else sessions._parts()["reader"]
        return reader.snapshot()
    except Exception:
        return []


def _who(request: Request) -> str:
    """Who asked, for the log: the add-on's id for the person, else the owner."""
    person = addons.person(request) or {}
    return str(person.get("id") or "owner")


@router.get("/api/newsession/projects", dependencies=[Depends(auth.require_token)])
def projects():
    return JSONResponse({"ok": True, "projects": newsession.listing(_rows()),
                         "default_model": newsession.default_model(),
                         "launchers": newsession.launcher_listing()}, headers=_NOSTORE)


@router.post("/api/newsession", dependencies=[Depends(auth.require_write)])
def start(request: Request, body: dict = Body(default={})):
    # A plain def: FastAPI runs it in a worker thread, so the up-to-12 s start
    # check never stalls the event loop for every other request. The origin is
    # checked here as well as in require_write: a host app may stand its own
    # write check in for that guard.
    if not auth.same_origin(request.headers):
        raise HTTPException(403, "writes come from this page only")
    body = body if isinstance(body, dict) else {}
    who = _who(request)
    raw = body.get("message")
    n = len(raw) if isinstance(raw, str) else 0
    proj = newsession.project(body.get("project"))
    if not proj:
        why = "not a configured project"
        chat.record(newsession.log_entry(who, "unknown", "", False, why, n))
        return JSONResponse({"ok": False, "detail": why}, status_code=400)
    message, why = newsession.clean_message(raw)
    if why:
        chat.record(newsession.log_entry(who, proj["key"], "", False, why, n))
        return JSONResponse({"ok": False, "detail": why}, status_code=400)
    # Absent or "" is plain claude; anything else must be a configured key.
    lkey = body.get("launcher")
    how = None
    if lkey not in (None, ""):
        how = newsession.launcher(lkey)
        if not how:
            why = "not a configured launcher"
            chat.record(newsession.log_entry(who, proj["key"], "", False, why, n))
            return JSONResponse({"ok": False, "detail": why}, status_code=400)
    r = newsession.launch(proj, message, launcher=how) if how else newsession.launch(proj, message)
    chat.record(newsession.log_entry(who, proj["key"], r.get("tmux") or "", bool(r.get("ok")), r.get("log") or "", n,
                                     launcher=how["key"] if how else ""))
    return JSONResponse({"ok": bool(r.get("ok")), "tmux": r.get("tmux") or "", "pid": r.get("pid"),
                         "detail": r.get("detail") or ""}, status_code=int(r.get("status") or 500))
