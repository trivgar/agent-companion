"""Real rate-limit utilisation from Anthropic's OAuth usage endpoint.

This is the ONE thing in this project that touches the network. Everything else
reads local files. It is here because the 5-hour and 7-day percentages exist
nowhere on disk -- not in transcripts, not in stats-cache.json -- and the
percentage is the number that answers "am I about to be cut off", which spend
cannot.

    GET https://api.anthropic.com/api/oauth/usage
    Authorization: Bearer <claudeAiOauth.accessToken from ~/.claude/.credentials.json>
    -> {"five_hour": {"utilization": N, "resets_at": ...},
        "seven_day":  {"utilization": N, "resets_at": ...}}

It is the same call the statusline already makes on this machine, with the same
credential, so it adds no exposure that was not already there.

THE ENDPOINT RATE-LIMITS. A first bare call returned 429. So: one fetch at most
every MIN_INTERVAL, the answer cached on disk across restarts, and on any
failure the last good answer is served and marked stale rather than retried in
a loop. A dashboard polling every 5s must never become a source of 429s.

The token is read at call time and never logged, never cached by us, and never
returned through the API. ``claude_limits = false`` in the settings turns all
of this off: no token is read and no call is made.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"
CACHE_NAME = "session-companion-limits.json"
MIN_INTERVAL = 300.0          # 5 minutes between live fetches
TIMEOUT = 6.0


def enabled() -> bool:
    """False when the owner set ``claude_limits`` to false, 0, no or off."""
    value = config.get("claude_limits", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off")
    return bool(value)


def credentials_path(home: Path) -> Path:
    return Path(home) / ".credentials.json"


def read_token(home: Path) -> str | None:
    """The Claude Code OAuth access token, or None. Never logged."""
    try:
        data = json.loads(credentials_path(home).read_text())
    except (OSError, ValueError):
        return None
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    return token if isinstance(token, str) and token else None


def token_expired(home: Path, now: float | None = None) -> bool:
    """expiresAt is milliseconds. A stale token gives 401, so say so instead."""
    try:
        data = json.loads(credentials_path(home).read_text())
        expires = data["claudeAiOauth"]["expiresAt"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if not isinstance(expires, (int, float)):
        return False
    return (now or time.time()) * 1000 >= expires


def parse(payload: object) -> dict:
    """Pull the two windows out. Defensive: this is an undocumented endpoint."""
    out: dict = {}
    if not isinstance(payload, dict):
        return out
    for key in ("five_hour", "seven_day"):
        window = payload.get(key)
        if not isinstance(window, dict):
            continue
        util = window.get("utilization")
        out[key] = {
            "utilization": util if isinstance(util, (int, float)) else None,
            "resets_at": window.get("resets_at"),
        }
    return out


def fetch(token: str, url: str = USAGE_URL) -> tuple[dict | None, str]:
    """(parsed, error). Never raises -- a dashboard must not fail on this."""
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": BETA_HEADER,
        "User-Agent": "session-companion",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return parse(json.loads(r.read())), ""
    except urllib.error.HTTPError as exc:
        # 429 is expected under load; the caller serves its cache.
        return None, {429: "rate-limited by Anthropic, will retry",
                      401: "Claude Code's sign-in has expired; run claude to sign in again",
                      403: "the usage check was refused for this account"}.get(exc.code, f"HTTP {exc.code}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, type(exc).__name__


class LimitsCache:
    """Last known utilisation, refreshed at most every MIN_INTERVAL."""

    def __init__(self, home: Path, cache_path: Path | None = None) -> None:
        self.home = Path(home)
        self.cache_path = Path(cache_path) if cache_path else self.home / CACHE_NAME
        self._data: dict = {}
        self._fetched_at = 0.0
        self._tried_at = 0.0        # the last ask, good or not: a failure waits MIN_INTERVAL too
        self._error = ""
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            self._data = raw.get("data") or {}
            self._fetched_at = float(raw.get("fetched_at") or 0)

    def _save(self) -> None:
        # One temp file per process: two writers sharing one name interleaved
        # their bytes, and a stray "}" left the cache unreadable (2026-10-28).
        tmp = self.cache_path.with_suffix(f".json.{os.getpid()}.sc-tmp")
        try:
            tmp.write_text(json.dumps({"data": self._data,
                                       "fetched_at": self._fetched_at}))
            os.replace(tmp, self.cache_path)
            os.chmod(self.cache_path, 0o600)
        except OSError:
            pass

    def get(self, now: float | None = None, force: bool = False) -> dict:
        if not enabled():
            return {"available": False, "reason": "off"}
        now = now or time.time()
        age = now - self._fetched_at
        if force or (age >= MIN_INTERVAL and now - self._tried_at >= MIN_INTERVAL
                     and not token_expired(self.home, now)):
            self._tried_at = now
            token = read_token(self.home)
            if token:
                data, error = fetch(token)
                self._error = error
                if data:
                    self._data = data
                    self._fetched_at = now
                    self._save()
                    age = 0.0
            else:
                self._error = "no credentials"

        if not self._data:
            return {"available": False, "reason": self._error or "never fetched"}

        result = {"available": True, "age_seconds": round(age, 1),
                  "stale": age >= MIN_INTERVAL * 2}
        if self._error:
            result["last_error"] = self._error
        for key, window in self._data.items():
            resets_at = window.get("resets_at")
            result[key] = {
                "utilization": window.get("utilization"),
                "resets_at": resets_at,
                "resets_in_seconds": _seconds_until(resets_at, now),
            }
        return result


def _seconds_until(resets_at: object, now: float) -> float | None:
    """resets_at may be an epoch (s or ms) or an ISO string."""
    if isinstance(resets_at, (int, float)):
        ts = float(resets_at)
        if ts > 1e11:          # milliseconds
            ts /= 1000.0
        return max(0.0, round(ts - now, 1))
    if isinstance(resets_at, str) and resets_at:
        try:
            from datetime import datetime
            ts = datetime.fromisoformat(resets_at.replace("Z", "+00:00")).timestamp()
            return max(0.0, round(ts - now, 1))
        except ValueError:
            return None
    return None
