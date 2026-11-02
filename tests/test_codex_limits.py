"""Codex plan limits off Codex's own rollout logs, in the shape both pages
read: the plan's long window at the top level, a 5-hour one under five_hour,
and why when there is nothing to show."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from agent_companion import codex_limits
from agent_companion.codex_limits import CodexLimits

NOW = 1_800_000_000.0
WEEK, FIVE = 10080, 300


def _iso(when):
    return datetime.fromtimestamp(when, timezone.utc).isoformat().replace("+00:00", "Z")


def _utc(when):
    return datetime.fromtimestamp(when, timezone.utc).isoformat()


def _event(when, primary, secondary=None, plan="plus"):
    return {"timestamp": _iso(when), "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
        "primary": primary, "secondary": secondary, "plan_type": plan}}}


def _rollout(home: Path, *events, day=NOW, name="example", modified=None, junk=0) -> Path:
    folder = home / "sessions" / datetime.fromtimestamp(day).strftime("%Y/%m/%d")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-{name}.jsonl"
    lines = [json.dumps({"timestamp": _iso(NOW), "type": "session_meta", "payload": {}})]
    lines += ["x" * 100] * junk
    lines += [json.dumps(event) for event in events]
    path.write_text("\n".join(lines) + "\n")
    stamp = NOW if modified is None else modified
    os.utime(path, (stamp, stamp))
    return path


def _window(used, minutes, reset):
    return {"used_percent": used, "window_minutes": minutes, "resets_at": reset}


def test_a_missing_sessions_folder_keeps_the_feature_off(tmp_path):
    assert CodexLimits(tmp_path / "missing").read(NOW) == {
        "enabled": False, "available": False, "why": "no Codex sessions folder"}


def test_no_runs_says_why(tmp_path):
    (tmp_path / "home" / "sessions").mkdir(parents=True)
    assert CodexLimits(tmp_path / "home").read(NOW) == {
        "enabled": True, "available": False, "why": "no Codex run in 8 days"}


def test_the_newest_runs_last_reading_sits_at_the_top(tmp_path):
    home = tmp_path / "home"
    _rollout(home, _event(NOW - 50_000, _window(35, WEEK, NOW + 3 * 86400)),
             day=NOW - 86400, name="a", modified=NOW - 50_000)
    _rollout(home, _event(NOW - 4000, _window(2, WEEK, NOW + 6 * 86400)),
             _event(NOW - 3600, _window(3, WEEK, NOW + 6 * 86400)), name="b", modified=NOW - 3600)
    assert CodexLimits(home).read(NOW) == {
        "enabled": True, "available": True, "utilization": 3, "resets_at": _utc(NOW + 6 * 86400),
        "resets_in_seconds": 6 * 86400, "age_seconds": 3600, "window_minutes": WEEK,
        "plan": "plus", "why": "", "five_hour": None}


def test_a_five_hour_window_sits_under_the_long_one(tmp_path):
    home = tmp_path / "home"
    _rollout(home, _event(NOW - 120, _window(18, WEEK, NOW + 30_000), _window(37, FIVE, NOW + 10_000)))
    value = CodexLimits(home).read(NOW)
    assert value["utilization"] == 18 and value["window_minutes"] == WEEK
    assert value["five_hour"] == {"utilization": 37, "resets_at": _utc(NOW + 10_000), "resets_in_seconds": 10_000}


def test_the_five_hour_window_may_come_first(tmp_path):
    home = tmp_path / "home"
    _rollout(home, _event(NOW - 60, _window(37, FIVE, NOW + 10_000), _window(18, WEEK, NOW + 30_000)))
    value = CodexLimits(home).read(NOW)
    assert value["utilization"] == 18 and value["five_hour"]["utilization"] == 37


def test_a_long_window_that_is_not_a_week_is_shown(tmp_path):
    home = tmp_path / "home"
    month = 30 * 1440
    _rollout(home, _event(NOW - 60, _window(41, month, NOW + 20 * 86400), plan="prolite"))
    value = CodexLimits(home).read(NOW)
    assert value["available"] is True and value["utilization"] == 41
    assert value["window_minutes"] == month and value["plan"] == "prolite"


def test_a_line_with_only_a_five_hour_window_is_not_a_reading(tmp_path):
    home = tmp_path / "home"
    _rollout(home, _event(NOW - 600, _window(9, WEEK, NOW + 86400)),
             _event(NOW - 60, _window(50, FIVE, NOW + 3600)))
    value = CodexLimits(home).read(NOW)
    assert value["utilization"] == 9 and value["five_hour"] is None and value["age_seconds"] == 600


def test_relative_reset_seconds_are_read(tmp_path):
    home = tmp_path / "home"
    _rollout(home, _event(NOW - 30, {"used_percent": 12, "window_minutes": WEEK, "resets_in_seconds": 500}))
    value = CodexLimits(home).read(NOW)
    assert value["utilization"] == 12 and value["five_hour"] is None
    assert value["resets_in_seconds"] == 470 and value["resets_at"] == _utc(NOW + 470)


def test_a_window_that_reset_since_says_so_not_zero(tmp_path):
    home = tmp_path / "home"
    _rollout(home, _event(NOW - 400, _window(49, WEEK, NOW - 1), _window(37, FIVE, NOW - 1)))
    assert CodexLimits(home).read(NOW) == {
        "enabled": True, "available": False, "why": "the window reset since the last Codex run",
        "age_seconds": 400}


def test_a_reading_older_than_its_window_is_not_shown(tmp_path):
    """The rule the core adds: a reset in the future cannot vouch for a reading
    older than the window it measured (the reset time is the plan's to move)."""
    home = tmp_path / "home"
    _rollout(home, _event(NOW - 2 * 86400, _window(80, 1440, NOW + 3600)))
    assert CodexLimits(home).read(NOW) == {
        "enabled": True, "available": False, "why": "the last Codex reading is older than its window",
        "age_seconds": 2 * 86400}


def test_a_five_hour_window_that_drops_never_blanks_the_long_one(tmp_path):
    reset = tmp_path / "reset"
    _rollout(reset, _event(NOW - 60, _window(30, WEEK, NOW + 86400), _window(12, FIVE, NOW - 5)))
    value = CodexLimits(reset).read(NOW)
    assert value["available"] is True and value["utilization"] == 30 and value["five_hour"] is None
    old = tmp_path / "old"
    _rollout(old, _event(NOW - 6 * 3600, _window(30, WEEK, NOW + 86400), _window(12, FIVE, NOW + 600)))
    value = CodexLimits(old).read(NOW)
    assert value["available"] is True and value["utilization"] == 30 and value["five_hour"] is None


def test_a_run_with_no_reading_yet_falls_back_to_the_one_before(tmp_path):
    home = tmp_path / "home"
    _rollout(home, _event(NOW - 600, _window(7, WEEK, NOW + 86400)), name="a", modified=NOW - 600)
    _rollout(home, name="b", modified=NOW - 5)
    assert CodexLimits(home).read(NOW)["utilization"] == 7


def test_only_the_tail_is_read_and_bad_lines_are_skipped(tmp_path):
    home = tmp_path / "home"
    path = _rollout(home, _event(NOW - 60, _window(12, WEEK, NOW + 86400)), junk=5000)
    with path.open("a") as output:
        output.write('{"rate_limits": broken\n')
    assert path.stat().st_size > codex_limits.TAIL_BYTES
    assert CodexLimits(home).read(NOW)["utilization"] == 12


def test_unchanged_rollout_is_not_reparsed_but_a_changed_one_is(tmp_path, monkeypatch):
    home = tmp_path / "home"
    path = _rollout(home, _event(NOW - 10, _window(18, WEEK, NOW + 30_000)))
    reader = CodexLimits(home)
    calls = []
    original = reader._tail_reading
    monkeypatch.setattr(reader, "_tail_reading", lambda file: calls.append(file) or original(file))
    assert reader.read(NOW)["utilization"] == 18
    assert reader.read(NOW + 10)["utilization"] == 18
    assert calls == [path]
    with path.open("a") as output:
        output.write(json.dumps(_event(NOW, _window(29, WEEK, NOW + 30_000))) + "\n")
    assert reader.read(NOW + 11)["utilization"] == 29
    assert calls == [path, path]


def test_the_plan_is_named_only_when_codex_names_it(tmp_path):
    home = tmp_path / "home"
    _rollout(home, _event(NOW - 60, _window(5, WEEK, NOW + 86400), plan=None))
    assert CodexLimits(home).read(NOW)["plan"] is None


def test_the_module_reads_the_configured_codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    _rollout(home, _event(NOW - 60, _window(21, WEEK, NOW + 86400)))
    monkeypatch.setenv("AGENT_COMPANION_CODEX_HOME", str(home))
    codex_limits._readers.clear()
    assert codex_limits.read(NOW)["utilization"] == 21


def test_sessions_usage_and_strip_include_the_optional_codex_reading():
    root = Path(__file__).resolve().parents[1]
    route = (root / "agent_companion/server/sessions.py").read_text()
    strip = (root / "agent_companion/server/static/sessions.js").read_text()
    assert 'out["codex"] = codex_limits.read()' in route
    assert "Codex ${codex.join(' · ')}" in strip
    assert "5h ${Math.round(c.five_hour.utilization)}%" in strip
    assert "${label} ${Math.round(c.utilization)}%" in strip
    assert "Codex: ${c.why}" in strip
    assert "as of ${ago(c.age_seconds)}" in strip
