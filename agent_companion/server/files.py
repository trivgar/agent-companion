"""Owner-only routes for the checkouts as files (agent_companion.files).

What git lists and nothing else; a save is the owner's write, from this page,
and never a push. Live sessions are the chat's own (chat.known()), as on the
Specs page.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .. import files
from . import auth, chat

TITLE = "Files"
router = APIRouter()
_NOSTORE = {"Cache-Control": "no-store"}


def _known() -> dict:
    try:
        return chat.known()
    except Exception:
        return {}


@router.get("/api/files", dependencies=[Depends(auth.require_token)])
def get_repos():
    agents: dict[str, list[str]] = {}
    head = {c["name"]: r["repo"] for r in files.repos() for c in r["checkouts"]}
    for t in _known().values():
        co = files.checkout_of(t["cwd"])
        if co in head:
            agents.setdefault(head[co], []).append(t["name"])
    return JSONResponse({"ok": True, "repos": files.repos(), "agents": {k: sorted(v) for k, v in agents.items()}},
                        headers=_NOSTORE)


@router.get("/api/files/tree", dependencies=[Depends(auth.require_token)])
def get_tree(checkout: str = "", path: str = ""):
    return JSONResponse(files.tree(checkout, path), headers=_NOSTORE)


@router.get("/api/files/paths", dependencies=[Depends(auth.require_token)])
def get_paths(checkout: str = ""):
    return JSONResponse(files.all_paths(checkout), headers=_NOSTORE)


@router.get("/api/files/file", dependencies=[Depends(auth.require_token)])
def get_file(checkout: str = "", path: str = ""):
    return JSONResponse(files.read(checkout, path), headers=_NOSTORE)


@router.get("/api/files/raw", dependencies=[Depends(auth.require_token)])
def get_raw(checkout: str = "", path: str = ""):
    got = files.raw(checkout, path)
    if not got:
        raise HTTPException(status_code=404, detail="No such image.")
    # An SVG opened on its own could run script: sandboxed, nothing loads.
    return FileResponse(got[0], media_type=got[1], headers={**_NOSTORE,
                        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
                        "X-Content-Type-Options": "nosniff"})


@router.get("/api/files/where", dependencies=[Depends(auth.require_token)])
def get_where(agent: str = "", path: str = ""):
    """Where a path in an agent's chat lives: an absolute path names its own
    checkout (and comes back relative to it); else the agent's checkout,
    and the path relative to the checkout from where the agent stands."""
    if path.startswith("/"):
        name = files.checkout_of(path)
        rel = files.relative(name, path) if name else None
        return JSONResponse({"ok": bool(name), "checkout": name or "", "path": rel}, headers=_NOSTORE)
    t = _known().get(agent.lower())
    name = files.checkout_of(t["cwd"]) if t else ""
    rel = files.relative(name, str(Path(t["cwd"]) / path)) if name and path else None
    if name and path and rel is not None and rel != path and rel not in files.listing(files.checkout(name)):
        rel = path          # agents mostly name paths from the repo's root, wherever they stand
    return JSONResponse({"ok": bool(name), "checkout": name or "", "path": rel}, headers=_NOSTORE)


@router.post("/api/files/save", dependencies=[Depends(auth.require_write)])
def save_file(request: Request, body: dict = Body(...)):
    # The origin is checked here as well as in require_write: a host app may
    # stand its own write check in for that guard.
    if not auth.same_origin(request.headers):
        raise HTTPException(status_code=403, detail="writes come from this page only")
    for k in ("checkout", "path", "text", "expect"):
        if not isinstance(body.get(k), str):
            raise HTTPException(status_code=400, detail=f"{k} must be a string")
    return JSONResponse(files.save(body["checkout"], body["path"], body["text"], body["expect"],
                                   str(body.get("message") or ""), bool(body.get("commit", True))), headers=_NOSTORE)
