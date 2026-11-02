"""Plug points: the `addons` setting names a module of yours, and the core
calls it at a few points. Every function here has a default for when there is
no module, and fails safe when the module raises. A plug-in can narrow what
the core allows, never widen it. `on_ask` receives created, decided, answered
and withdrawn events; a created result {"pushed": True} replaces the core
push, and {"answerable": False} sends the question back to the terminal."""
from __future__ import annotations

import functools
import importlib
import logging
from pathlib import PurePath

from agent_companion import config

log = logging.getLogger("agent_companion.addons")


@functools.lru_cache(maxsize=1)
def load():
    name = str(config.get("addons", "") or "").strip()
    if not name:
        return None
    try:
        return importlib.import_module(name)
    except Exception as exc:  # a broken add-on must not take the server down
        log.warning("addons %s: not loaded (%s)", name, exc)
        return None


def _call(fn: str, default, on_error, *args):
    mod = load()
    f = getattr(mod, fn, None) if mod else None
    if f is None:
        return default
    try:
        return f(*args)
    except Exception as exc:
        log.warning("addons.%s raised %s", fn, exc)
        return on_error


def person(request) -> dict | None:
    """A shared person's identity. Mail uses `mail_account` (or `id`) to
    select that person's configured mailbox; the owner always uses `owner`."""
    got = _call("person", None, None, request)
    return got if isinstance(got, dict) else None


def on_ask(event: str, ask: dict) -> dict | None:
    got = _call("on_ask", None, None, event, ask)
    return got if isinstance(got, dict) else None


def asleep() -> bool:
    return bool(_call("asleep", False, True))


def comms_busy() -> bool:
    """Whether routine spoken clips should wait.

    Add-ons may put comms on hold for local context such as a meeting.  A
    missing add-on never holds clips.  A broken add-on does hold them, because
    speaking unexpectedly is the less safe failure mode.
    """
    return bool(_call("comms_busy", False, True))


def private(cwd: str, project: str) -> bool:
    listed = {str(p) for p in (config.get("private_projects") or [])}
    if project in listed or any(part in listed for part in PurePath(cwd or "").parts):
        return True
    return bool(_call("private", False, True, cwd, project))


def chat_target(target: dict) -> bool:
    """Whether an already-known session is available in direct chat.

    This can only remove a target from the core's own list. A broken add-on
    removes it too, so a policy failure never exposes a session.
    """
    return _call("chat_target", True, False, dict(target)) is True


def chat_characters(target: dict) -> list[dict]:
    """Optional names a known session may take. The core has none."""
    got = _call("chat_characters", [], [], target)
    return [item for item in got if isinstance(item, dict)] if isinstance(got, list) else []


def chat_action(action: str, target: dict, body: dict) -> dict | None:
    """Optional direct-chat action for an already-known session.

    The core authenticates the owner and finds the target before this runs.
    Add-ons therefore cannot use it to add a route or reach an unknown pane.
    """
    got = _call("chat_action", None, None, action, target, body)
    return got if isinstance(got, dict) else None


def tts_fix(kind: str) -> dict | None:
    """Your repair steps for a comms delivery failure, shown with it: kind
    "undelivered" (a clip never reached the player) or "silent" (it arrived
    and did not play). {"what", "where", "cmd", "after"}, each text; "cmd"
    is one line to paste. The core has none, so it names the failure and
    offers nothing."""
    got = _call("tts_fix", None, None, kind)
    return {key: str(got.get(key) or "") for key in ("what", "where", "cmd", "after")} if isinstance(got, dict) else None


def voice_host_status(host: str, report: dict | None, now: float) -> dict:
    """What only you know about a voice host that is not ready: {"why":
    words} replaces the core's reason (a host stopped on purpose, say), and
    {"resting": True} marks it down on purpose, so the quiet "Voices on X
    only" line leaves it out. Nothing here makes a host ready, so a broken
    add-on costs only its words."""
    got = _call("voice_host_status", None, None, host, report, now)
    return got if isinstance(got, dict) else {}
