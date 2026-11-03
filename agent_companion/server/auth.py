"""Who may write.

Two credentials, never interchangeable.

The HOOK token (agent-companion-token, mode 600) is read by the permission
hook on this machine. It opens the hook's own routes only: create, poll and
withdraw an ask. Any process on the account can read that file, so it must
never be enough to decide anything.

An OWNER key decides: approving, answering, chat, mail, push, reads of private
state. The server keeps only a SHA-256 hash of each key, in owner-keys.json
(mode 600), one entry per paired device. A key is made by pairing: the owner
runs `python -m agent_companion.server pair` on the computer, opens the link
it prints on the device, and the page trades the one-time code for a random
key that lives in that browser's localStorage and is sent as a header. The key
is never in a URL and never on disk in plain text.

Writes also arrive as JSON and from this page's own origin. The JSON rule is
the anti-CSRF wall: a hostile page in the owner's browser can POST a form here
without permission, but it cannot send Content-Type: application/json without
a preflight the browser refuses. The Origin check covers the rest. Neither
header proves anything against a process on this machine (curl can send
both); the owner key is what does.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path

from fastapi import HTTPException, Request

from .. import config

TOKEN_FILENAME = "agent-companion-token"
OWNER_KEYS_FILENAME = "owner-keys.json"
PAIRING_FILENAME = "pairing-codes.json"
QUERY_KEY = "k"                      # only the one-time pairing code rides in a URL now
HEADER_NAME = "x-agent-companion-token"     # the hook's token
OWNER_HEADER = "x-agent-companion-key"      # the owner's key
# Before the rename (2026-11-01) these were x-claude-companion-*; a phone app
# built before it still sends them, so both are heard until it is rebuilt.
LEGACY_HEADER_NAME = "x-claude-companion-token"
LEGACY_OWNER_HEADER = "x-claude-companion-key"
JSON_TYPE = "application/json"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LEN = 8
CODE_LIFE_S = 600
MAX_FAILED_PAIRS = 10                # per CODE_LIFE_S, across all callers

_KEY_IN_URL = re.compile(r"([?&](?:" + re.escape(QUERY_KEY) + r"|code)=)[^&#\s\"]*")


def redact_key(text: str) -> str:
    return _KEY_IN_URL.sub(r"\1REDACTED", text)


class RedactKey(logging.Filter):
    """Keeps a pairing code out of uvicorn's access log."""
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact_key(a) if isinstance(a, str) else a for a in record.args)
        return True


def _config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "agent-companion"


def token_path() -> Path:
    return Path(str(config.get("hook_token_file") or _config_dir() / TOKEN_FILENAME)).expanduser()


def owner_keys_path() -> Path:
    return Path(str(config.env("OWNER_KEYS") or config.get("owner_keys_file")
                    or _config_dir() / OWNER_KEYS_FILENAME)).expanduser()


def pairing_path() -> Path:
    return Path(str(config.env("PAIRING") or config.get("pairing_file")
                    or _config_dir() / PAIRING_FILENAME)).expanduser()


def load_or_create_token(path: Path | None = None) -> str:
    """The hook token, made on first run (mode 600)."""
    path = path or token_path()
    try:
        tok = path.read_text().strip()
        if tok:
            return tok
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok + "\n")
    return tok


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# --- small 600 JSON lists (owner keys, pairing codes), locked while changed ---

def _read_list(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _update(path: Path, fn):
    """Read, change with fn(list) -> (list, result), write back atomically,
    all under one lock so two pairings never lose each other."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = os.open(str(path) + ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        items, result = fn(_read_list(path))
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(items, fh, indent=1)
        os.replace(tmp, path)
        return result
    finally:
        os.close(lock)


def devices(path: Path | None = None) -> list[dict]:
    """Paired devices: name and created only, never a hash."""
    return [{"name": e.get("name"), "created": e.get("created")} for e in _read_list(path or owner_keys_path())]


def add_device(name: str, key: str, path: Path | None = None, now: float | None = None) -> None:
    base = (name or "device").strip()[:40] or "device"

    def fn(items):
        # a second device with a taken name is paired as name-2, never a silent replace
        taken = {e.get("name") for e in items}
        n, i = base, 2
        while n in taken:
            n, i = f"{base[:36]}-{i}", i + 1
        entry = {"name": n, "hash": hash_secret(key), "created": time.time() if now is None else now}
        return items + [entry], None
    _update(path or owner_keys_path(), fn)


def remove_device(name: str, path: Path | None = None) -> bool:
    def fn(items):
        keep = [e for e in items if e.get("name") != name]
        return keep, len(keep) != len(items)
    return _update(path or owner_keys_path(), fn)


def is_owner_key(key: str, path: Path | None = None, extra: frozenset | set = frozenset()) -> bool:
    """Hash compare against every paired device, in constant time per entry."""
    if not key:
        return False
    h = hash_secret(key).encode()
    ok = False
    for want in [e.get("hash") for e in _read_list(path or owner_keys_path())] + list(extra):
        if isinstance(want, str) and secrets.compare_digest(h, want.encode()):
            ok = True
    return ok


def make_pairing_code(name: str = "device", path: Path | None = None, now: float | None = None) -> str:
    now = time.time() if now is None else now
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    entry = {"hash": hash_secret(code), "name": (name or "device").strip()[:40] or "device", "expires": now + CODE_LIFE_S}
    _update(path or pairing_path(), lambda items: ([e for e in items if float(e.get("expires") or 0) > now] + [entry], None))
    return code


def redeem_pairing_code(code: str, keys: Path | None = None, path: Path | None = None,
                        now: float | None = None) -> str | None:
    """Trade a live code for a new owner key, once. None when the code is
    wrong, used or expired. The key is returned here and kept nowhere."""
    now = time.time() if now is None else now
    norm = "".join(str(code or "").split()).upper()
    if len(norm) != CODE_LEN:
        return None
    h = hash_secret(norm)

    def fn(items):
        live = [e for e in items if float(e.get("expires") or 0) > now]
        hit = next((e for e in live if secrets.compare_digest(str(e.get("hash")), h)), None)
        return [e for e in live if e is not hit], hit
    hit = _update(path or pairing_path(), fn)
    if not hit:
        return None
    key = secrets.token_hex(32)
    add_device(hit.get("name") or "device", key, keys, now)
    return key


def is_json(content_type: str | None) -> bool:
    return (content_type or "").split(";")[0].strip().lower() == JSON_TYPE


def _hostname(host: str) -> str:
    """The name in a Host header, port off: "[::1]:8770" -> "::1"."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def host_allowed(host: str) -> bool:
    """Loopback names, the host of the configured `url`, and any `allowed_hosts`
    (a list, or comma-separated). Anything else is a DNS-rebinding or
    mis-pointed request and gets 400."""
    from urllib.parse import urlsplit
    from .. import config
    ok = {"127.0.0.1", "localhost", "::1"}
    ok.add((urlsplit(str(config.get("url") or "")).hostname or "").lower())
    extra = config.get("allowed_hosts") or ()
    if isinstance(extra, str):
        extra = extra.split(",")
    ok.update(str(h).strip().lower() for h in extra)
    ok.discard("")
    return _hostname(host) in ok


def same_origin(headers) -> bool:
    """An Origin header naming this page's own host. Sec-Fetch-Site alone is
    not enough: any process can send it. Behind tailscale serve the page's host
    is X-Forwarded-Host."""
    origin = headers.get("origin") or ""
    host = headers.get("x-forwarded-host") or headers.get("host") or ""
    return bool(origin and host) and origin.split("://", 1)[-1].rstrip("/").lower() == host.lower()


def _hook_presented(request: Request) -> str:
    return (request.headers.get(HEADER_NAME) or request.headers.get(LEGACY_HEADER_NAME) or "").strip()


def _owner_presented(request: Request) -> str:
    return (request.headers.get(OWNER_HEADER) or request.headers.get(LEGACY_OWNER_HEADER) or "").strip()


def require_hook(request: Request) -> None:
    """The permission hook's own routes. An owner key does not open them."""
    want = getattr(request.app.state, "token", "") or ""
    if not want or not secrets.compare_digest(_hook_presented(request).encode(), want.encode()):
        raise HTTPException(401, "missing or invalid hook token")


def require_token(request: Request) -> None:
    """Owner routes that only read (private words, state): an owner key."""
    extra = getattr(request.app.state, "owner_hashes", frozenset())
    if not is_owner_key(_owner_presented(request), extra=extra):
        raise HTTPException(401, "missing or invalid key: pair this device")


def is_owner(request: Request) -> bool:
    """Whether this request has an owner key, without raising."""
    extra = getattr(request.app.state, "owner_hashes", frozenset())
    return is_owner_key(_owner_presented(request), extra=extra)


def require_person(request: Request) -> None:
    """Routes the owner shares: an owner key, or someone the addons module
    names. Never used on an owner route, so a plug-in cannot widen one."""
    if is_owner(request):
        return
    from agent_companion import addons
    if addons.person(request) is None:
        raise HTTPException(401, "missing or invalid key: pair this device")


def require_write(request: Request) -> None:
    if not is_json(request.headers.get("content-type")):
        raise HTTPException(415, "writes must be application/json")
    require_token(request)
    if not same_origin(request.headers):
        raise HTTPException(403, "writes come from this page only")


def require_subscribe(request: Request) -> None:
    """A device subscribing to push: require_write. Its own guard, so a host app
    whose installed page cannot carry the token can stand a check of its own in.
    That check must keep the JSON rule: the route reads the body whatever its
    type, and a form or text/plain post needs no preflight, so without it any
    page open in the browser could subscribe a device of its own to every push."""
    require_write(request)


def require_person_write(request: Request) -> None:
    """A shared person's write, with the same JSON and origin walls."""
    if not is_json(request.headers.get("content-type")):
        raise HTTPException(415, "writes must be application/json")
    require_person(request)
    if not same_origin(request.headers):
        raise HTTPException(403, "writes come from this page only")
