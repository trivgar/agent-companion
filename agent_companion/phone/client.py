"""One client for the phone driver's relay: the `phone` command (bin/phone)
and the phone agent's MCP tools both use it.
Plan: docs/superpowers/plans/2026-11-16-phone-driver-2-relay.md.

It talks to agent-companion on loopback with the write token. A started job's
capability stays with this client (or its optional local cache) and goes on
its command and end calls. The phone agent's key is an argument and never read
here: the `phone` command passes none, so it can drive only the test emulator."""
from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import config
from . import settings

def default_base() -> str:
    return str(os.environ.get("AGENT_COMPANION_URL") or config.get("url")
               or f"http://{config.get('server_bind', '127.0.0.1')}:{config.get('server_port', 8770)}")


def token_file() -> Path:
    configured = os.environ.get("AGENT_COMPANION_TOKEN_FILE") or config.get("hook_token_file")
    if configured:
        return Path(str(configured)).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "agent-companion" / "agent-companion-token"
KEY_HEADER = "x-phone-agent-key"
JOB_CAP_HEADER = "x-phone-job-cap"
TIMEOUT = 90           # the relay holds a command up to 60 s, a start up to about 65


def new_cmd_id() -> str:
    return "c-" + secrets.token_hex(8)


def _read_token() -> str:
    try:
        return token_file().read_text().strip()
    except OSError:
        return ""


class Client:
    def __init__(self, base: str | None = None, token: str | None = None, key: str = "", opener=None,
                 wait=time.sleep, cap_path: Path | None = None):
        self.base = (base or default_base()).rstrip("/")
        self.token = _read_token() if token is None else token
        self.key = key
        self.opener = opener or urllib.request.urlopen
        self.wait = wait
        self.cap_path = Path(cap_path) if cap_path is not None else None
        self.caps = self._load_caps()

    def _load_caps(self) -> dict[str, str]:
        if self.cap_path is None:
            return {}
        try:
            got = json.loads(self.cap_path.read_text())
        except (OSError, ValueError):
            return {}
        return {str(job): cap for job, cap in got.items()
                if isinstance(job, str) and isinstance(cap, str) and cap} if isinstance(got, dict) else {}

    def _save_caps(self) -> None:
        if self.cap_path is None:
            return
        try:
            self.cap_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = self.cap_path.with_name(f".{self.cap_path.name}.{os.getpid()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.caps, f)
            os.replace(tmp, self.cap_path)
        except OSError:
            # A cache miss merely makes a future command refuse safely; do not
            # hide a useful relay reply because this optional local cache failed.
            pass

    def _remember_cap(self, got: dict) -> None:
        job = got.get("job") if isinstance(got, dict) else None
        cap = got.get("job_cap") if isinstance(got, dict) else None
        job_id = job.get("id") if isinstance(job, dict) else None
        if isinstance(job_id, str) and job_id and isinstance(cap, str) and cap:
            self.caps[job_id] = cap
            self._save_caps()

    def _job_headers(self, job_id: str, cap: str | None) -> dict[str, str]:
        use = cap if cap is not None else self.caps.get(job_id, "")
        return {JOB_CAP_HEADER: use} if use else {}

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = TIMEOUT,
             headers: dict[str, str] | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header(settings.token_header(), self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.key:
            req.add_header(KEY_HEADER, self.key)
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        try:
            with self.opener(req, timeout=timeout) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode() or "{}").get("detail")
            except ValueError:
                detail = None
            return {"ok": False, "code": f"http_{e.code}", "text": str(detail or e.reason)}
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            return {"ok": False, "code": "unreachable",
                    "text": f"{settings.app_name()} did not answer ({type(e).__name__}): "
                            f"is {settings.app_name()} running?"}
        except ValueError:
            return {"ok": False, "code": "bad_reply", "text": f"{settings.app_name()}'s answer was not JSON"}

    def start(self, device: str, task: str, agent: str) -> dict:
        got = self.call("POST", "/api/phone/jobs", {"device": device, "task": task, "agent": agent})
        self._remember_cap(got)
        return got

    def jobs(self) -> dict:
        return self.call("GET", "/api/phone/jobs")

    def live_job(self, device: str) -> dict | None:
        for job in self.jobs().get("live") or []:
            if job.get("device") == device:
                return job
        return None

    def command(self, job_id: str, verb: str, args: dict | None = None, cmd_id: str | None = None,
                cap: str | None = None) -> dict:
        """One command, answered. If agent-companion drops the call mid-command (the
        merge queue restarts it), it asks once more with the same cmd_id, which
        the relay and the driver answer without running it twice."""
        body = {"cmd_id": cmd_id or new_cmd_id(), "verb": verb, "args": args or {}}
        headers = self._job_headers(job_id, cap)
        got = self.call("POST", f"/api/phone/jobs/{job_id}/cmd", body, headers=headers)
        if got.get("code") == "unreachable":
            self.wait(5)
            got = self.call("POST", f"/api/phone/jobs/{job_id}/cmd", body, headers=headers)
            if got.get("code") == "unreachable":
                got["text"] = f"{settings.app_name()} went away mid-command (a restart?): look again before acting"
        return got

    def end(self, job_id: str, cap: str | None = None) -> dict:
        got = self.call("POST", f"/api/phone/jobs/{job_id}/end", {}, headers=self._job_headers(job_id, cap))
        if got.get("ok"):
            self.caps.pop(job_id, None)
            self._save_caps()
        return got

    def card_stop(self, job_id: str) -> dict:
        """The dashboard card's Stop, with the same loopback Origin as its page."""
        return self.call("POST", f"/api/phone/jobs/{job_id}/stop", {}, headers={"Origin": self.base})
