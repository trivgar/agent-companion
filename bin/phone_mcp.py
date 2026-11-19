#!/usr/bin/env python3
"""phone-agent's stdio MCP server for the phone driver.

The wire format is newline-delimited JSON-RPC 2.0.  Standard output is only
for protocol replies; diagnostics belong on standard error.
"""
from __future__ import annotations

import base64
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_companion import config  # noqa: E402
from agent_companion.phone import client as phoneclient  # noqa: E402
from agent_companion.phone import drive as phonedrive  # noqa: E402
from agent_companion.phone import emu as phoneemu  # noqa: E402
from agent_companion.phone import job as phonejob  # noqa: E402
from agent_companion.phone import settings as phone_settings  # noqa: E402

leases = phonejob.leases


SERVER_NAME = "phone"
SERVER_VERSION = "1"
AGENT = phone_settings.agent_name()
LEASE_HOLDER = "phone-agent"
KEY_FILE = Path(str(config.get("phone_agent_key_file") or phonedrive.AGENT_KEY)).expanduser()
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
SHOT_MAX = 4 * 1024 * 1024 + 1024   # the relay caps an upload at 4 MiB
RULES = ("Numbers belong to the latest look; held means the device holder's tap; your_turn means the device holder types; "
         "twins means the device holder chooses the matching action.")


def _schema(properties: dict[str, Any], required: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    """The small common part of each MCP tool input schema."""
    result: dict[str, Any] = {"type": "object", "properties": properties,
                              "additionalProperties": False}
    if required:
        result["required"] = required
    result.update(extra)
    return result


def _tool(name: str, action: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "description": f"{action} {RULES}", "inputSchema": schema}


TOOLS = (
    _tool("phone_start", "Start phone-agent's one current job on an emulator, or on the phone when its key is present.",
          _schema({"task": {"type": "string", "minLength": 1},
                   "device": {"type": "string", "enum": ["emulator", "phone"], "default": "emulator"}},
                  ["task"])),
    _tool("phone_look", "Read the current numbered screen.", _schema({})),
    _tool("phone_tap", "Tap a numbered latest-look element or an x,y point.",
          _schema({"n": {"type": "integer", "minimum": 0},
                   "x": {"type": "integer", "minimum": 0},
                   "y": {"type": "integer", "minimum": 0}},
                  oneOf=[{"required": ["n"]}, {"required": ["x", "y"]}])),
    _tool("phone_long", "Long-press a numbered latest-look element or an x,y point.",
          _schema({"n": {"type": "integer", "minimum": 0},
                   "x": {"type": "integer", "minimum": 0},
                   "y": {"type": "integer", "minimum": 0}},
                  oneOf=[{"required": ["n"]}, {"required": ["x", "y"]}])),
    _tool("phone_type", "Set a numbered field's text in one go, never key by key or with Enter.",
          _schema({"n": {"type": "integer", "minimum": 0}, "text": {"type": "string"}}, ["n", "text"])),
    _tool("phone_scroll", "Scroll a numbered list or the screen up, down, left, or right.",
          _schema({"dir": {"type": "string", "enum": list(phonedrive.DIRS)},
                   "n": {"type": "integer", "minimum": 0}}, ["dir"])),
    _tool("phone_swipe", "Swipe from x1,y1 to x2,y2, normally for 300 milliseconds.",
          _schema({"x1": {"type": "integer", "minimum": 0},
                   "y1": {"type": "integer", "minimum": 0},
                   "x2": {"type": "integer", "minimum": 0},
                   "y2": {"type": "integer", "minimum": 0},
                   "ms": {"type": "integer", "minimum": 50, "maximum": 5000, "default": 300}},
                  ["x1", "y1", "x2", "y2"])),
    _tool("phone_drag", "Press and hold, move, and let go: pick up a numbered latest-look element "
          "(n) or the point x1,y1, and drop it on a numbered element (to) or the point x2,y2, "
          "such as an icon onto a folder. The move takes 800 milliseconds unless given. "
          "Never ends at the top of the screen, where launchers put Remove and Uninstall. "
          "It moves an icon to a spot; to put one into a folder use phone_move.",
          _schema({"n": {"type": "integer", "minimum": 0},
                   "x1": {"type": "integer", "minimum": 0},
                   "y1": {"type": "integer", "minimum": 0},
                   "to": {"type": "integer", "minimum": 0},
                   "x2": {"type": "integer", "minimum": 0},
                   "y2": {"type": "integer", "minimum": 0},
                   "ms": {"type": "integer", "minimum": 200, "maximum": 5000, "default": 800}})),
    _tool("phone_move", "The launcher's own move for a numbered latest-look home screen icon, as a "
          "screen reader does it. Then look: every spot is its own element (\"Create folder with: X\", "
          "\"Add to folder: X\", \"Move to row R column C\") to tap by number; press back to cancel. "
          "Never tap Remove or Uninstall there.",
          _schema({"n": {"type": "integer", "minimum": 0}}, ["n"])),
    _tool("phone_press", "Press back, home, recents, or enter.",
          _schema({"key": {"type": "string", "enum": list(phonedrive.KEYS)}}, ["key"])),
    _tool("phone_open", "Open an app by its name.", _schema({"app": {"type": "string", "minLength": 1}}, ["app"])),
    _tool("phone_wait", "Wait for the screen to settle or text to show, for no more than 55 seconds.",
          _schema({"text": {"type": "string", "minLength": 1, "maxLength": 200},
                   "seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 55, "default": 10}})),
    _tool("phone_shot", "Take a screenshot of the current screen.", _schema({})),
    _tool("phone_end", "End phone-agent's current job and return the app to the device.", _schema({})),
    _tool("phone_status", "Show live jobs, recent jobs, and registered devices.", _schema({})),
)
TOOL_NAMES = {tool["name"] for tool in TOOLS}


def _text_block(text: str) -> dict[str, str]:
    return {"type": "text", "text": text or "ok"}


def _tool_result(text: str, is_error: bool = False, content: list[dict] | None = None) -> dict[str, Any]:
    return {"content": content if content is not None else [_text_block(text)], "isError": is_error}


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class PhoneServer:
    """One phone-agent process, holding at most one job in its own memory."""

    def __init__(self, client=None, *, client_factory=None, lease_api=leases,
                 key_file: Path | None = None, lease_session: str | None = None,
                 emulator_touch=phonejob.touch_emulator,
                 emu_booted=phoneemu.booted, emu_start=phoneemu.start):
        self.client = client
        self.client_factory = client_factory or phoneclient.Client
        self.lease_api = lease_api
        self.key_file = Path(key_file or KEY_FILE)
        self.lease_session = lease_session
        self.emulator_touch = emulator_touch
        self.emu_booted = emu_booted
        self.emu_start = emu_start
        self.job: dict[str, str] | None = None

    def _client(self, key: str = ""):
        if self.client is None:
            self.client = self.client_factory(key=key)
        elif key and hasattr(self.client, "key"):
            self.client.key = key
        return self.client

    def _phone_key(self) -> str:
        """Read phone-agent's key here, and nowhere in the shared client module."""
        try:
            return self.key_file.read_text().strip()
        except OSError:
            return ""

    def _take_emulator_lease(self, task: str) -> dict:
        return phonejob.take_emulator_lease(
            LEASE_HOLDER, f"phone job: {task[:60]}", lease_api=self.lease_api,
            session=self.lease_session,
        )

    def _release_emulator_lease(self) -> None:
        phonejob.release_emulator_lease(LEASE_HOLDER, lease_api=self.lease_api,
                                        session=self.lease_session)

    @staticmethod
    def _relay_reply(got: Any) -> dict[str, Any] | None:
        """Return a tool reply for a relay result, or None for a success."""
        if not isinstance(got, dict) or not isinstance(got.get("ok"), bool):
            return _tool_result("agent-companion gave an invalid reply [bad_reply]", True)
        if got["ok"]:
            return None
        text = phonejob.result_text(got)
        return _tool_result(text, got.get("code") in ("unreachable", "bad_reply"))

    @staticmethod
    def _arguments_error(name: str, arguments: Any) -> str | None:
        if not isinstance(arguments, dict):
            return f"{name} needs an object of arguments"
        allowed = {
            "phone_start": {"task", "device"}, "phone_look": set(),
            "phone_tap": {"n", "x", "y"}, "phone_long": {"n", "x", "y"},
            "phone_type": {"n", "text"}, "phone_scroll": {"dir", "n"},
            "phone_swipe": {"x1", "y1", "x2", "y2", "ms"}, "phone_press": {"key"},
            "phone_drag": {"n", "x1", "y1", "to", "x2", "y2", "ms"}, "phone_move": {"n"},
            "phone_open": {"app"}, "phone_wait": {"text", "seconds"},
            "phone_shot": set(), "phone_end": set(), "phone_status": set(),
        }[name]
        unexpected = set(arguments) - allowed
        if unexpected:
            return f"{name} does not take {sorted(unexpected)[0]!r}"
        if name == "phone_start":
            if not isinstance(arguments.get("task"), str) or not arguments["task"].strip():
                return "phone_start needs a task"
            if arguments.get("device", "emulator") not in ("emulator", "phone"):
                return "phone_start device is emulator or phone"
        elif name in ("phone_tap", "phone_long"):
            has_n = _integer(arguments.get("n"))
            has_xy = _integer(arguments.get("x")) and _integer(arguments.get("y"))
            if not (has_n or has_xy):
                return f"{name} needs n, or x and y"
            if has_n and ("x" in arguments or "y" in arguments):
                return f"{name} takes n, or x and y"
        elif name == "phone_type":
            if not _integer(arguments.get("n")) or not isinstance(arguments.get("text"), str):
                return "phone_type needs n and text"
        elif name == "phone_scroll":
            if arguments.get("dir") not in phonedrive.DIRS:
                return "phone_scroll dir is up, down, left, or right"
            if "n" in arguments and not _integer(arguments["n"]):
                return "phone_scroll n is a number from the latest look"
        elif name == "phone_swipe":
            if not all(_integer(arguments.get(key)) for key in ("x1", "y1", "x2", "y2")):
                return "phone_swipe needs x1, y1, x2, and y2"
            if "ms" in arguments and (not _integer(arguments["ms"]) or not 50 <= arguments["ms"] <= 5000):
                return "phone_swipe ms is 50 to 5000"
        elif name == "phone_move" and not _integer(arguments.get("n")):
            return "phone_move needs n, a home screen icon's number from the latest look"
        elif name == "phone_drag":
            has_n = _integer(arguments.get("n"))
            has_xy1 = _integer(arguments.get("x1")) and _integer(arguments.get("y1"))
            has_to = _integer(arguments.get("to"))
            has_xy2 = _integer(arguments.get("x2")) and _integer(arguments.get("y2"))
            if not ((has_n or has_xy1) and (has_to or has_xy2)):
                return "phone_drag needs n or x1 and y1, and to or x2 and y2"
            if (has_n and ("x1" in arguments or "y1" in arguments)) or (
                    has_to and ("x2" in arguments or "y2" in arguments)):
                return "phone_drag takes n or x1 and y1, and to or x2 and y2, not both"
            if "ms" in arguments and (not _integer(arguments["ms"]) or not 200 <= arguments["ms"] <= 5000):
                return "phone_drag ms is 200 to 5000"
        elif name == "phone_press" and arguments.get("key") not in phonedrive.KEYS:
            return "phone_press key is back, home, recents, or enter"
        elif name == "phone_open" and (not isinstance(arguments.get("app"), str) or not arguments["app"].strip()):
            return "phone_open needs an app name"
        elif name == "phone_wait":
            seconds = arguments.get("seconds", 10)
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds <= 55:
                return "phone_wait seconds is more than 0 and at most 55"
            text = arguments.get("text")
            if text is not None and (not isinstance(text, str) or not 0 < len(text) <= 200):
                return "phone_wait text is 1 to 200 characters"
        return None

    def _start(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.job is not None:
            return _tool_result(f"a live job is already phone-agent's: {self.job['id']}: {self.job['task']} [busy]")
        task = arguments["task"]
        device = arguments.get("device", "emulator")
        if device == "phone" and not phone_settings.real_phone_enabled():
            return _tool_result("the real phone driver is off; set phone_driver_real_phone = true [driver_off]")
        key = self._phone_key() if device == "phone" else ""
        if device == "phone" and not key:
            return _tool_result("the phone agent key is not available [no_key]")
        lease = None
        if device == "emulator":
            lease = self._take_emulator_lease(task)
            if not lease.get("ok"):
                return _tool_result(f"{lease.get('detail') or 'the emulator lease was refused'} [lease]")
            # phone-agent has no shell, so his start boots the emulator himself,
            # under his own lease: the emulator's watcher stops it the moment
            # nobody holds the lease (found on his first run, 2026-11-16).
            if not self.emu_booted():
                ok, words = self.emu_start()
                if not ok:
                    phonejob.release_if_new(lease, LEASE_HOLDER, lease_api=self.lease_api,
                                            session=self.lease_session)
                    return _tool_result(f"{words} [emulator]")
            self.emulator_touch()
        got = self._client(key).start(device, task, AGENT)
        reply = self._relay_reply(got)
        if reply is not None:
            if device == "emulator":
                phonejob.release_if_new(lease, LEASE_HOLDER, lease_api=self.lease_api,
                                        session=self.lease_session)
            return reply
        job = got.get("job")
        if not isinstance(job, dict) or not isinstance(job.get("id"), str) or not job["id"]:
            if device == "emulator":
                phonejob.release_if_new(lease, LEASE_HOLDER, lease_api=self.lease_api,
                                        session=self.lease_session)
            return _tool_result("agent-companion did not name the new job [bad_reply]", True)
        cap = got.get("job_cap")
        if not isinstance(cap, str) or not cap:
            if device == "emulator":
                phonejob.release_if_new(lease, LEASE_HOLDER, lease_api=self.lease_api,
                                        session=self.lease_session)
            return _tool_result("agent-companion did not return the job capability [bad_reply]", True)
        self.job = {"id": job["id"], "device": device, "task": task, "cap": cap}
        words = phonejob.result_text(got, include_shot=False)
        return _tool_result(f"job {job['id']} on the {device}: {words}")

    def _current_job(self) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
        if self.job is None:
            return None, _tool_result("no current phone job: call phone_start first [no_job]")
        return self.job, None

    def _forget_if_over(self, got: Any) -> None:
        """The relay ended the job itself (a stop, a stall, a lost phone):
        forget it and let the emulator go, so the lease does not sit 35 min."""
        if not isinstance(got, dict) or self.job is None:
            return
        view = got.get("job") if isinstance(got.get("job"), dict) else {}
        if view.get("state") == "ended" or got.get("code") == "no_job":
            if self.job["device"] == "emulator":
                self._release_emulator_lease()
            self.job = None

    def _shot_bytes(self, job_id: str, shot: str) -> bytes | None:
        """A screenshot only from this job's own shots/ folder, a regular file
        of PNG size: a relay reply never picks which file is read."""
        root = os.path.realpath(phonedrive.DATA / "jobs" / job_id / "shots")
        path = os.path.realpath(shot)
        if not path.startswith(root + os.sep):
            return None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > SHOT_MAX:
                return None
            with os.fdopen(fd, "rb") as f:
                fd = -1
                return f.read(SHOT_MAX + 1)[:SHOT_MAX]
        finally:
            if fd != -1:
                os.close(fd)

    def _end(self) -> dict[str, Any]:
        job, reply = self._current_job()
        if reply is not None:
            return reply
        if job["device"] == "emulator":
            lease = self._take_emulator_lease(job["task"])
            if not lease.get("ok"):
                return _tool_result(f"{lease.get('detail') or 'the emulator lease was refused'} [lease]")
            self.emulator_touch()
        got = self._client().end(job["id"], cap=job["cap"])
        reply = self._relay_reply(got)
        if reply is not None:
            self._forget_if_over(got)
            return reply
        if job["device"] == "emulator":
            self._release_emulator_lease()
        self.job = None
        return _tool_result(phonejob.result_text(got, include_shot=False))

    def _status(self) -> dict[str, Any]:
        got = self._client().jobs()
        reply = self._relay_reply(got)
        if reply is not None:
            return reply
        return _tool_result("\n".join(phonejob.status_lines(got)))

    def _command(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        job, reply = self._current_job()
        if reply is not None:
            return reply
        if job["device"] == "emulator":
            lease = self._take_emulator_lease(job["task"])
            if not lease.get("ok"):
                return _tool_result(f"{lease.get('detail') or 'the emulator lease was refused'} [lease]")
            self.emulator_touch()
        verb = name.removeprefix("phone_")
        relay_args: dict[str, Any] = dict(arguments)
        if verb == "wait":
            relay_args = {"s": relay_args.pop("seconds", 10), **relay_args}
        if verb == "swipe":
            relay_args.setdefault("ms", 300)
        got = self._client().command(job["id"], verb, relay_args, cap=job["cap"])
        reply = self._relay_reply(got)
        if reply is not None:
            self._forget_if_over(got)
            return reply
        if verb != "shot":
            return _tool_result(phonejob.result_text(got, include_shot=False))
        shot = got.get("shot")
        if not isinstance(shot, str) or not shot:
            return _tool_result("agent-companion did not return the screenshot [bad_reply]", True)
        image = self._shot_bytes(job["id"], shot)
        if image is None:
            return _tool_result("the screenshot is not this job's own [bad_reply]", True)
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            return _tool_result("the screenshot was not a PNG [bad_reply]", True)
        content = [
            {"type": "image", "data": base64.b64encode(image).decode("ascii"), "mimeType": "image/png"},
            _text_block(phonejob.result_text(got)),
        ]
        return _tool_result("", content=content)

    def call_tool(self, name: Any, arguments: Any) -> dict[str, Any]:
        if not isinstance(name, str) or name not in TOOL_NAMES:
            return _tool_result(f"no such phone tool {name!r}", True)
        error = self._arguments_error(name, arguments)
        if error:
            return _tool_result(error, True)
        if name == "phone_start":
            return self._start(arguments)
        if name == "phone_end":
            return self._end()
        if name == "phone_status":
            return self._status()
        return self._command(name, arguments)

    def handle(self, request: Any) -> dict[str, Any] | None:
        """Handle one decoded JSON-RPC request, returning None for a notification."""
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                    "error": {"code": -32600, "message": "Invalid Request"}}
        notification = "id" not in request
        method = request["method"]
        params = request.get("params", {})
        request_id = request.get("id")
        if method == "initialize":
            protocol = params.get("protocolVersion") if isinstance(params, dict) else None
            result = {"protocolVersion": protocol if isinstance(protocol, str) else DEFAULT_PROTOCOL_VERSION,
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": list(TOOLS)}
        elif method == "tools/call":
            if not isinstance(params, dict):
                result = _tool_result("tools/call needs an object of parameters", True)
            else:
                result = self.call_tool(params.get("name"), params.get("arguments", {}))
        elif method == "notifications/initialized":
            result = None
        else:
            if notification:
                return None
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32601, "message": "Method not found"}}
        if notification or result is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


Server = PhoneServer


def serve(reader=None, writer=None, server: PhoneServer | None = None) -> None:
    """Run the newline-delimited stdio server until standard input closes."""
    reader = reader or sys.stdin
    writer = writer or sys.stdout
    server = server or PhoneServer()
    for line in reader:
        try:
            if isinstance(line, bytes):
                line = line.decode("utf-8")
            request = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            reply = {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "Parse error"}}
        else:
            try:
                reply = server.handle(request)
            except Exception as exc:  # a malformed local dependency must not kill phone-agent's server
                print(f"phone_mcp: {type(exc).__name__}: {exc}", file=sys.stderr)
                request_id = request.get("id") if isinstance(request, dict) else None
                reply = {"jsonrpc": "2.0", "id": request_id,
                         "error": {"code": -32603, "message": "Internal error"}}
        if reply is not None:
            writer.write(json.dumps(reply, separators=(",", ":")) + "\n")
            writer.flush()


if __name__ == "__main__":
    if not phone_settings.enabled():
        print("phone_mcp: the phone driver is off; set phone_driver = true to enable it", file=sys.stderr)
        sys.exit(1)
    serve()
