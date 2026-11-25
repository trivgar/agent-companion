"""Routes for agent-published, sandboxed HTML pages."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .. import addons, config, pages as pages_mod
from . import auth, push


TITLE = "Pages"
router = APIRouter()


def _store() -> pages_mod.Store:
    """Late-bind the configured directory so tests never use a live store."""
    return pages_mod.Store()


def _viewer(request: Request) -> str | None:
    if auth.is_owner(request):
        return pages_mod.OWNER_ID
    person = addons.person(request) or {}
    person_id = str(person.get("id") or "").strip()
    # An add-on cannot turn a shared identity into the owner.
    return person_id if person_id and person_id != pages_mod.OWNER_ID else None


def _push(owner: str, title: str, body: str, url: str, tag: str) -> int:
    """The publisher is the owner, so use the core owner PushStore."""
    del owner
    return push.store().send(title, body, url=url, tag=tag, actions=[])


@router.post("/api/pages", dependencies=[Depends(auth.require_hook)])
def publish(body: dict = Body(...)):
    body = body if isinstance(body, dict) else {}
    try:
        record, created = _store().publish(
            html=body.get("html"), title=body.get("title"), folder=body.get("folder", body.get("cwd")),
            source=body.get("source"), speaker=body.get("speaker") or "an agent",
            project=body.get("project") or "", household=bool(body.get("household")), pusher=_push,
        )
    except pages_mod.Refused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return JSONResponse({"ok": True, "page": pages_mod.page_summary(record), "created": created,
                         "url": f"/?view=pages&page={record['id']}"})


@router.get("/api/pages", dependencies=[Depends(auth.require_person)])
def list_pages(request: Request):
    return JSONResponse({"pages": _store().list_for(_viewer(request))}, headers={"Cache-Control": "no-store"})


@router.get("/pages/{page_id}", dependencies=[Depends(auth.require_person)])
def html(page_id: str, request: Request):
    store = _store()
    record = store.get(page_id)
    if record is None or not store.can_view(record, _viewer(request)):
        raise HTTPException(status_code=404, detail="page not found")
    if pages_mod.unsealed_app(request.headers.get("user-agent"), config.get("app_name")):
        return Response(pages_mod.NOTICE_HTML, status_code=403, headers=pages_mod.NOTICE_HEADERS)
    content = store.html(page_id)
    if content is None:
        raise HTTPException(status_code=404, detail="page not found")
    return Response(content=content, headers=pages_mod.PAGE_HEADERS)


@router.delete("/api/pages/{page_id}", dependencies=[Depends(auth.require_person_write)])
def delete(page_id: str, request: Request):
    if not _store().delete(page_id, _viewer(request)):
        raise HTTPException(status_code=404, detail="page not found")
    return {"ok": True}
