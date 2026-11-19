"""Store and report recent pytest durations from JUnit XML."""
from __future__ import annotations

import argparse
import gzip
import json
import os
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Callable, Optional


def default_dir() -> Path:
    configured = os.environ.get("AGENT_COMPANION_TEST_TIMES_DIR")
    if configured:
        return Path(configured)
    from . import mergeq
    return mergeq.state_dir() / "test-times"


def _xml_text(xml_text_or_path: str | Path) -> str:
    if isinstance(xml_text_or_path, Path):
        try:
            return xml_text_or_path.read_text()
        except OSError as exc:
            raise ValueError(f"cannot read JUnit XML: {exc}") from exc
    if xml_text_or_path.lstrip().startswith("<"):
        return xml_text_or_path
    try:
        return Path(xml_text_or_path).read_text()
    except OSError as exc:
        raise ValueError(f"cannot read JUnit XML: {exc}") from exc


def _nodeid(classname: str, name: str, tree: Optional[Path]) -> str:
    parts = [part for part in classname.split(".") if part]
    path = ""
    classes: list[str] = []
    if tree is not None:
        root = Path(tree)
        for end in range(len(parts), 0, -1):
            candidate = "/".join(parts[:end]) + ".py"
            if (root / candidate).is_file():
                path, classes = candidate, parts[end:]
                break
    if not path:
        test_part = next((i for i, part in enumerate(parts) if part.startswith("test_")), None)
        if test_part is None:
            path, classes = "/".join(parts) + ".py", []
        else:
            path, classes = "/".join(parts[:test_part + 1]) + ".py", parts[test_part + 1:]
    suffix = "::".join(classes)
    return path + (f"::{suffix}" if suffix else "") + f"::{name}"


def parse_junit(xml_text_or_path: str | Path, tree: Optional[Path] = None) -> list[list]:
    """Turn pytest JUnit XML into node ids, rounded durations, and outcomes."""
    try:
        root = ET.fromstring(_xml_text(xml_text_or_path))
    except (ET.ParseError, ValueError) as exc:
        raise ValueError(f"invalid JUnit XML: {exc}") from exc
    rows = []
    for case in root.iter():
        if case.tag.rsplit("}", 1)[-1] != "testcase":
            continue
        children = {child.tag.rsplit("}", 1)[-1] for child in case}
        if "failure" in children:
            outcome = "f"
        elif "error" in children:
            outcome = "e"
        elif "skipped" in children:
            outcome = "s"
        else:
            outcome = "p"
        try:
            seconds = round(float(case.attrib.get("time", "0") or 0), 3)
        except ValueError as exc:
            raise ValueError(f"invalid testcase time: {case.attrib.get('time')!r}") from exc
        rows.append([_nodeid(case.attrib.get("classname", ""), case.attrib.get("name", ""), tree), seconds, outcome])
    return rows


def record(xml_path: Path, result: str, directory: Path, tree: Optional[Path] = None,
           now: Optional[float] = None, keep: int = 60) -> Path:
    """Atomically save one parsed run, retaining the newest ``keep`` files."""
    tests = parse_junit(xml_path, tree)
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    at = int(time.time() if now is None else now)
    stamp = datetime.fromtimestamp(at, timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = directory / f"{stamp}.json.gz"
    collision = 1
    while target.exists():
        target = directory / f"{stamp}-{collision}.json.gz"
        collision += 1
    payload = json.dumps({"at": at, "result": result, "tests": tests}, separators=(",", ":")).encode()
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb") as zipped:
                zipped.write(payload)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    files = sorted(directory.glob("*.json.gz"))
    old = files[:-keep] if keep > 0 else files
    for path in old:
        path.unlink()
    return target


def load(directory: Path) -> list[dict]:
    """Load readable recorded runs in filename, and therefore time, order."""
    try:
        paths = sorted(Path(directory).glob("*.json.gz"))
    except OSError:
        return []
    rows = []
    for path in paths:
        try:
            with gzip.open(path, "rt") as source:
                value = json.load(source)
        except (OSError, ValueError, EOFError):
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _tests_in(run: dict):
    tests = run.get("tests", [])
    if not isinstance(tests, list):
        return
    for test in tests:
        if not isinstance(test, (list, tuple)) or len(test) < 3 or not isinstance(test[0], str):
            continue
        try:
            yield test[0], float(test[1]), test[2]
        except (TypeError, ValueError):
            continue


def slowest(runs: list[dict], n: int) -> list[tuple[str, float, int]]:
    """Return the tests with the highest median non-skipped durations."""
    durations: dict[str, list[float]] = {}
    for run in runs:
        if not isinstance(run, dict):
            continue
        for nodeid, seconds, outcome in _tests_in(run):
            if outcome != "s":
                durations.setdefault(nodeid, []).append(seconds)
    rows = [(nodeid, median(seconds), len(seconds)) for nodeid, seconds in durations.items()]
    return sorted(rows, key=lambda row: (-row[1], row[0]))[:n]


def never_failed(runs: list[dict]) -> list[tuple[str, float, int]]:
    """Return recorded tests with no failure or error, slowest total first."""
    totals: dict[str, float] = {}
    seen: dict[str, int] = {}
    bad: set[str] = set()
    for run in runs:
        if not isinstance(run, dict):
            continue
        for nodeid, seconds, outcome in _tests_in(run):
            totals[nodeid] = totals.get(nodeid, 0.0) + seconds
            seen[nodeid] = seen.get(nodeid, 0) + 1
            if outcome in {"f", "e"}:
                bad.add(nodeid)
    rows = [(nodeid, total, seen[nodeid]) for nodeid, total in totals.items() if nodeid not in bad]
    return sorted(rows, key=lambda row: (-row[1], row[0]))


def _print_slowest(rows: list[tuple[str, float, int]], out: Callable[[str], None]) -> None:
    out("median_s runs test")
    for nodeid, seconds, seen in rows:
        out(f"{seconds:.3f} {seen} {nodeid}")


def _print_never_failed(rows: list[tuple[str, float, int]], runs: int,
                        out: Callable[[str], None]) -> None:
    out(f"runs considered: {runs}")
    out("total_s runs test")
    for nodeid, seconds, seen in rows:
        out(f"{seconds:.3f} {seen} {nodeid}")


def main(argv: Optional[list[str]] = None, out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slow", type=int, metavar="N")
    parser.add_argument("--never-failed", action="store_true")
    parser.add_argument("--dir", dest="directory", type=Path, default=None)
    args = parser.parse_args(argv)
    runs = load(args.directory or default_dir())
    if not runs:
        out("no runs are recorded yet")
        return 0
    if args.slow is not None or not args.never_failed:
        _print_slowest(slowest(runs, args.slow if args.slow is not None else 25), out)
    if args.never_failed:
        _print_never_failed(never_failed(runs), len(runs), out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
