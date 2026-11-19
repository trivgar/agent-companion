"""Optional phone-driver relay routes.

The Android driver and its local tools authenticate with the server's write
token. Dashboard reads and the irreversible pairing choices require the
paired browser's owner key as well.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .. import fcm
from ..phone import drive
from ..phone import settings as phone_settings
from . import auth

TITLE = "Phone"
router = APIRouter()
NOSTORE = {"Cache-Control": "no-store"}
relay = drive.Relay(push=lambda device, job: fcm.send_drive(str(device.get("fcm") or ""), job))


def _key(request: Request) -> str:
    return request.headers.get(drive.KEY_HEADER, "")


def _device_secret(request: Request) -> str:
    return request.headers.get(drive.DEVICE_SECRET_HEADER, "")


def _job_cap(request: Request) -> str:
    return request.headers.get(drive.JOB_CAP_HEADER, "")


def _require_json(request: Request) -> None:
    if not auth.is_json(request.headers.get("content-type")):
        raise HTTPException(415, "writes must be application/json")


def _device_reply(out: dict) -> JSONResponse:
    if out.get("code") == "bad_device_secret":
        raise HTTPException(403, out["text"])
    return JSONResponse(out, headers=NOSTORE)


def _real_phone_allowed() -> None:
    if not phone_settings.real_phone_enabled():
        raise HTTPException(403, "the real phone driver is off; set phone_driver_real_phone = true")


@router.post("/api/phone/jobs", dependencies=[Depends(auth.require_hook), Depends(_require_json)])
async def start(request: Request, body: dict = Body(...)) -> JSONResponse:
    device = str(body.get("device") or "emulator")
    if device == "phone":
        _real_phone_allowed()
    out = await relay.start(device, str(body.get("task") or ""), str(body.get("agent") or ""), key=_key(request))
    return JSONResponse(out, headers=NOSTORE)


@router.post("/api/phone/jobs/{job_id}/cmd", dependencies=[Depends(auth.require_hook), Depends(_require_json)])
async def command(request: Request, job_id: str, body: dict = Body(...)) -> JSONResponse:
    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    out = await relay.command(job_id, str(body.get("cmd_id") or ""), str(body.get("verb") or ""), args,
                              key=_key(request), cap=_job_cap(request))
    return JSONResponse(out, headers=NOSTORE)


@router.post("/api/phone/jobs/{job_id}/end", dependencies=[Depends(auth.require_hook), Depends(_require_json)])
async def end(request: Request, job_id: str, body: dict = Body(default={})) -> JSONResponse:
    return JSONResponse(relay.end(job_id, key=_key(request), cap=_job_cap(request)), headers=NOSTORE)


@router.post("/api/phone/device", dependencies=[Depends(auth.require_hook), Depends(_require_json)])
async def device(request: Request, body: dict = Body(...)) -> JSONResponse:
    if body.get("kind") == "phone":
        _real_phone_allowed()
    return _device_reply(relay.register(body, secret=_device_secret(request)))


@router.post("/api/phone/devices/{device_id}/approve",
             dependencies=[Depends(auth.require_write)])
async def approve(request: Request, device_id: str, body: dict = Body(default={})) -> JSONResponse:
    _real_phone_allowed()
    if not auth.same_origin(request.headers):
        raise HTTPException(403, "only from this dashboard page")
    out = relay.approve_device(device_id)
    if not out["ok"]:
        raise HTTPException(409, out["text"])
    return JSONResponse(out, headers=NOSTORE)


@router.post("/api/phone/devices/{device_id}/forget",
             dependencies=[Depends(auth.require_write)])
async def forget(request: Request, device_id: str, body: dict = Body(default={})) -> JSONResponse:
    if not auth.same_origin(request.headers):
        raise HTTPException(403, "only from this dashboard page")
    out = relay.forget_device(device_id)
    if not out["ok"]:
        raise HTTPException(409, out["text"])
    return JSONResponse(out, headers=NOSTORE)


@router.post("/api/phone/next", dependencies=[Depends(auth.require_hook), Depends(_require_json)])
async def next_command(request: Request, body: dict = Body(...)) -> JSONResponse:
    device_id = str(body.get("device_id") or "")
    rec = relay.devices().get("devices", {}).get(device_id)
    if isinstance(rec, dict) and rec.get("kind") == "phone" and not phone_settings.real_phone_enabled():
        return JSONResponse({"type": "idle"}, headers=NOSTORE)
    hung_up = asyncio.ensure_future(request.receive())
    try:
        out = await relay.next(device_id, gone=hung_up.done, holds=str(body.get("job") or ""),
                               running=str(body.get("running") or ""), secret=_device_secret(request))
    finally:
        hung_up.cancel()
    return _device_reply(out)


@router.post("/api/phone/result", dependencies=[Depends(auth.require_hook), Depends(_require_json)])
async def result(request: Request, body: dict = Body(...)) -> JSONResponse:
    return _device_reply(relay.result(body, secret=_device_secret(request)))


@router.post("/api/phone/event", dependencies=[Depends(auth.require_hook), Depends(_require_json)])
async def event(request: Request, body: dict = Body(...)) -> JSONResponse:
    return _device_reply(relay.event(body, secret=_device_secret(request)))


@router.get("/api/phone/jobs", dependencies=[Depends(auth.require_token)])
async def jobs() -> JSONResponse:
    return JSONResponse(relay.listing(), headers=NOSTORE)


@router.get("/api/phone/jobs/{job_id}", dependencies=[Depends(auth.require_token)])
async def job(job_id: str) -> JSONResponse:
    got = relay.detail(job_id)
    if got is None:
        raise HTTPException(404, "no such job")
    return JSONResponse(got, headers=NOSTORE)


@router.get("/api/phone/jobs/{job_id}/shot", dependencies=[Depends(auth.require_token)])
async def shot(job_id: str) -> FileResponse:
    path = relay.shot_path(job_id)
    if path is None:
        raise HTTPException(404, "no screenshot")
    return FileResponse(path, media_type="image/png", headers=NOSTORE)


@router.post("/api/phone/jobs/{job_id}/stop", dependencies=[Depends(auth.require_write)])
async def stop(request: Request, job_id: str, body: dict = Body(default={})) -> JSONResponse:
    if not auth.same_origin(request.headers):
        raise HTTPException(403, "only from this dashboard page")
    return JSONResponse(relay.stop(job_id), headers=NOSTORE)
