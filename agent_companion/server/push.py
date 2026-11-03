"""Web push: the page's public key, a device subscribing, and a test push."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..webpush import PushStore, push_dir
from . import auth

TITLE = "Push"
router = APIRouter()
# A host app with a store of its own (one that delivers somewhere first, or keeps
# a feed of what it sent) sets it here, so these routes and every push the core
# sends use that one store. None: a PushStore in push_dir.
_store = None


def store() -> PushStore:
    return _store if _store is not None else PushStore(push_dir())


@router.get("/api/push/key", dependencies=[Depends(auth.require_token)])
def key():
    s = store()
    return {"key": s.public_key(), "devices": s.devices()}


@router.post("/api/push/subscribe", dependencies=[Depends(auth.require_subscribe)])
async def subscribe(request: Request):
    try:
        sub = await request.json()
    except ValueError:
        sub = None
    s = store()
    if not s.subscribe(sub):
        raise HTTPException(400, "not a push subscription")
    return {"ok": True, "devices": s.devices()}


@router.post("/api/push/test", dependencies=[Depends(auth.require_write)])
def test():
    n = store().send("Companion", "Notifications are on.", url="/", tag="push-test")
    return {"ok": True, "sent": n}
