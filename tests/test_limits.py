import json
import time

import pytest

from agent_companion import limits


def _creds(tmp_path, token="tok-abc", expires_ms=None):
    home = tmp_path / ".claude"
    home.mkdir(exist_ok=True)
    expires = expires_ms if expires_ms is not None else int((time.time() + 3600) * 1000)
    (home / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": token, "expiresAt": expires}}))
    return home


# --- credentials -----------------------------------------------------------

@pytest.mark.parametrize("value", ["false", "0", "no", "off", "False"])
def test_claude_limits_off_reads_no_token_and_calls_nothing(monkeypatch, tmp_path, value):
    home = _creds(tmp_path)
    monkeypatch.setenv("AGENT_COMPANION_CLAUDE_LIMITS", value)
    monkeypatch.setattr(limits, "read_token", lambda h: pytest.fail("read the token"))
    monkeypatch.setattr(limits, "fetch", lambda t, url=limits.USAGE_URL: pytest.fail("called Anthropic"))

    assert limits.LimitsCache(home, tmp_path / "cache.json").get(force=True) == {"available": False, "reason": "off"}


def test_claude_limits_is_on_by_default(monkeypatch, tmp_path):
    for name in ("AGENT_COMPANION_CLAUDE_LIMITS",):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(limits, "fetch",
                        lambda t, url=limits.USAGE_URL: ({"five_hour": {"utilization": 3}}, ""))

    assert limits.enabled() is True
    assert limits.LimitsCache(_creds(tmp_path), tmp_path / "cache.json").get(force=True)["five_hour"]["utilization"] == 3


def test_reads_the_oauth_token(tmp_path):
    assert limits.read_token(_creds(tmp_path)) == "tok-abc"


def test_no_credentials_is_none_not_an_error(tmp_path):
    assert limits.read_token(tmp_path / "nope") is None


def test_expired_token_is_detected(tmp_path):
    home = _creds(tmp_path, expires_ms=int((time.time() - 60) * 1000))
    assert limits.token_expired(home) is True


# --- parsing an undocumented endpoint --------------------------------------

def test_parses_both_windows():
    out = limits.parse({"five_hour": {"utilization": 7, "resets_at": 123},
                        "seven_day": {"utilization": 3, "resets_at": 456}})
    assert out["five_hour"]["utilization"] == 7
    assert out["seven_day"]["resets_at"] == 456


def test_garbage_payloads_do_not_raise():
    for bad in (None, [], "nope", {"five_hour": "x"}, {}):
        assert isinstance(limits.parse(bad), dict)


def test_non_numeric_utilization_becomes_none():
    assert limits.parse({"five_hour": {"utilization": "7%"}})["five_hour"]["utilization"] is None


# --- reset countdown -------------------------------------------------------

def test_reset_from_epoch_seconds():
    now = time.time()
    assert limits._seconds_until(now + 600, now) == 600.0


def test_reset_from_epoch_milliseconds():
    now = time.time()
    assert limits._seconds_until((now + 600) * 1000, now) == 600.0


def test_reset_from_iso_string():
    from datetime import datetime, timezone
    now = time.time()
    iso = datetime.fromtimestamp(now + 300, timezone.utc).isoformat().replace("+00:00", "Z")
    assert 299 <= limits._seconds_until(iso, now) <= 301


def test_past_reset_clamps_to_zero():
    now = time.time()
    assert limits._seconds_until(now - 500, now) == 0.0


# --- the cache -------------------------------------------------------------

def test_reports_unavailable_without_credentials(tmp_path):
    c = limits.LimitsCache(tmp_path / "nope", tmp_path / "cache.json")
    assert c.get()["available"] is False


def test_serves_cache_when_the_fetch_fails(monkeypatch, tmp_path):
    """429 is expected from this endpoint; the last good answer must survive."""
    home = _creds(tmp_path)
    cache = tmp_path / "cache.json"
    monkeypatch.setattr(limits, "fetch",
                        lambda t, url=limits.USAGE_URL: ({"five_hour": {"utilization": 7}}, ""))
    c = limits.LimitsCache(home, cache)
    assert c.get(force=True)["five_hour"]["utilization"] == 7

    monkeypatch.setattr(limits, "fetch", lambda t, url=limits.USAGE_URL: (None, "rate-limited by Anthropic, will retry"))
    again = c.get(force=True)
    assert again["available"] is True
    assert again["five_hour"]["utilization"] == 7
    assert again["last_error"] == "rate-limited by Anthropic, will retry"


def test_does_not_refetch_inside_the_interval(monkeypatch, tmp_path):
    """A 5s dashboard poll must not become a source of 429s."""
    home = _creds(tmp_path)
    calls = []
    monkeypatch.setattr(limits, "fetch",
                        lambda t, url=limits.USAGE_URL: (calls.append(1) or ({"five_hour": {"utilization": 1}}, "")))
    c = limits.LimitsCache(home, tmp_path / "cache.json")
    now = time.time()
    for _ in range(20):
        c.get(now=now)
    assert len(calls) == 1


def test_refetches_after_the_interval(monkeypatch, tmp_path):
    home = _creds(tmp_path)
    calls = []
    monkeypatch.setattr(limits, "fetch",
                        lambda t, url=limits.USAGE_URL: (calls.append(1) or ({"five_hour": {"utilization": 1}}, "")))
    c = limits.LimitsCache(home, tmp_path / "cache.json")
    now = time.time()
    c.get(now=now)
    c.get(now=now + limits.MIN_INTERVAL + 1)
    assert len(calls) == 2


def test_cache_survives_a_restart(monkeypatch, tmp_path):
    home = _creds(tmp_path)
    cache = tmp_path / "cache.json"
    monkeypatch.setattr(limits, "fetch",
                        lambda t, url=limits.USAGE_URL: ({"five_hour": {"utilization": 9}}, ""))
    limits.LimitsCache(home, cache).get(force=True)
    fresh = limits.LimitsCache(home, cache)
    assert fresh.get(now=time.time())["five_hour"]["utilization"] == 9


def test_expired_token_is_not_used_for_a_fetch(monkeypatch, tmp_path):
    home = _creds(tmp_path, expires_ms=int((time.time() - 60) * 1000))
    calls = []
    monkeypatch.setattr(limits, "fetch", lambda t, url=limits.USAGE_URL: calls.append(1) or (None, ""))
    limits.LimitsCache(home, tmp_path / "cache.json").get()
    assert calls == []


def test_the_token_never_appears_in_output(monkeypatch, tmp_path):
    home = _creds(tmp_path, token="SUPER-SECRET-TOKEN")
    monkeypatch.setattr(limits, "fetch",
                        lambda t, url=limits.USAGE_URL: ({"five_hour": {"utilization": 2}}, ""))
    c = limits.LimitsCache(home, tmp_path / "cache.json")
    assert "SUPER-SECRET" not in json.dumps(c.get(force=True))
    assert "SUPER-SECRET" not in (tmp_path / "cache.json").read_text()


def test_a_failed_fetch_waits_the_interval_too(monkeypatch, tmp_path):
    """2026-10-28: after a 429, every page poll asked again, which kept the 429 going."""
    home = _creds(tmp_path)
    calls = []
    monkeypatch.setattr(limits, "fetch",
                        lambda t, url=limits.USAGE_URL: (calls.append(1) or (None, "rate-limited by Anthropic, will retry")))
    c = limits.LimitsCache(home, tmp_path / "cache.json")
    now = time.time()
    for i in range(20):
        c.get(now=now + i)
    assert len(calls) == 1
    c.get(now=now + limits.MIN_INTERVAL + 1)
    assert len(calls) == 2


def test_the_cache_is_saved_through_a_temp_file_of_its_own(monkeypatch, tmp_path):
    home = _creds(tmp_path)
    monkeypatch.setattr(limits, "fetch",
                        lambda t, url=limits.USAGE_URL: ({"five_hour": {"utilization": 3}}, ""))
    cache = tmp_path / "cache.json"
    limits.LimitsCache(home, cache).get(force=True)
    assert json.loads(cache.read_text())["data"]["five_hour"]["utilization"] == 3
    assert not list(tmp_path.glob("*.sc-tmp")), "the temp file is renamed into place"
