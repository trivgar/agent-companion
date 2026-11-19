"""agent_companion/modelwatch.py: the picker against Anthropic's lineup. Never fetches,
never files for real: the page is a fixture and file_fn a list."""
from pathlib import Path

import pytest

from agent_companion import modelwatch as mw

MD = (Path(__file__).parent / "fixtures/modelwatch/overview.md").read_text()
LINEUP = ["claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]


def test_the_lineup_is_the_claude_api_id_row_only():
    # The footnote and the alias row name ids too; only the ID row counts.
    assert mw.lineup(MD) == LINEUP


def test_a_page_without_the_row_is_an_error_not_an_empty_lineup():
    with pytest.raises(ValueError):
        mw.lineup("# Models\n\nNothing here.\n")


def test_a_matching_picker_files_nothing():
    filed = []
    said, state = mw.run(MD, list(LINEUP), set(LINEUP), {}, filed.append)
    assert filed == [] and said.startswith("picker matches") and state["filed"] is None


def test_the_day_fable_5_2_shipped_files_once():
    # 2026-11-09: the picker offered 5.1, and the price table had no 5.2 row.
    offered = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"]
    priced = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1", "claude-fable-5"}
    filed = []
    _, state = mw.run(MD, offered, priced, {}, filed.append)
    assert len(filed) == 1
    assert "new: claude-fable-5-2" in filed[0] and "gone: claude-fable-5-1" in filed[0]
    assert "unpriced: claude-fable-5-2" in filed[0]
    said, _ = mw.run(MD, offered, priced, state, filed.append)
    assert len(filed) == 1 and said == "same difference as already filed"


def test_a_prefix_price_is_not_a_price():
    # claude-fable-5 is a prefix of claude-fable-5-2; only an exact row counts.
    assert mw.compare(["claude-fable-5-2"], ["claude-fable-5-2"], {"claude-fable-5"})["unpriced"] == ["claude-fable-5-2"]


def test_a_changed_page_is_filed_once_as_blind():
    filed = []
    _, state = mw.run("no table", LINEUP, set(LINEUP), {}, filed.append)
    mw.run("no table", LINEUP, set(LINEUP), state, filed.append)
    assert len(filed) == 1 and "blind" in filed[0]


def test_once_fixed_a_later_difference_files_again():
    filed = []
    _, state = mw.run(MD, LINEUP[:3], set(LINEUP), {}, filed.append)
    _, state = mw.run(MD, list(LINEUP), set(LINEUP), state, filed.append)
    mw.run(MD, LINEUP[:3], set(LINEUP), state, filed.append)
    assert len(filed) == 2
