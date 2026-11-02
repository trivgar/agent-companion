"""The dashboard server: the library's rows, asks, chat, mail and comms on one
phone-first page. Binds to loopback; put `tailscale serve` in front for a phone.

    python -m agent_companion.server               # serve on 127.0.0.1:8770
    python -m agent_companion.server pair [--name phone]   # a one-time link for a device
    python -m agent_companion.server unpair <name>         # take a device off
    python -m agent_companion.server devices               # who is paired
"""
from __future__ import annotations

import importlib
import argparse
import html
import json as _json
import logging
import re
import sys
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .. import config, jobreaper, restart
from ..clearline import Watcher as ClearLineWatcher  # by name: .clearline is the router
from ..webpush import PushStore
from . import auth, restore, sessions
from . import auth, newsession, sessions
from . import livecall
from . import clearline
from .features import FEATURES
from .gzip import GzipTextMiddleware

STATIC = Path(__file__).resolve().parent / "static"
log = logging.getLogger("agent_companion.server")
_SPRITE_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def app_name() -> str:
    return str(config.get("app_name", "") or "").strip() or "Companion"


def _sprite_overrides() -> dict[str, str]:
    """The small, safe subset of config needed by the public page shell."""
    value = config.get("sprite_overrides", {})
    if not isinstance(value, dict):
        return {}
    return {str(name): color for name, color in value.items()
            if isinstance(name, str) and isinstance(color, str) and _SPRITE_COLOR.fullmatch(color)}


def _identity_config() -> str:
    # This is embedded in a script tag, so JSON's normal output alone is not
    # enough: protect the HTML parser from a configured display name too.
    value = _json.dumps(_sprite_overrides(), ensure_ascii=False)
    return "window.CC_IDENTITY_OVERRIDES=" + value.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026") + ";"


def _named(path: Path, live_call: bool = False) -> str:
    return (path.read_text()
            .replace("{{APP_NAME}}", html.escape(app_name()))
            .replace("{{IDENTITY_CONFIG}}", _identity_config())
            .replace("{{LIVE_CALL_BUTTON}}", '<span id="call"></span>' if live_call else "")
            .replace("{{LIVE_CALL_SCRIPT}}", '<script src="/static/call.js"></script>' if live_call else ""))


def _enabled() -> list[str]:
    want = config.get("features")
    if isinstance(want, str):
        want = [w.strip() for w in want.split(",") if w.strip()]
    return [f for f in FEATURES if not want or f in want]


def live_call_enabled() -> bool:
    """A configured key-file path opts in to the Live Call UI and routes.

    This deliberately checks only for configuration. The route reads the file
    when a caller starts a call and gives a clean refusal if it has gone away.
    """
    return bool(str(config.get("gemini_key_file", "") or "").strip())


def _clear_rows() -> list[dict]:
    try:
        return sessions.order(sessions._parts()["reader"].snapshot())
    except Exception:
        return []


def _send_clear_push(title: str, body: str, url: str, tag: str) -> bool:
    return PushStore(sessions.data_dir()).send(title, body, url=url, tag=tag, actions=[]) > 0


def create_app(token: str | None = None, owner_key: str | None = None) -> FastAPI:
    """token: the hook's. owner_key: a key accepted without a file (tests)."""
    app = FastAPI(title="agent-companion", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(GzipTextMiddleware)
    app.state.token = auth.load_or_create_token() if token is None else token
    app.state.owner_hashes = frozenset([auth.hash_secret(owner_key)] if owner_key else [])
    app.state.pair_failures = []
    app.state.restart_check = restart.Check()
    app.include_router(restore.router)
    app.include_router(newsession.router)
    app.include_router(clearline.router)

    @app.middleware("http")
    async def host_check(request: Request, call_next):
        if not auth.host_allowed(request.headers.get("host") or ""):
            return PlainTextResponse("unknown host", status_code=400)
        return await call_next(request)
    app.state.live_call_enabled = live_call_enabled()
    # The Call button is part of the page shell rather than a tab. It is an
    # explicit opt-in because a Live Call sends audio and selected session text
    # to Gemini. With no configured key file, neither the UI nor routes exist.
    if app.state.live_call_enabled:
        app.include_router(livecall.router)
        if "pytest" not in sys.modules:
            livecall.start(app)
    app.state.features = []
    for name in _enabled():
        try:
            mod = importlib.import_module(f"{__name__}.{name}")
        except ModuleNotFoundError as exc:
            if exc.name == f"{__name__}.{name}":
                log.info("feature %s: not installed, skipped", name)
                continue
            raise
        app.include_router(mod.router)
        app.state.features.append({"id": name, "title": getattr(mod, "TITLE", name.title())})
        start = getattr(mod, "start", None)
        if start and "pytest" not in sys.modules:
            start(app)
    if "pytest" not in sys.modules:
        ClearLineWatcher(_clear_rows, _send_clear_push).start()
        jobreaper.start(sessions.data_dir())

    @app.get("/api/features")
    def features():
        return app.state.features

    @app.get("/api/needs-restart", dependencies=[Depends(auth.require_token)])
    def needs_restart():
        return {"needed": app.state.restart_check.needed(), "command": restart.RESTART_COMMAND}

    @app.get("/")
    def page():
        return HTMLResponse(_named(STATIC / "index.html", app.state.live_call_enabled),
                            headers={"Cache-Control": "no-cache"})

    @app.get("/pair")
    def pair_page():
        return HTMLResponse(_named(STATIC / "pair.html"), headers={"Cache-Control": "no-store"})

    @app.get("/manifest.json")
    def manifest():
        m = _json.loads((STATIC / "manifest.json").read_text())
        m["name"] = m["short_name"] = app_name()
        return JSONResponse(m, headers={"Cache-Control": "no-cache"})

    @app.post("/api/pair")
    async def pair(request: Request):
        """Trade a one-time code for an owner key. The key is in this one
        response and nowhere on the server in plain text."""
        if not auth.is_json(request.headers.get("content-type")) or not auth.same_origin(request.headers):
            raise HTTPException(403, "pairing comes from this page only")
        try:
            body = await request.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        now = time.time()
        fails = [t for t in app.state.pair_failures if now - t < auth.CODE_LIFE_S]
        app.state.pair_failures = fails
        if len(fails) >= auth.MAX_FAILED_PAIRS:
            raise HTTPException(429, "too many wrong codes: wait, then make a new link")
        key = auth.redeem_pairing_code(str(body.get("code") or ""))
        if not key:
            fails.append(now)
            raise HTTPException(403, "that code is wrong, used or expired: make a new link")
        return {"ok": True, "key": key}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def _base_url() -> str:
    bind = str(config.get("server_bind", "127.0.0.1"))
    port = int(config.get("server_port", 8770))
    return str(config.get("url") or f"http://{bind}:{port}").rstrip("/")


def _pair(name: str) -> int:
    code = auth.make_pairing_code(name)
    print(f"Open this on the device within {auth.CODE_LIFE_S // 60} minutes; it works once:")
    print(f"  {_base_url()}/pair?code={code}")
    if not config.get("url"):
        print("(Set url in the config to your tailscale serve address to print one a phone can open.)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agent_companion.server")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("pair", help="print a one-time link that pairs a device")
    p.add_argument("--name", default="phone")
    u = sub.add_parser("unpair", help="remove a paired device")
    u.add_argument("name")
    sub.add_parser("devices", help="list paired devices")
    args = ap.parse_args(argv)
    if args.cmd == "pair":
        return _pair(args.name)
    if args.cmd == "unpair":
        ok = auth.remove_device(args.name)
        print(f"{args.name}: {'unpaired' if ok else 'no such device'}")
        return 0 if ok else 1
    if args.cmd == "devices":
        for d in auth.devices():
            print(f"{d['name']}\tpaired {time.strftime('%Y-%m-%d %H:%M', time.localtime(d['created'] or 0))}")
        return 0

    import uvicorn
    port = int(config.get("server_port", 8770))
    bind = str(config.get("server_bind", "127.0.0.1"))
    app = create_app()
    logging.getLogger("uvicorn.access").addFilter(auth.RedactKey())
    paired = len(auth.devices())
    if config.problem():
        print(f"WARNING: {config.problem()}", file=sys.stderr)
    print(f"agent-companion on http://{bind}:{port}/ ({paired} device{'s' if paired != 1 else ''} paired; "
          "run `python -m agent_companion.server pair` to add one)", flush=True)
    uvicorn.run(app, host=bind, port=port, log_level="info")
    return 0
