"""Owner-only view of this machine's local health facts."""
from __future__ import annotations

import sys

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from .. import health as core
from . import auth, push, sessions

TITLE = "Machine"
router = APIRouter()
reader = core.HealthReader()


@router.get("/api/health", dependencies=[Depends(auth.require_token)])
def health():
    return JSONResponse(reader.snapshot(), headers={"Cache-Control": "no-store"})


def _send(title: str, body: str, url: str, tag: str) -> bool:
    return push.store().send(title, body, url=url, tag=tag, actions=[]) > 0


def start(app) -> None:
    """Start optional local warning pushes outside tests, as other watchers do."""
    if "pytest" in sys.modules or not core.push_enabled():
        return
    watcher = core.Watcher(reader, _send, sessions.data_dir() / "health-alerts.json")
    watcher.start()
    app.state.health_watcher = watcher
