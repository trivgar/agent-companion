from __future__ import annotations

from pathlib import Path

from agent_companion import mergeq, testtimes


XML = """<testsuite>
  <testcase classname="tests.test_basic" name="test_pass" time="1.2346" />
  <testcase classname="tests.test_basic" name="test_failure" time="2.0"><failure /></testcase>
  <testcase classname="tests.test_basic" name="test_error" time="3.0"><error /></testcase>
  <testcase classname="tests.test_basic" name="test_skip" time="4.0"><skipped /></testcase>
  <testcase classname="tests.test_class.TestClass" name="test_method" time="5.0" />
  <testcase classname="checks.special.Widget" name="test_resolved" time="6.0" />
</testsuite>"""


def test_default_dir_uses_the_merge_queue_state_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_COMPANION_TEST_TIMES_DIR", raising=False)
    monkeypatch.setattr(mergeq, "state_dir", lambda: tmp_path / "mergeq")
    assert testtimes.default_dir() == tmp_path / "mergeq" / "test-times"
    monkeypatch.setenv("AGENT_COMPANION_TEST_TIMES_DIR", str(tmp_path / "configured"))
    assert testtimes.default_dir() == tmp_path / "configured"


def test_parse_junit_rebuilds_nodeids_and_outcomes(tmp_path):
    resolved = tmp_path / "checks" / "special.py"
    resolved.parent.mkdir()
    resolved.write_text("\n")
    assert testtimes.parse_junit(XML, tree=tmp_path) == [
        ["tests/test_basic.py::test_pass", 1.235, "p"],
        ["tests/test_basic.py::test_failure", 2.0, "f"],
        ["tests/test_basic.py::test_error", 3.0, "e"],
        ["tests/test_basic.py::test_skip", 4.0, "s"],
        ["tests/test_class.py::TestClass::test_method", 5.0, "p"],
        ["checks/special.py::Widget::test_resolved", 6.0, "p"],
    ]


def test_record_writes_shape_and_keeps_newest_files(tmp_path):
    xml = tmp_path / "suite.xml"
    xml.write_text(XML)
    directory = tmp_path / "times"
    for second in range(1, 5):
        testtimes.record(xml, "green", directory, now=second, keep=3)
    files = sorted(directory.glob("*.json.gz"))
    assert [path.name for path in files] == ["19700101-000002.json.gz", "19700101-000003.json.gz", "19700101-000004.json.gz"]
    saved = testtimes.load(directory)
    assert saved[0] == {
        "at": 2,
        "result": "green",
        "tests": testtimes.parse_junit(XML),
    }


def test_record_does_not_collide_in_the_same_second(tmp_path):
    xml = tmp_path / "suite.xml"
    xml.write_text(XML)
    first = testtimes.record(xml, "green", tmp_path / "times", now=1)
    second = testtimes.record(xml, "green", tmp_path / "times", now=1)
    assert first != second
    assert first.name == "19700101-000001.json.gz"
    assert second.name == "19700101-000001-1.json.gz"


def test_load_skips_corrupt_files(tmp_path):
    xml = tmp_path / "suite.xml"
    xml.write_text(XML)
    directory = tmp_path / "times"
    testtimes.record(xml, "green", directory, now=1)
    (directory / "19700101-000002.json.gz").write_text("not gzip")
    assert len(testtimes.load(directory)) == 1


def test_slowest_uses_median_and_skips_skipped_entries():
    runs = [
        {"tests": [["a", 1, "p"], ["b", 8, "f"], ["skip", 99, "s"]]},
        {"tests": [["a", 5, "p"], ["b", 2, "p"], ["skip", 100, "s"]]},
        {"tests": [["a", 3, "p"], ["b", 6, "p"]]},
    ]
    assert testtimes.slowest(runs, 2) == [("b", 6.0, 3), ("a", 3.0, 3)]


def test_never_failed_excludes_failures_and_errors():
    runs = [
        {"tests": [["pass", 2, "p"], ["failure", 3, "f"], ["error", 4, "e"]]},
        {"tests": [["pass", 5, "s"], ["failure", 6, "p"], ["error", 7, "p"]]},
    ]
    assert testtimes.never_failed(runs) == [("pass", 7.0, 2)]


def test_cli_missing_dir_prints_a_friendly_message(tmp_path):
    printed = []
    assert testtimes.main(["--dir", str(tmp_path / "missing")], printed.append) == 0
    assert printed == ["no runs are recorded yet"]


def test_cli_prints_slowest_and_never_failed(tmp_path):
    xml = tmp_path / "suite.xml"
    xml.write_text(XML)
    directory = tmp_path / "times"
    testtimes.record(xml, "green", directory, now=1)
    slow = []
    assert testtimes.main(["--dir", str(directory), "--slow", "2"], slow.append) == 0
    assert slow[0] == "median_s runs test"
    assert "checks/special/Widget.py::test_resolved" in slow[1]
    never = []
    assert testtimes.main(["--dir", str(directory), "--never-failed"], never.append) == 0
    assert never[:2] == ["runs considered: 1", "total_s runs test"]
    assert all("test_failure" not in line and "test_error" not in line for line in never[2:])
