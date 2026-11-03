"""Owner-only routes for the specs and plans agents write (agent_companion.specs).

Read only. The list says who is live in each repository, so a reply can open
that agent's chat; the live sessions are the chat's own (chat.known()), so a
host app that hides some sessions from the chat hides them here too. Each new
spec is pushed once (specs.Watcher), outside tests.
"""
from __future__ import annotations

import sys

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from .. import specs
from . import auth, chat, push

TITLE = "Specs"
router = APIRouter()
_NOSTORE = {"Cache-Control": "no-store"}


def live_by_repo() -> dict[str, list[str]]:
    try:
        known = chat.known()
    except Exception:
        known = {}
    return specs.live_by_repo([(t["name"], t["cwd"]) for t in known.values()])


def _send(title: str, body: str, url: str, tag: str) -> bool:
    return push.store().send(title, body, url=url, tag=tag, actions=[]) > 0


def start(app) -> None:
    """The spec push, through this app's Web Push, when the server and not pytest starts."""
    if "pytest" in sys.modules:
        return
    specs.Watcher(_send, live_by_repo).start()


@router.get("/api/specs", dependencies=[Depends(auth.require_token)])
def get_specs():
    return JSONResponse({"ok": True, "docs": specs.all_docs(), "agents": live_by_repo()}, headers=_NOSTORE)


@router.get("/api/specs/doc", dependencies=[Depends(auth.require_token)])
def get_doc(id: str = ""):
    d = specs.read_id(id)
    if d.get("ok"):
        repo = next((x.get("repo") for x in specs.all_docs() if x["id"] == d["id"]), d["project"])
        d["agents"] = live_by_repo().get(repo, [])
    return JSONResponse(d, headers=_NOSTORE)
