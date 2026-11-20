"""The phone driver's relay: jobs, one command at a time, every one logged.

Spec: docs/superpowers/specs/2026-11-13-phone-driver-design.md (sections 1, 3, 6).
Plan: docs/superpowers/plans/2026-11-16-phone-driver-2-relay.md, which holds
the wire protocol the driver in the agent-companion app speaks (step 3).

An agent starts a job on a device ("emulator" or "phone"); agent-companion wakes the
device with a `drive` push carrying only the job id; the driver asks for the
job, then long-polls for commands, runs each and posts its result. Between
jobs nothing polls.

Every route that calls this is async, so it all runs on the server's event
loop and needs no locks: a waiter checks its condition every `tick`, as the
comms outbox's long poll does. The timers are lazy (`tidy`, run by every
call): nothing runs in the background, so nothing starts under pytest.

Files, folders 700 and files 600, under DATA:
  devices.json               the registered drivers; which is the phone, which the emulator
  jobs/<id>/job.json         task, agent, device, times, state, outcome
  jobs/<id>/actions.jsonl    every command; its result (the target as the driver read it);
                             every event: prompts and the device holder's answers, stops, takeovers
  jobs/<id>/looks/<cmd>.txt  what the screen said     } deleted seven days after
  jobs/<id>/shots/<cmd>.png  screenshots               } the job ends (sweep)
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

from . import settings

DATA = Path(os.environ.get("AGENT_COMPANION_PHONE_DIR")
            or Path.home() / ".local" / "share" / "agent-companion" / "phone")
AGENT_KEY = Path(os.environ.get("AGENT_COMPANION_PHONE_AGENT_KEY")
                 or Path.home() / ".config" / "agent-companion" / "phone-agent.key")
KEY_HEADER = "x-phone-agent-key"
DEVICE_SECRET_HEADER = "x-phone-device-secret"
JOB_CAP_HEADER = "x-phone-job-cap"
DEVICES = ("emulator", "phone")
VERBS = ("look", "tap", "long", "type", "scroll", "swipe", "drag", "move", "press", "open", "wait", "shot")
KEYS = ("back", "home", "recents", "enter")
DIRS = ("up", "down", "left", "right")
EVENTS = ("started", "locked", "unlocked", "call_wait", "prompt", "answer", "your_turn", "continue",
          "takeover", "resume", "stop", "end", "driver_off")
STOPPED_BY = ("banner", "notification", "power")
DEVICE_ENDS = ("expired", "lost", "failed")
KEEP_S = 7 * 86400
SHOT_MAX = 4 * 1024 * 1024
TEXT_MAX = 64 * 1024
RESULTS_MAX = 500
ID_RE = re.compile(r"^[A-Za-z0-9-]{16,64}$")
CMD_RE = re.compile(r"^[A-Za-z0-9_-]{6,40}$")
JOB_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
TIMING = {"tick": 0.1, "pickup_resend_s": 30.0, "pickup_give_up_s": 60.0, "starting_s": 20.0,
          "cmd_wait_s": 60.0, "poll_hold_s": 25.0, "stall_s": 300.0, "lock_wait_s": 900.0,
          "prompt_s": 360.0, "lost_s": 180.0, "sweep_every_s": 3600.0, "forget_s": 3600.0}
NOT_ANSWERING = {"phone": "the phone isn't answering",
                 "emulator": "the emulator isn't answering: is it up, with the -emu build and its driver on? "
                             "(phone emu status)"}
VIEW = ("id", "device", "task", "agent", "state", "outcome", "detail", "created", "started", "ended",
        "last_action", "prompt", "your_turn", "by")
LOG = logging.getLogger(__name__)


def DRIVER_OFF() -> str:
    return f"the driver is off: Settings, Accessibility, {settings.app_name()}"


def ENDED_WORDS() -> dict[str, str]:
    return {"expired": "the job expired on the phone",
            "lost": f"the phone lost {settings.app_name()} for two minutes and gave itself back",
            "failed": "the driver failed"}


def _no(code: str, text: str, **extra) -> dict:
    return {"ok": False, "code": code, "text": text, **extra}


def _write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, p)


def _append(p: Path, row: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(row) + "\n")


def _write_bytes(p: Path, data: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _read_json(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _secret_hash(secret: str) -> str:
    """The only form of a device or job secret that reaches disk."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _hm(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100_000


def args_error(verb: str, args: dict) -> str | None:
    """Why a command's arguments are wrong, in words, or None. The driver checks again."""
    if verb not in VERBS:
        return f"no such verb {verb!r}: one of " + ", ".join(VERBS)
    if verb in ("tap", "long"):
        if _int(args.get("n")) or (_int(args.get("x")) and _int(args.get("y"))):
            return None
        return f"{verb} takes a number from the last look, or x and y"
    if verb == "type":
        if not _int(args.get("n")):
            return "type takes the field's number from the last look"
        text = args.get("text")
        return None if isinstance(text, str) and len(text) <= 4000 else "type takes text, at most 4000 characters"
    if verb == "scroll":
        if args.get("dir") not in DIRS:
            return "scroll takes a direction: up, down, left or right"
        return None if "n" not in args or _int(args.get("n")) else "scroll takes a list's number, or none for the screen"
    if verb == "swipe":
        if not all(_int(args.get(k)) for k in ("x1", "y1", "x2", "y2")):
            return "swipe takes x1,y1 and x2,y2"
        ms = args.get("ms", 300)
        return None if _int(ms) and 50 <= ms <= 5000 else "a swipe lasts 50 to 5000 ms"
    if verb == "move":
        return None if _int(args.get("n")) else "move takes a home screen icon's number from the last look"
    if verb == "drag":
        # Press and hold, move, let go: an icon onto a folder (2026-11-18, for
        # phone-agent's launcher job). From a number or x1,y1 to a number (to) or x2,y2.
        start = _int(args.get("n")) or (_int(args.get("x1")) and _int(args.get("y1")))
        end = _int(args.get("to")) or (_int(args.get("x2")) and _int(args.get("y2")))
        if not (start and end):
            return "drag takes a number or x1,y1 to start from, and a number (to) or x2,y2 to end on"
        ms = args.get("ms", 800)
        return None if _int(ms) and 200 <= ms <= 5000 else "a drag moves for 200 to 5000 ms"
    if verb == "press":
        return None if args.get("key") in KEYS else "press takes back, home, recents or enter"
    if verb == "open":
        app = args.get("app")
        return None if isinstance(app, str) and 0 < len(app.strip()) <= 80 else "open takes an app's name"
    if verb == "wait":
        s = args.get("s", 10)
        if isinstance(s, bool) or not isinstance(s, (int, float)) or not 0 < s <= 55:
            return "wait takes up to 55 seconds"
        text = args.get("text")
        if text is None or (isinstance(text, str) and 0 < len(text) <= 200):
            return None
        return "wait's text is 1 to 200 characters"
    return None


def sweep(data: Path | None = None, now: float | None = None, keep_s: float = KEEP_S) -> int:
    """Delete looks/ and shots/ of jobs that ended more than keep_s ago;
    job.json and actions.jsonl stay. Returns how many folders went."""
    data, now = Path(data or DATA), time.time() if now is None else now
    gone = 0
    for p in sorted((data / "jobs").glob("*/job.json")):
        job = _read_json(p)
        if not isinstance(job, dict) or job.get("state") != "ended":
            continue
        if now - float(job.get("ended") or now) < keep_s:
            continue
        for sub in ("looks", "shots"):
            d = p.parent / sub
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
                gone += 1
    return gone


class Relay:
    """The jobs. The phone router owns one relay; tests make their own with a
    temp folder, fakes and short timings."""

    def __init__(self, data: Path | None = None, push=None, notify=None, clock=time.time,
                 key_path: Path | None = None, **timing):
        self.data = Path(data or DATA)
        self.push = push or (lambda device, job: "no push sender")
        self.notify = notify or (lambda title, body: None)
        self.clock = clock
        self.key_path = Path(key_path or AGENT_KEY)
        self.t = {**TIMING, **timing}
        self.jobs: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}
        self.inflight: dict[str, dict] = {}
        self.results: dict[tuple[str, str], dict] = {}
        self.swept_at = 0.0
        for d in (self.data, self.data / "jobs"):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
        self._load()

    # --- files -------------------------------------------------------------------
    def _dir(self, job_id: str) -> Path:
        return self.data / "jobs" / job_id

    def _save(self, job: dict) -> None:
        _write_json(self._dir(job["id"]) / "job.json", job)

    def _log(self, job: dict, row: dict) -> None:
        _append(self._dir(job["id"]) / "actions.jsonl", {"at": round(self.clock(), 3), **row})

    def _read_job(self, job_id: str) -> dict | None:
        if not JOB_RE.match(str(job_id or "")):
            return None
        job = _read_json(self._dir(job_id) / "job.json")
        return job if isinstance(job, dict) and job.get("id") == job_id else None

    def _load(self) -> None:
        """Live jobs and their answered commands, after a restart. Their clocks
        restart too: the device reconnects and the agent looks again."""
        now = self.clock()
        for p in sorted((self.data / "jobs").glob("*/job.json")):
            job = self._read_job(p.parent.name)
            if job is None or job.get("state") == "ended":
                continue
            job["last_poll"] = now if job.get("last_poll") else None
            job["last_cmd"] = now
            self.jobs[job["id"]] = job
            try:
                rows = [json.loads(x) for x in (p.parent / "actions.jsonl").read_text().splitlines() if x.strip()]
            except (OSError, ValueError):
                rows = []
            for row in rows:
                if row.get("kind") == "result" and CMD_RE.match(str(row.get("cmd_id") or "")):
                    self.results[(job["id"], row["cmd_id"])] = self._reply_on_disk(job, row)

    def _reply_on_disk(self, job: dict, row: dict) -> dict:
        d = self._dir(job["id"])
        try:
            text = (d / "looks" / f"{row['cmd_id']}.txt").read_text()
        except OSError:
            text = ""
        reply = {"ok": bool(row.get("ok")), "code": str(row.get("code") or ""), "text": text,
                 "target": str(row.get("target") or "")}
        shot = d / "shots" / f"{row['cmd_id']}.png"
        if shot.is_file():
            reply["shot"] = str(shot)
        return reply

    # --- devices -----------------------------------------------------------------
    def devices(self) -> dict:
        d = _read_json(self.data / "devices.json")
        if not isinstance(d, dict) or not isinstance(d.get("devices"), dict):
            d = {"devices": {}, "phone": None, "emulator": None}
        return d

    @staticmethod
    def _device_approved(d: dict, device_id: str, rec: dict) -> bool:
        """Whether this record may receive work.

        Devices paired before approval existed have no ``approved`` field. An
        emulator was always safe to use, while only the phone that was driving
        at the time is grandfathered.
        """
        if not isinstance(rec, dict):
            return False
        if rec.get("kind") == "emulator":
            return True
        if rec.get("kind") != "phone":
            return False
        if "approved" not in rec:
            return d.get("phone") == device_id
        return rec.get("approved") is True

    def _device_secret(self, device_id: str, secret: str) -> dict:
        """Check one paired driver's install secret without ever retaining it.

        A legacy record that has no digest is deliberately not silently paired:
        that would let a holder of the generic write token take it over. Forget
        it locally first, then let the app make a fresh first registration.
        """
        rec = self.devices()["devices"].get(device_id)
        want = rec.get("secret_sha256") if isinstance(rec, dict) else None
        if not isinstance(want, str) or not want:
            why = "the device is not paired"
        elif not isinstance(secret, str) or not secret or not hmac.compare_digest(want, _secret_hash(secret)):
            why = "the device secret is missing or does not match"
        else:
            return {"ok": True}
        LOG.warning("phone device secret refused for %r: %s", device_id[:64], why)
        return _no("bad_device_secret", why)

    def register(self, body: dict, secret: str = "") -> dict:
        """A driver says where it is: its id, kind, model and push token."""
        did = str(body.get("device_id") or "")
        if not ID_RE.match(did):
            return _no("bad_args", "device_id is 16 to 64 letters, digits or dashes")
        kind = body.get("kind")
        if kind not in DEVICES:
            return _no("bad_args", "kind is phone or emulator")
        now = self.clock()
        d = self.devices()
        old = d["devices"].get(did) or {}
        first_pairing = not old
        if not first_pairing:
            allowed = self._device_secret(did, secret)
            if not allowed["ok"]:
                return allowed
        device_secret = secrets.token_urlsafe(32) if first_pairing else ""
        if kind == "emulator":
            # Only the first emulator is taken on trust; a second one never
            # replaces it silently, so a job meant for one cannot reach another.
            current = d.get("emulator")
            approved = current in (None, did) or current not in d["devices"]
        else:
            approved = old.get("kind") == "phone" and self._device_approved(d, did, old)
        rec = {"kind": kind, "model": str(body.get("model") or "")[:80],
               "android": str(body.get("android") or "")[:16],
               "fcm": str(body.get("fcm") or old.get("fcm") or "")[:4096],
               "driver_on": body.get("driver_on") is True,
               "app_version": str(body.get("app_version") or "")[:32],
               "registered": old.get("registered") or now, "seen": now,
               "secret_sha256": _secret_hash(device_secret) if first_pairing else old["secret_sha256"],
               "approved": approved}
        d["devices"][did] = rec
        if approved:
            d[kind] = did
        _write_json(self.data / "devices.json", d)
        if kind == "phone" and first_pairing:
            self.notify("Phone driver", "A new phone wants to drive: "
                        f"{rec['model'] or 'a phone'}. Approve it in {settings.app_name()}'s phone section, or forget it.")
        out = {"ok": True, "role": kind if approved else "pending", "driver_on": rec["driver_on"]}
        if first_pairing:
            out["device_secret"] = device_secret
        return out

    def approve_device(self, device_id: str) -> dict:
        """Make one pending phone the active phone driver."""
        if not ID_RE.match(str(device_id or "")):
            return _no("bad_args", "device_id is 16 to 64 letters, digits or dashes")
        d = self.devices()
        rec = d["devices"].get(device_id)
        if not isinstance(rec, dict):
            return _no("no_device", f"no such paired device {device_id}")
        if rec.get("kind") != "phone" or self._device_approved(d, device_id, rec):
            return _no("not_pending", "this device is not a pending phone")
        rec["approved"] = True
        # One approved phone at a time: a phone replaced earlier cannot come
        # back as the driver without his approval again (review on 0905995).
        for other_id, other in d["devices"].items():
            if other_id != device_id and isinstance(other, dict) and other.get("kind") == "phone":
                other["approved"] = False
        d["phone"] = device_id
        _write_json(self.data / "devices.json", d)
        text = f"Phone driver: {rec.get('model') or 'a phone'} approved"
        self.notify("Phone driver", text)
        return {"ok": True, "text": text}

    def forget_device(self, device_id: str) -> dict:
        """Forget a driver locally so its next registration is a first pairing.

        A live job would still name the old driver, so refuse the chore until
        that job has ended.
        """
        if not ID_RE.match(str(device_id or "")):
            return _no("bad_args", "device_id is 16 to 64 letters, digits or dashes")
        d = self.devices()
        if device_id not in d["devices"]:
            return _no("no_device", f"no such paired device {device_id}")
        for p in (self.data / "jobs").glob("*/job.json"):
            job = _read_json(p)
            if isinstance(job, dict) and job.get("device_id") == device_id and job.get("state") != "ended":
                return _no("busy", "end this device's live job before forgetting it")
        d["devices"].pop(device_id)
        for kind in DEVICES:
            if d.get(kind) == device_id:
                d[kind] = None
        _write_json(self.data / "devices.json", d)
        return {"ok": True, "text": f"forgot {device_id}; it may pair again"}

    def key_ok(self, key: str) -> bool:
        """The phone agent's key: a job on the device holder's phone needs it."""
        try:
            want = self.key_path.read_text().strip()
        except OSError:
            return False
        return bool(want) and bool(key) and hmac.compare_digest(want.encode(), str(key).encode())

    def live_on(self, device: str) -> dict | None:
        return next((j for j in self.jobs.values() if j["device"] == device and j["state"] != "ended"), None)

    def view(self, job: dict) -> dict:
        out = {k: job.get(k) for k in VIEW}
        out["shot"] = bool(job.get("last_shot"))
        return out

    # --- the agent's side -----------------------------------------------------------
    async def start(self, device: str, task: str, agent: str, key: str = "") -> dict:
        """Start a job, and wait until the device has it (or it gives up)."""
        self.tidy()
        if device not in DEVICES:
            return _no("bad_args", f"no such device {device!r}: emulator or phone")
        task = " ".join(str(task or "").split())[:300]
        if not task:
            return _no("bad_args", "a job needs its task, in a line")
        agent = re.sub(r"[^A-Za-z0-9 _.-]", "", str(agent or ""))[:40].strip() or "agent"
        if device == "phone" and not self.key_ok(key):
            return _no("no_key", f"a job on {settings.holder_possessive()} phone is the phone agent's: "
                       f"hand {settings.agent_name()} the task")
        live = self.live_on(device)
        if live:
            return _no("busy", f"the {device} already has a job: {live['task']} ({live['agent']})",
                       job=self.view(live))
        d = self.devices()
        device_id = d.get(device) or ""
        dev = d["devices"].get(device_id)
        if (not dev or dev.get("kind") != device
                or not self._device_approved(d, device_id, dev)):
            return _no("no_device", f"no {device} has registered a driver yet")
        if not dev.get("driver_on"):
            return _no("driver_off", DRIVER_OFF())
        now = self.clock()
        job_cap = secrets.token_urlsafe(24)
        job = {"id": time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-" + secrets.token_hex(3),
               "device": device, "device_id": d[device], "task": task, "agent": agent,
               "state": "waking", "outcome": None, "detail": f"waking the {device}",
               "created": now, "fetched": None, "started": None, "ended": None,
               "last_cmd": now, "last_poll": None, "last_action": "", "last_shot": None,
               "prompt": None, "your_turn": None, "by": None, "pushes": [], "waiting_since": None,
               "paused_since": None, "told": False, "cap_sha256": _secret_hash(job_cap)}
        self.jobs[job["id"]] = job
        self._log(job, {"kind": "start", "device": device, "task": task, "agent": agent})
        self.sweep_maybe()
        await self._push(job, dev)
        while job["state"] in ("waking", "starting"):
            await asyncio.sleep(self.t["tick"])
            self.tidy()
            if (job["state"] == "waking" and len(job["pushes"]) < 2
                    and self.clock() - job["created"] >= self.t["pickup_resend_s"]):
                await self._push(job, dev)
        if job["state"] == "ended":
            return _no(job["outcome"] or "ended", job["detail"], job=self.view(job))
        return {"ok": True, "text": job["detail"], "job": self.view(job), "job_cap": job_cap}

    async def _push(self, job: dict, dev: dict) -> None:
        try:
            # The push is a network call (FCM), so it waits in a thread, never on agent-companion's event loop.
            got = str(await asyncio.to_thread(self.push, dev, job["id"]))
        except Exception as e:  # noqa: BLE001 - a push that raised has failed; the resend may land
            got = f"push: {type(e).__name__}"
        job["pushes"].append(self.clock())
        self._log(job, {"kind": "push", "result": got[:80]})
        if got == "gone" and job["state"] == "waking":
            self.end_job(job, "failed", f"the {job['device']}'s push address is gone: "
                         f"open the {settings.app_name()} app on it once")
        else:
            self._save(job)

    def _job_cap_ok(self, job: dict, cap: str) -> bool:
        want = job.get("cap_sha256")
        return (isinstance(want, str) and bool(want) and isinstance(cap, str) and bool(cap)
                and hmac.compare_digest(want, _secret_hash(cap)))

    async def command(self, job_id: str, cmd_id: str, verb: str, args: dict, key: str = "",
                      cap: str | None = None) -> dict:
        """One command to the device, answered within cmd_wait_s. A cmd_id
        already answered is answered again from memory, never run twice."""
        self.tidy()
        job = self.jobs.get(job_id) or self._read_job(job_id)
        if job is None:
            return _no("no_job", f"no job {job_id}")
        if job["device"] == "phone" and not self.key_ok(key):
            return _no("no_key", f"a job on {settings.holder_possessive()} phone is the phone agent's")
        if cap is not None and not self._job_cap_ok(job, cap):
            return _no("no_cap", "this job's capability is missing or does not match")
        if (job["id"], cmd_id) in self.results:
            return self.results[(job["id"], cmd_id)]
        if job["state"] == "ended":
            return _no("ended", f"the job is over: {job['detail']}", job=self.view(job))
        if not CMD_RE.match(cmd_id or ""):
            return _no("bad_args", "cmd_id is 6 to 40 letters, digits, dashes or underscores")
        args = args if isinstance(args, dict) else {}
        why = args_error(verb, args)
        if why:
            return _no("bad_args", why)
        if job["state"] in ("paused", "waiting") and verb != "wait":
            return _no(job["state"], self.waiting_words(job), job=self.view(job))
        cur = self.pending.get(job_id) or self.inflight.get(job_id)
        if cur and cur["cmd_id"] != cmd_id:
            return _no("busy", f"one command at a time: {cur['verb']} has not come back yet")
        now = self.clock()
        job["last_cmd"] = now
        if not cur:
            self.pending[job_id] = {"cmd_id": cmd_id, "verb": verb, "args": args, "at": now}
            self._log(job, {"kind": "cmd", "cmd_id": cmd_id, "verb": verb, "args": args})
        self._save(job)
        deadline = now + self.t["cmd_wait_s"]
        while self.clock() < deadline:
            await asyncio.sleep(self.t["tick"])
            if (job["id"], cmd_id) in self.results:
                return self.results[(job["id"], cmd_id)]
            if job["state"] in ("paused", "waiting") and verb != "wait" \
                    and self.pending.get(job_id, {}).get("cmd_id") == cmd_id:
                # Paused (or waiting) before the device took it: it never runs,
                # and the agent hears why at once, with the job's own code.
                self.pending.pop(job_id, None)
                return _no(job["state"], self.waiting_words(job), job=self.view(job))
            self.tidy()
            if job["state"] == "ended":
                return _no("ended", f"the job is over: {job['detail']}", job=self.view(job))
        if self.pending.get(job_id, {}).get("cmd_id") == cmd_id:
            self.pending.pop(job_id, None)
            code = job["state"] if job["state"] in ("paused", "waiting") else "waiting"
            return _no(code, self.waiting_words(job), job=self.view(job))
        return _no("timeout", f"no answer in {self.t['cmd_wait_s']:.0f} seconds: send the same command again "
                   "for its answer, or look", job=self.view(job))

    def waiting_words(self, job: dict) -> str:
        st = job["state"]
        if st == "waiting":
            return job["detail"]
        if st == "paused":
            return job["detail"] + f"; the job goes on when {settings.holder()} taps Resume"
        if st in ("waking", "starting"):
            return f"the {job['device']} has not started the job yet"
        return f"the {job['device']} has not taken the command"

    def end(self, job_id: str, key: str = "", cap: str | None = None) -> dict:
        """The agent is done: the device gets its stop and gives the phone back."""
        job = self.jobs.get(job_id) or self._read_job(job_id)
        if job is None:
            return _no("no_job", f"no job {job_id}")
        if job["device"] == "phone" and not self.key_ok(key):
            return _no("no_key", f"a job on {settings.holder_possessive()} phone is the phone agent's")
        if cap is not None and not self._job_cap_ok(job, cap):
            return _no("no_cap", "this job's capability is missing or does not match")
        if job["state"] != "ended":
            self.end_job(job, "done", f"{job['agent']} ended the job")
        return {"ok": True, "text": job["detail"], "job": self.view(job)}

    # --- the device's side ----------------------------------------------------------
    async def next(self, device_id: str, gone=None, holds: str = "", running: str = "",
                   secret: str | None = None) -> dict:
        """The device's long poll: its job, a command, a stop, or idle after
        poll_hold_s. A poll whose device has hung up (`gone()` true: its app
        restarted, its connection died) takes nothing; what it would have
        taken waits for the device's next poll."""
        if secret is not None:
            allowed = self._device_secret(device_id, secret)
            if not allowed["ok"]:
                return allowed
        d = self.devices()
        rec = d["devices"].get(device_id)
        if not isinstance(rec, dict):
            return {"type": "unknown"}
        deadline = self.clock() + self.t["poll_hold_s"]
        if not self._device_approved(d, device_id, rec):
            # A pending phone takes no work; it waits out the poll like an idle
            # one, so it does not ask again at once in a tight loop.
            while not (gone is not None and gone()) and self.clock() < deadline:
                await asyncio.sleep(self.t["tick"])
            return {"type": "idle"}
        while True:
            self.tidy()
            if gone is not None and gone():
                return {"type": "idle"}
            out = self._next_for(device_id, holds, running)
            if out is not None:
                return out
            if self.clock() >= deadline:
                return {"type": "idle"}
            await asyncio.sleep(self.t["tick"])

    def _next_for(self, device_id: str, holds: str, running: str = "") -> dict | None:
        holds = str(holds or "")
        if not JOB_RE.match(holds):
            holds = ""
        if holds:
            held = self.jobs.get(holds) or self._read_job(holds)
            if held is None or held.get("device_id") != device_id:
                return {"type": "stop", "job": holds, "why": "unknown",
                        "text": f"{settings.app_name()} has no such job"}
            if held.get("state") == "ended":
                if not held.get("told"):
                    held["told"] = True
                    self._save(held)
                return {"type": "stop", "job": held["id"], "why": held["outcome"], "text": held["detail"]}
        running = str(running or "")
        if running and not CMD_RE.match(running):
            # Any poll that says `running` is a watch (Codex's review): a
            # malformed one is answered idle and never takes a job or command.
            return None
        if running and CMD_RE.match(running):
            # A watch (added in step 4b): the device runs command `running` of the
            # job it holds and asks only to hear a stop, which the checks above
            # give. It never takes a job or a command, and the command it runs is
            # not sent again: it has it.
            held = self.jobs.get(holds) if holds else None
            if held is not None:
                held["last_poll"] = self.clock()
            return None
        now = self.clock()
        for job in list(self.jobs.values()):
            if job["device_id"] != device_id:
                continue
            if job["state"] == "ended":
                if not job["told"]:
                    job["told"] = True
                    self._save(job)
                    return {"type": "stop", "job": job["id"], "why": job["outcome"], "text": job["detail"]}
                continue
            job["last_poll"] = now
            if job["state"] == "waking":
                job.update(state="starting", fetched=now, detail=f"the {job['device']} has the job")
                self._log(job, {"kind": "fetched"})
                self._save(job)
                return {"type": "job", "job": {k: job[k] for k in ("id", "task", "agent", "device")}}
            if job["state"] == "starting" and holds != job["id"]:
                self._log(job, {"kind": "again", "what": "job"})
                self._save(job)
                return {"type": "job", "job": {k: job[k] for k in ("id", "task", "agent", "device")}}
            if job["state"] == "running" and job["id"] in self.pending:
                cmd = self.pending.pop(job["id"])
                self.inflight[job["id"]] = cmd
                return {"type": "cmd", "job": job["id"], "cmd": {k: cmd[k] for k in ("cmd_id", "verb", "args")}}
            if job["state"] == "running" and job["id"] in self.inflight:
                # The device asks only between commands, so one still in flight was
                # lost on its way down or its answer on the way up: it goes out again,
                # and the device answers a cmd_id it has run from its kept results.
                cmd = self.inflight[job["id"]]
                self._log(job, {"kind": "again", "cmd_id": cmd["cmd_id"]})
                return {"type": "cmd", "job": job["id"], "cmd": {k: cmd[k] for k in ("cmd_id", "verb", "args")}}
        return None

    def result(self, body: dict, secret: str | None = None) -> dict:
        """Keep an answer only for a command handed to the device. After a
        agent-companion restart the in-flight command is lost; the agent sends the
        same cmd_id again and the device answers it from its own kept results,
        so nothing runs twice."""
        device_id = str(body.get("device_id") or "")
        if secret is not None:
            allowed = self._device_secret(device_id, secret)
            if not allowed["ok"]:
                return allowed
        job = self.jobs.get(str(body.get("job") or ""))
        if not job or job["device_id"] != device_id:
            return _no("not_yours", "no such job on this device")
        cmd_id = str(body.get("cmd_id") or "")
        if (job["id"], cmd_id) in self.results:
            return {"ok": True, "duplicate": True}
        cmd = self.inflight.get(job["id"])
        if not cmd or cmd["cmd_id"] != cmd_id:
            return _no("not_asked", "no such command in flight: the agent sends it again if it still wants it")
        self.inflight.pop(job["id"], None)
        res = body.get("result") if isinstance(body.get("result"), dict) else {}
        reply = self._keep(job, cmd_id, res)
        self.results[(job["id"], cmd_id)] = reply
        job["last_poll"] = self.clock()
        verb = cmd.get("verb") or "?"
        action = f"{verb}: {reply['target']}" if reply["target"] else verb
        job["last_action"] = (action[:200] + ("" if reply["ok"] else f" ({reply['code']})"))
        self._log(job, {"kind": "result", "cmd_id": cmd_id, "verb": verb, "ok": reply["ok"],
                        "code": reply["code"], "target": reply["target"]})
        self._save(job)
        return {"ok": True}

    def _keep(self, job: dict, cmd_id: str, res: dict) -> dict:
        """The device's answer as the agent gets it; the screen's words and the
        picture go to looks/ and shots/ (swept), never into actions.jsonl."""
        d = self._dir(job["id"])
        ok = res.get("ok") is True
        text = str(res.get("text") or "")[:TEXT_MAX]
        reply = {"ok": ok, "code": "" if ok else str(res.get("code") or "failed")[:24], "text": text,
                 "target": str(res.get("target") or "")[:300]}
        named = bool(CMD_RE.match(cmd_id))
        if text and named:
            _write_bytes(d / "looks" / f"{cmd_id}.txt", text.encode())
        png = res.get("png")
        if isinstance(png, str) and png and named:
            try:
                raw = base64.b64decode(png, validate=True)
            except (binascii.Error, ValueError):
                raw = b""
            if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) <= SHOT_MAX:
                p = d / "shots" / f"{cmd_id}.png"
                _write_bytes(p, raw)
                reply["shot"] = str(p)
                job["last_shot"] = f"shots/{cmd_id}.png"
            else:
                reply.update(ok=False, code="failed",
                             text=(text + "\n" if text else "") + "the screenshot did not arrive whole")
        return reply

    def event(self, body: dict, secret: str | None = None) -> dict:
        device_id = str(body.get("device_id") or "")
        if secret is not None:
            allowed = self._device_secret(device_id, secret)
            if not allowed["ok"]:
                return allowed
        job = self.jobs.get(str(body.get("job") or ""))
        if not job or job["device_id"] != device_id:
            return _no("not_yours", "no such job on this device")
        kind = str(body.get("kind") or "")
        if kind not in EVENTS:
            return _no("bad_args", f"no such event {kind!r}")
        now = self.clock()
        job["last_poll"] = now
        what = str(body.get("what") or "")[:300]
        row = {"kind": "event", "event": kind}
        if job["state"] == "ended":
            self._log(job, {**row, "late": True})
            return {"ok": True, "ended": True}
        if kind == "started":
            job.update(state="running", started=job["started"] or now, detail=f"driving the {job['device']}",
                       waiting_since=None, last_cmd=now)
        elif kind == "locked":
            since = job["waiting_since"] or now
            job.update(state="waiting", waiting_since=since,
                       detail=f"{settings.holder_possessive()} phone is locked: waiting for {settings.holder()} to unlock it (until "
                              f"{_hm(since + self.t['lock_wait_s'])})")
        elif kind == "call_wait":
            job.update(state="waiting", waiting_since=job["waiting_since"] or now,
                       detail=f"{settings.holder()} is in a call: the job starts when it ends")
        elif kind == "prompt":
            job["prompt"] = {"cmd_id": str(body.get("cmd_id") or "")[:40], "what": what, "at": now}
            row.update(cmd_id=job["prompt"]["cmd_id"], what=what)
        elif kind == "answer":
            answer = str(body.get("answer") or "")
            row.update(cmd_id=str(body.get("cmd_id") or "")[:40],
                       answer=answer if answer in ("allow", "deny", "timeout") else "deny",
                       what=(job.get("prompt") or {}).get("what") or what)
            job.update(prompt=None, last_cmd=now)
        elif kind == "your_turn":
            job["your_turn"] = {"what": what or "a password or code", "at": now}
            row.update(what=what)
        elif kind == "continue":
            job.update(your_turn=None, last_cmd=now)
        elif kind == "takeover":
            why = "call" if body.get("why") == "call" else "touch"
            job.update(state="paused", paused_since=job.get("paused_since") or now,
                       detail=f"paused: {settings.holder()} is in a call" if why == "call"
                       else f"paused: {settings.holder()} is using the phone")
            row.update(why=why)
        elif kind == "resume":
            job.update(state="running", detail=f"driving the {job['device']}", last_cmd=now, paused_since=None)
        elif kind == "stop":
            by = str(body.get("by") or "")
            by = by if by in STOPPED_BY else "phone"
            self._log(job, {**row, "by": by})
            self.end_job(job, "stopped", f"{settings.holder()} stopped it ({by})", by=by, told=True)
            return {"ok": True}
        elif kind == "end":
            outcome = str(body.get("outcome") or "")
            outcome = outcome if outcome in DEVICE_ENDS else "failed"
            text = str(body.get("text") or "")[:300] or ENDED_WORDS()[outcome]
            self._log(job, {**row, "outcome": outcome, "text": text})
            self.end_job(job, outcome, text, told=True)
            return {"ok": True}
        elif kind == "driver_off":
            self._log(job, row)
            self.end_job(job, "failed", DRIVER_OFF(), told=True)
            return {"ok": True}
        self._log(job, row)
        self._save(job)
        return {"ok": True}

    # --- endings and the lazy timers -------------------------------------------------
    def end_job(self, job: dict, outcome: str, detail: str, by: str | None = None, told: bool = False) -> None:
        if job["state"] == "ended":
            return
        never_had_it = job["state"] == "waking"
        job.update(state="ended", outcome=outcome, detail=detail, ended=self.clock(),
                   by=by or job.get("by"), told=told or never_had_it, prompt=None, your_turn=None)
        self.pending.pop(job["id"], None)
        self.inflight.pop(job["id"], None)
        self._log(job, {"kind": "end", "outcome": outcome, "detail": detail, **({"by": by} if by else {})})
        self._save(job)

    def stop(self, job_id: str) -> dict:
        """the device holder's Stop on the card. Only ever takes power away."""
        job = self.jobs.get(job_id)
        if job is None or job["state"] == "ended":
            return _no("no_job", "no live job to stop")
        self.end_job(job, "stopped", f"{settings.holder()} stopped it from {settings.app_name()}", by="card")
        return {"ok": True, "job": self.view(job)}

    def tidy(self) -> None:
        """The lazy timers, run by every call: nothing runs in the background."""
        now = self.clock()
        for job in list(self.jobs.values()):
            st = job["state"]
            if st == "ended":
                # An away device gave the phone back itself long ago; the job remains on disk.
                if now - (job["ended"] or now) > self.t["forget_s"]:
                    self.jobs.pop(job["id"], None)
                continue
            if st == "waking" and now - job["created"] >= self.t["pickup_give_up_s"]:
                self.end_job(job, "expired", NOT_ANSWERING[job["device"]])
            elif st == "starting" and now - (job["fetched"] or now) >= self.t["starting_s"]:
                self.end_job(job, "failed", "the driver took the job and never started it")
            elif st == "waiting" and now - (job["waiting_since"] or now) >= self.t["lock_wait_s"] + 60:
                self.end_job(job, "expired", f"{settings.holder()} did not hand the phone over in 15 minutes")
            elif st in ("running", "paused"):
                if job["last_poll"] and now - job["last_poll"] >= self.t["lost_s"]:
                    self.end_job(job, "lost", f"the {job['device']} stopped asking {settings.app_name()} for commands")
                elif st == "paused":
                    if now - (job.get("paused_since") or now) >= self.t["lock_wait_s"]:
                        self.end_job(job, "expired", f"{settings.holder()} did not hand the phone back in 15 minutes")
                elif job["your_turn"] and now - job["your_turn"]["at"] >= self.t["lock_wait_s"]:
                    self.end_job(job, "expired", f"{settings.holder()} did not take a turn in 15 minutes, so the job ended and the phone went back")
                elif job["prompt"] and now - job["prompt"]["at"] >= self.t["prompt_s"]:
                    # The device lets a prompt go after five minutes and says so; one
                    # that never said so is let go here, as a timeout.
                    self._log(job, {"kind": "event", "event": "answer", "cmd_id": job["prompt"]["cmd_id"],
                                    "answer": "timeout", "what": job["prompt"]["what"], "by": "relay"})
                    job.update(prompt=None, last_cmd=now)
                    self._save(job)
                elif (not job["prompt"] and not job["your_turn"]
                      and now - (job["last_cmd"] or now) >= self.t["stall_s"]):
                    self.end_job(job, "stalled", "no command for 5 minutes, so the job ended and the phone went back")
        if len(self.results) > RESULTS_MAX:
            for k in list(self.results)[:len(self.results) - RESULTS_MAX // 2]:
                self.results.pop(k, None)

    def sweep_maybe(self) -> None:
        now = self.clock()
        if now - self.swept_at >= self.t["sweep_every_s"]:
            self.swept_at = now
            sweep(self.data, now)

    # --- the device holder's side ------------------------------------------------------------------
    def listing(self, limit: int = 20) -> dict:
        self.tidy()
        live = [self.view(j) for j in self.jobs.values() if j["state"] != "ended"]
        history = []
        for p in sorted((self.data / "jobs").glob("*/job.json"), reverse=True):
            if len(history) >= limit:
                break
            job = self._read_job(p.parent.name)
            if job and job.get("state") == "ended":
                history.append(self.view(job))
        d = self.devices()
        devices = {}
        for kind in DEVICES:
            device_id = d.get(kind) or ""
            rec = d["devices"].get(device_id)
            if rec and rec.get("kind") == kind and self._device_approved(d, device_id, rec):
                devices[kind] = {k: rec.get(k) for k in ("model", "android", "driver_on", "app_version", "seen")}
        pending = []
        for device_id, rec in d["devices"].items():
            if isinstance(rec, dict) and rec.get("kind") == "phone" and not self._device_approved(d, device_id, rec):
                pending.append({"device_id": device_id,
                                **{k: rec.get(k) for k in ("model", "android", "app_version", "registered")}})
        return {"live": live, "history": history, "devices": devices, "pending": pending}

    def detail(self, job_id: str) -> dict | None:
        job = self.jobs.get(job_id) or self._read_job(job_id)
        if job is None:
            return None
        try:
            lines = (self._dir(job_id) / "actions.jsonl").read_text().splitlines()[-300:]
            actions = [json.loads(x) for x in lines if x.strip()]
        except (OSError, ValueError):
            actions = []
        return {**self.view(job), "actions": actions}

    def shot_path(self, job_id: str) -> Path | None:
        job = self.jobs.get(job_id) or self._read_job(job_id)
        rel = (job or {}).get("last_shot")
        if not rel:
            return None
        p = self._dir(job["id"]) / rel
        return p if p.is_file() else None
