"""Turn a long-unanswered ask into one alert while the owner is awake.

The decision functions are pure except for their small state files. ``Watcher``
is the only background worker and never starts while pytest is imported.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from . import addons, config, notify


def _data_dir() -> Path:
    return Path(str(config.get("data_dir") or Path.home() / ".local" / "share" / "agent-companion")).expanduser()


STATE = _data_dir() / "ring.json"
KEEP_RUNG_S = 2 * 86400
KEEP_DAYS = 7
EVERY_S = 30
RING_KEEP_S = 3600
# A ring's push lives 60 s and the phone gives the fetch two 6 s tries, so a
# ring nobody fetched 90 s after it was sent can no longer ring.
LAND_S = 90
_KEYS_LOCK = threading.Lock()


def _setting_int(key: str, default: int) -> int:
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def ring_after_s() -> int:
    return _setting_int("ring_after_min", 30) * 60


def gap_s() -> int:
    return _setting_int("ring_gap_min", 10) * 60


def day_max() -> int:
    return _setting_int("ring_day_max", 8)


def _keys_path(state_path: Path | None) -> Path:
    p = Path(state_path or STATE)
    return p.with_name(p.stem + "-keys.json")


def _read_keys(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def issue(ask_id: str, now: float, state_path: Path | None = None) -> str:
    """Mint a fresh one-time button key for an ask."""
    key = secrets.token_urlsafe(16)
    path = _keys_path(state_path)
    with _KEYS_LOCK:
        data = {k: value for k, value in _read_keys(path).items()
                if isinstance(value, dict) and now - float(value.get("at") or 0) <= KEEP_RUNG_S}
        data[ask_id] = {"n": key, "at": now}
        save(data, path)
    return key


def consume(ask_id: str, key: str, now: float | None = None, state_path: Path | None = None) -> bool:
    """Accept the button key once, only for the ask that received it."""
    now = time.time() if now is None else now
    path = _keys_path(state_path)
    with _KEYS_LOCK:
        data = _read_keys(path)
        entry = data.get(str(ask_id))
        ok = (isinstance(entry, dict) and isinstance(key, str) and bool(key)
              and secrets.compare_digest(str(entry.get("n") or ""), key)
              and now - float(entry.get("at") or 0) <= KEEP_RUNG_S)
        if ok:
            del data[str(ask_id)]
            save(data, path)
        return ok


def _empty() -> dict:
    return {"rung": {}, "last_at": 0.0, "days": {}}


def load(path: Path | None = None) -> dict:
    try:
        data = json.loads(Path(path or STATE).read_text())
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    try:
        return {
            "rung": {str(key): float(value) for key, value in (data.get("rung") or {}).items()
                     if isinstance(value, (int, float))},
            "last_at": float(data.get("last_at") or 0.0),
            "days": {str(key): int(value) for key, value in (data.get("days") or {}).items()
                     if isinstance(value, int)},
        }
    except (AttributeError, TypeError, ValueError):
        return _empty()


def save(state: dict, path: Path | None = None) -> None:
    p = Path(path or STATE)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(state, file)
    os.replace(tmp, p)


def _day(value: float) -> str:
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d")


def decide(pending: list[dict], state: dict, now: float, asleep: bool,
           private=lambda ask: False) -> list[dict]:
    """Return alertable asks, oldest first, subject to privacy and rate limits."""
    if asleep:
        return []
    ripe = [ask for ask in pending
            if now - float(ask.get("at") or now) >= ring_after_s()
            and ask.get("id") not in state["rung"] and not private(ask)]
    if not ripe or now - state["last_at"] < gap_s():
        return []
    if state["days"].get(_day(now), 0) >= day_max():
        return []
    return sorted(ripe, key=lambda ask: ask["at"])


def mark(state: dict, rung: list[dict], now: float) -> dict:
    out = {"rung": dict(state["rung"]), "last_at": now, "days": dict(state["days"])}
    for ask in rung:
        out["rung"][ask["id"]] = now
    out["rung"] = {key: value for key, value in out["rung"].items() if now - value <= KEEP_RUNG_S}
    today = _day(now)
    out["days"][today] = out["days"].get(today, 0) + 1
    keep = sorted(out["days"])[-KEEP_DAYS:]
    out["days"] = {key: out["days"][key] for key in keep}
    return out


def message(ask: dict, who: str, now: float) -> tuple[str, str]:
    mins = max(1, int((now - float(ask["at"])) // 60))
    title = f"{who} stuck {mins} min"
    if ask.get("questions"):
        question = ask["questions"][0]
        options = " · ".join(option.get("label", "") for option in question.get("options") or []
                               if option.get("label"))
        more = len(ask["questions"]) - 1
        body = (f"{question['question']}\n{options}" if options else question["question"]) + (
            f"\n(+{more} more question{'s' if more > 1 else ''})" if more else "")
    else:
        body = f"{notify.address('{you}, needs your OK.')} {ask.get('tool', '')}: {ask.get('what', '')}"
    return title, body[:400]


def actions(ask: dict) -> list[dict]:
    if ask.get("questions"):
        return [{"action": "answer", "title": "Answer"}, {"action": "call", "title": "Call"}]
    return [{"action": "allow", "title": "Allow"}, {"action": "deny", "title": "Deny"},
            {"action": "call", "title": "Call"}]


def _helper_root(ask: dict, rows: list[dict]) -> str:
    row = next((item for item in rows if item.get("session_id") == ask.get("session_id")), {})
    if not row.get("root_pid"):
        return ""
    root = next((item for item in rows if item.get("pid") == row.get("root_pid")), {})
    return str(root.get("speaker") or "")


def who_and_tap(ask: dict, rows: list[dict]) -> tuple[str, str, str]:
    """Return the alert name, chat URL and tag for an ask."""
    who = str(ask.get("speaker") or ask.get("project") or "An agent")
    name = str(ask.get("speaker") or "")
    root = _helper_root(ask, rows)
    if root:
        who, name = f"{root}'s helper", root
    url = f"/?agent={quote(name)}&ask={ask['id']}" if name else "/?view=room"
    return who, url, f"stuck-{name or ask['id']}"


def _private(ask: dict, rows: list[dict]) -> bool:
    """The live call's own test, git checkout included: a private project's
    worktree under another folder name never rings with its words."""
    from . import livecall
    cwd = next((row.get("cwd") or "" for row in rows if row.get("session_id") == ask.get("session_id")), "")
    return livecall.private_place(cwd, str(ask.get("project") or ""))


def _rings_path(state_path: Path | None) -> Path:
    p = Path(state_path or STATE)
    return p.with_name(p.stem + "-rings.json")


def new_ring(who: str, alerts: list[dict], now: float, state_path: Path | None = None,
             ask_ids=()) -> tuple[str, str]:
    rid, key = secrets.token_hex(8), secrets.token_urlsafe(16)
    path = _rings_path(state_path)
    with _KEYS_LOCK:
        data = {item_key: value for item_key, value in _read_keys(path).items()
                if isinstance(value, dict) and now - float(value.get("at") or 0) <= RING_KEEP_S}
        data[rid] = {"at": now, "who": who, "key": key, "alerts": alerts,
                     "asks": [str(ask_id) for ask_id in ask_ids]}
        save(data, path)
    return rid, key


def brief(rid: str, now: float | None = None, state_path: Path | None = None) -> dict | None:
    now = time.time() if now is None else now
    if not rid or not all(char in "0123456789abcdef" for char in rid):
        return None
    entry = _read_keys(_rings_path(state_path)).get(rid)
    if not isinstance(entry, dict) or now - float(entry.get("at") or 0) > RING_KEEP_S:
        return None
    first = (entry.get("alerts") or [{}])[0]
    return {"who": entry.get("who", ""), "title": first.get("title", ""), "body": first.get("body", ""),
            "answer_url": f"/?agent={quote(entry.get('who', ''))}&act=answer&r={rid}&rk={entry.get('key', '')}",
            "alerts": entry.get("alerts") or []}


def answer(rid: str, key: str, now: float | None = None, state_path: Path | None = None) -> str | None:
    now = time.time() if now is None else now
    path = _rings_path(state_path)
    with _KEYS_LOCK:
        data = _read_keys(path)
        entry = data.get(str(rid))
        ok = (isinstance(entry, dict) and isinstance(key, str) and bool(key) and bool(entry.get("key"))
              and secrets.compare_digest(str(entry["key"]), key)
              and now - float(entry.get("at") or 0) <= RING_KEEP_S)
        if not ok:
            return None
        entry["key"] = ""
        save(data, path)
        return str(entry.get("who") or "")


def ready(rid: str, now: float | None = None, state_path: Path | None = None) -> list[str] | None:
    """Return an answered ring's asks without spending its brief."""
    now = time.time() if now is None else now
    if not rid or not all(char in "0123456789abcdef" for char in rid):
        return None
    path = _rings_path(state_path)
    with _KEYS_LOCK:
        data = _read_keys(path)
        entry = data.get(rid)
        if (not isinstance(entry, dict) or entry.get("key") != "" or entry.get("claimed")
                or now - float(entry.get("at") or 0) > RING_KEEP_S):
            return None
        return [str(ask_id) for ask_id in entry.get("asks") or []]


def claim(rid: str, now: float | None = None, state_path: Path | None = None) -> list[str] | None:
    """Return a ring's asks once, after the ring's answer key was consumed."""
    now = time.time() if now is None else now
    if not rid or not all(char in "0123456789abcdef" for char in rid):
        return None
    path = _rings_path(state_path)
    with _KEYS_LOCK:
        data = _read_keys(path)
        entry = data.get(rid)
        if (not isinstance(entry, dict) or entry.get("key") != "" or entry.get("claimed")
                or now - float(entry.get("at") or 0) > RING_KEEP_S):
            return None
        entry["claimed"] = now
        save(data, path)
        return [str(ask_id) for ask_id in entry.get("asks") or []]


def release(rid: str, state_path: Path | None = None) -> None:
    """Undo a claim whose call did not open, so the brief is not lost."""
    if not rid or not all(char in "0123456789abcdef" for char in rid):
        return
    path = _rings_path(state_path)
    with _KEYS_LOCK:
        data = _read_keys(path)
        entry = data.get(rid)
        if isinstance(entry, dict) and entry.pop("claimed", None) is not None:
            save(data, path)


def _hex(rid: str) -> bool:
    return bool(rid) and all(char in "0123456789abcdef" for char in rid)


def fetched(rid: str, now: float | None = None, state_path: Path | None = None) -> None:
    """Note that the phone asked for this ring's brief, so it has the ring or its alerts."""
    now = time.time() if now is None else now
    if not _hex(rid):
        return
    path = _rings_path(state_path)
    with _KEYS_LOCK:
        data = _read_keys(path)
        entry = data.get(rid)
        if isinstance(entry, dict) and not entry.get("fetched"):
            entry["fetched"] = now
            save(data, path)


def _spent(rid: str, now: float, state_path: Path | None = None) -> None:
    """Mark a ring done: its alerts went another way, so it must not fall back."""
    path = _rings_path(state_path)
    with _KEYS_LOCK:
        data = _read_keys(path)
        entry = data.get(rid)
        if isinstance(entry, dict):
            entry["fell"] = now
            save(data, path)


def fall_back(pending: list[dict], send, now: float | None = None, state_path: Path | None = None) -> int:
    """Send the alerts of every ring that can no longer ring; return how many went."""
    now = time.time() if now is None else now
    path = _rings_path(state_path)
    waiting = {str(item.get("id")) for item in pending}
    with _KEYS_LOCK:
        data = _read_keys(path)
    due = [(rid, entry) for rid, entry in data.items()
           if isinstance(entry, dict) and LAND_S <= now - float(entry.get("at") or 0) <= RING_KEEP_S
           and not entry.get("fetched") and not entry.get("fell") and entry.get("key")]
    count = 0
    for rid, entry in due:
        alerts, asks = entry.get("alerts") or [], entry.get("asks") or []
        before = set(entry.get("sent") or [])
        done = set(before)
        for i, alert in enumerate(alerts):
            if i in done:
                continue
            if i < len(asks) and str(asks[i]) not in waiting:
                continue    # answered, withdrawn, or out of sight just now: not this pass
            if send(alert.get("title", ""), alert.get("body", ""), alert.get("url", ""),
                    alert.get("tag", ""), alert.get("actions") or []):
                done.add(i)
                count += 1
        if done == before and len(done) < len(alerts):
            continue        # nothing new for this ring: write nothing, look again next pass
        with _KEYS_LOCK:
            data = _read_keys(path)
            record = data.get(rid)
            if isinstance(record, dict):
                record["sent"] = sorted(done)
                if len(done) >= len(alerts):
                    record["fell"] = now
                save(data, path)
    return count


def tick(pending: list[dict], rows: list[dict], asleep: bool | None, send,
         state_path: Path | None = None, now: float | None = None, ring_send=None) -> int:
    now = time.time() if now is None else now
    asleep = addons.asleep() if asleep is None else asleep
    state = load(state_path)
    ripe = decide(pending, state, now, asleep, private=lambda ask: _private(ask, rows))
    if not ripe:
        return 0
    alerts, whos = [], []
    for ask in ripe:
        who, url, tag = who_and_tap(ask, rows)
        title, body = message(ask, who, now)
        if url.startswith("/?agent="):
            url += f"&n={issue(ask['id'], now, state_path)}"
        alerts.append({"title": title, "body": body, "url": url, "tag": tag, "actions": actions(ask)})
        whos.append(tag[len("stuck-"):] if tag.startswith("stuck-") else who)
    went = []
    if ring_send is not None:
        rid, _ = new_ring(whos[0], alerts, now, state_path, ask_ids=[ask["id"] for ask in ripe])
        if ring_send(rid):
            went = list(ripe)
        else:
            _spent(rid, now, state_path)    # the push never left: the alerts go now, not again later
    if not went:
        for ask, alert in zip(ripe, alerts):
            if send(alert["title"], alert["body"], alert["url"], alert["tag"], alert["actions"]):
                went.append(ask)
    if went:
        save(mark(state, went, now), state_path)
    return len(went)


class Watcher:
    """Run ``tick`` every ``EVERY_S`` seconds, never under pytest."""

    def __init__(self, pending, rows, asleep, send, ring_send=None):
        self.pending, self.rows, self.asleep, self.send = pending, rows, asleep, send
        self.ring_send = ring_send
        self.thread = None

    def start(self) -> None:
        if "pytest" in sys.modules:
            return
        self.thread = threading.Thread(target=self._run, name="ring", daemon=True)
        self.thread.start()

    def once(self, now: float | None = None) -> tuple[int, int]:
        """One pass: alerts for rings the phone never fetched, then new rings."""
        pending = self.pending()
        try:
            fell = fall_back(pending, self.send, now)
        except Exception as exc:  # noqa: BLE001 - a failed fall back never stops the ring
            print(f"ring: fall back: {type(exc).__name__}: {exc}"[:200], flush=True)
            fell = 0
        asleep = self.asleep() if self.asleep is not None else None
        rung = tick(pending, self.rows(), asleep, self.send, now=now, ring_send=self.ring_send)
        return fell, rung

    def _run(self) -> None:
        while True:
            try:
                fell, count = self.once()
                if count:
                    print(f"ring: {count} stuck ask(s) alerted", flush=True)
                if fell:
                    print(f"ring: {fell} alert(s) sent for a ring the phone never fetched", flush=True)
            except Exception as exc:  # noqa: BLE001 - a bad pass never ends the watcher
                print(f"ring: {type(exc).__name__}: {exc}"[:200], flush=True)
            time.sleep(EVERY_S)
