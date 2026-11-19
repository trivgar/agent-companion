from __future__ import annotations

import shlex
from pathlib import Path
from types import SimpleNamespace

from agent_companion import memclear, mergeq, testtimes


JUNIT = """<testsuite>
  <testcase classname="tests.test_example" name="test_fast" time="0.125" />
</testsuite>"""


def _passing_run(command, **_kwargs):
    junit = next(argument.split("=", 1)[1] for argument in shlex.split(command)
                 if argument.startswith("--junitxml="))
    Path(junit).write_text(JUNIT)
    return SimpleNamespace(returncode=0, stdout="1 passed in 0.01s\n", stderr="")


def test_run_command_records_a_full_run_in_an_explicit_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(mergeq.config, "get", lambda key, default=None: default)
    monkeypatch.setattr(mergeq, "_run", _passing_run)
    directory = tmp_path / "times"

    assert mergeq.run_command(tmp_path, command="python3 -m pytest", times_dir=directory)[0] is True

    runs = testtimes.load(directory)
    assert len(runs) == 1
    assert runs[0]["result"] == "green"
    assert runs[0]["tests"] == [["tests/test_example.py::test_fast", 0.125, "p"]]


def test_run_command_never_records_to_the_default_directory_under_pytest(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_mergeq_helpers.py::test (call)")
    monkeypatch.setattr(mergeq.config, "get", lambda key, default=None: default)
    monkeypatch.setattr(mergeq, "_run", _passing_run)
    monkeypatch.setattr(testtimes, "default_dir", lambda: (_ for _ in ()).throw(AssertionError("used")))
    recorded = []
    monkeypatch.setattr(testtimes, "record", lambda *args, **kwargs: recorded.append((args, kwargs)))

    assert mergeq.run_command(tmp_path, command="python3 -m pytest")[0] is True
    assert recorded == []


def test_run_command_preflights_only_when_enabled_and_outside_pytest(tmp_path, monkeypatch):
    monkeypatch.setattr(mergeq, "_run", _passing_run)
    calls = []
    monkeypatch.setattr(memclear, "preflight", lambda: calls.append("called") or {"ok": True, "free_gb": 12.0})
    enabled = {"value": False}
    monkeypatch.setattr(mergeq.config, "get", lambda key, default=None: enabled["value"] if key == "mergeq_make_room" else default)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    assert mergeq.run_command(tmp_path, command="python3 -m pytest", times_dir=tmp_path / "off")[0] is True
    assert calls == []

    enabled["value"] = True
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_mergeq_helpers.py::test (call)")
    assert mergeq.run_command(tmp_path, command="python3 -m pytest", times_dir=tmp_path / "under-pytest")[0] is True
    assert calls == []

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    green, _bad, detail = mergeq.run_command(tmp_path, command="python3 -m pytest", times_dir=tmp_path / "on")
    assert green is True and calls == ["called"]
    assert "[make room]" in detail and '"free_gb":12.0' in detail
