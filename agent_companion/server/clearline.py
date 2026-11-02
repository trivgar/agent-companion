"""The context line that flags a session for /clear: read it, set it. A
router of its own, so a host that keeps its own sessions view can mount
just these two."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import clearline as line
from . import auth

router = APIRouter()
NO_STORE = {"Cache-Control": "no-store"}


@router.get("/api/clearline", dependencies=[Depends(auth.require_token)])
def get_clear_line():
    return JSONResponse({"tokens": line.get()}, headers=NO_STORE)


@router.post("/api/clearline", dependencies=[Depends(auth.require_write)])
async def set_clear_line(request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    tokens = body.get("tokens") if isinstance(body, dict) else None
    try:
        saved = line.put(tokens)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except OSError as exc:
        return JSONResponse({"tokens": line.get(), "detail": f"could not save it: {exc}"}, status_code=500)
    return JSONResponse({"tokens": saved}, headers=NO_STORE)
