"""Firebase Cloud Messaging delivery for the optional Android ringer.

FCM receives only a ring id.  The phone fetches the ring's words from the
dashboard over its authenticated connection, so notification delivery never
carries ask text, audio, or a credential.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from . import config

_lock = threading.Lock()
_sessions: dict[str, tuple[object, str]] = {}


def state_path() -> Path:
    raw = config.get("fcm_device_file")
    if raw:
        return Path(str(raw)).expanduser()
    base = config.get("data_dir") or Path.home() / ".local" / "share" / "agent-companion"
    return Path(str(base)).expanduser() / "fcm-device.json"


def device() -> dict:
    """The Android registration, if an authenticated setup saved one."""
    try:
        data = json.loads(state_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_device(token: str, ring: bool = False) -> bool:
    """Save a validated FCM registration without ever logging the token."""
    token = str(token or "").strip()
    if not token or len(token) > 4096 or any(char.isspace() for char in token):
        return False
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_suffix(path.suffix + ".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump({"token": token, "ring": bool(ring)}, file)
        os.replace(path.with_suffix(path.suffix + ".tmp"), path)
    except OSError:
        try:
            path.with_suffix(path.suffix + ".tmp").unlink()
        except OSError:
            pass
        return False
    return True


def can_ring() -> bool:
    data = device()
    return bool(data.get("token") and data.get("ring") and config.get("fcm_service_account_file"))


def _authed() -> tuple[object, str] | None:
    """An authorized session and Firebase project, loaded only when configured."""
    raw = config.get("fcm_service_account_file")
    if not raw:
        return None
    path = str(Path(str(raw)).expanduser())
    with _lock:
        if path in _sessions:
            return _sessions[path]
        try:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account
            credentials = service_account.Credentials.from_service_account_file(
                path, scopes=["https://www.googleapis.com/auth/firebase.messaging"])
            project = str(credentials.project_id or "")
            if not project:
                return None
            got = (AuthorizedSession(credentials), project)
        except Exception:
            return None
        _sessions[path] = got
        return got


def send_ring(ring_id: str) -> bool:
    """Send a high-priority, one-minute FCM data message containing only an id."""
    data = device()
    token = str(data.get("token") or "")
    if not token or not data.get("ring") or not ring_id:
        return False
    client = _authed()
    if client is None:
        return False
    session, project = client
    message = {"message": {"token": token, "data": {"kind": "ring", "ring": str(ring_id)},
                           "android": {"priority": "HIGH", "ttl": "60s"}}}
    try:
        response = session.post(f"https://fcm.googleapis.com/v1/projects/{project}/messages:send",
                                json=message, timeout=10)
    except Exception:
        return False
    return bool(getattr(response, "status_code", 0) == 200)


def send_drive(token: str, job_id: str) -> str:
    """Send a phone-driver wakeup with only its kind and job identifier."""
    token = str(token or "")
    if not token:
        return "no token"
    client = _authed()
    if client is None:
        return "not configured"
    session, project = client
    message = {"message": {"token": token, "data": {"kind": "drive", "job": str(job_id)},
                           "android": {"priority": "HIGH", "ttl": "60s"}}}
    try:
        response = session.post(f"https://fcm.googleapis.com/v1/projects/{project}/messages:send",
                                json=message, timeout=10)
    except Exception:
        return "failed"
    if getattr(response, "status_code", 0) == 200:
        return "ok"
    if getattr(response, "status_code", 0) in (404, 410):
        return "gone"
    return f"http {getattr(response, 'status_code', 0)}"
