"""Shared phone-job details for the CLI and MCP server.

The emulator lease is deliberately small and local to this optional feature.
It prevents two local callers from treating the test emulator as theirs at
once; a real phone is always controlled through the relay's per-job
capability.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import drive

EMULATOR_LEASE_S = 30 * 60 + 300


class _Leases:
    def _path(self) -> Path:
        return drive.DATA / "emulator-lease.json"

    def live(self, device: str) -> dict | None:
        try:
            got = json.loads(self._path().read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(got, dict) or got.get("device") != device or float(got.get("until") or 0) <= time.time():
            return None
        return got

    def take(self, device: str, holder: str, seconds: float, *, why: str = "", session: str = "") -> dict:
        old = self.live(device)
        if old and (old.get("holder") != holder or old.get("session") != session):
            return {"ok": False, "detail": f"{device} is held by {old.get('holder') or 'another caller'}"}
        rec = {"device": device, "holder": holder, "why": why, "session": session,
               "until": time.time() + seconds}
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return {"ok": True, "new": old is None, **rec}

    def release(self, device: str, holder: str, *, session: str = "") -> dict:
        old = self.live(device)
        if old and old.get("holder") == holder and old.get("session") == session:
            try:
                self._path().unlink()
            except OSError:
                pass
            return {"ok": True}
        return {"ok": False, "detail": "lease is not held by this caller"}


_file_leases = _Leases()
_lease_backend = _file_leases


class _LeaseProxy:
    """Keep imported ``leases`` references pointed at the selected backend."""
    def live(self, device: str) -> dict | None:
        return _lease_backend.live(device)

    def take(self, device: str, holder: str, seconds: float, *, why: str = "", session: str = "") -> dict:
        return _lease_backend.take(device, holder, seconds, why=why, session=session)

    def release(self, device: str, holder: str, *, session: str = "") -> dict:
        return _lease_backend.release(device, holder, session=session)


leases = _LeaseProxy()


def set_lease_backend(backend) -> None:
    """Use a host lease backend; None restores the built-in file lease."""
    global _lease_backend
    _lease_backend = _file_leases if backend is None else backend


def session_id() -> str:
    return os.environ.get("AGENT_COMPANION_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID") or ""


def take_emulator_lease(holder: str, why: str, *, lease_api=leases, session: str | None = None) -> dict:
    return lease_api.take("emulator", holder, EMULATOR_LEASE_S, why=why,
                          session=session_id() if session is None else session)


def release_emulator_lease(holder: str, *, lease_api=leases, session: str | None = None) -> dict:
    return lease_api.release("emulator", holder, session=session_id() if session is None else session)


def release_if_new(got: dict | None, holder: str, *, lease_api=leases, session: str | None = None) -> None:
    if got and got.get("new"):
        release_emulator_lease(holder, lease_api=lease_api, session=session)


def touch_emulator() -> None:
    from . import emu
    emu.touch_used()


def point(text: str) -> tuple[int, int] | None:
    parts = str(text).split(",")
    if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1])


def target(text: str) -> dict:
    xy = point(text)
    if xy:
        return {"x": xy[0], "y": xy[1]}
    if str(text).isdigit():
        return {"n": int(text)}
    raise ValueError(f"{text!r} is neither a number from the last look nor X,Y")


def result_text(got: dict, *, include_shot: bool = True) -> str:
    text = str(got.get("text") or "")
    if include_shot and got.get("shot"):
        text = (text + "\n" if text else "") + f"screenshot: {got['shot']}"
    code = str(got.get("code") or "")
    if not got.get("ok") and code and f"[{code}]" not in text:
        text = f"{text} [{code}]".strip()
    return text


def status_lines(got: dict) -> list[str]:
    lines = []
    for job in got.get("live") or []:
        line = f"live: {job['id']} on the {job['device']}, {job['agent']}: {job['task']} ({job['state']}: {job['detail']})"
        if job.get("last_action"):
            line += f"; last: {job['last_action']}"
        lines.append(line)
    for job in (got.get("history") or [])[:5]:
        lines.append(f"ended: {job['id']} on the {job['device']}, {job['agent']}: {job['task']} ({job['outcome']}: {job['detail']})")
    for kind, device in (got.get("devices") or {}).items():
        lines.append(f"{kind}: {device.get('model') or '?'}, Android {device.get('android') or '?'}, driver "
                     + ("on" if device.get("driver_on") else "off"))
    return lines or ["no jobs and no devices yet"]
