"""The suite runs in a home of its own, so no default path in it is the
machine's: not data_dir, not ~/.claude, not the push store under it."""
import os
import pwd
import tempfile
from pathlib import Path

import pytest


def test_the_suite_has_a_home_of_its_own():
    real = Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert Path.home() != real
    assert str(Path.home()).startswith(tempfile.gettempdir())


def test_default_paths_land_in_that_home():
    pytest.importorskip("fastapi")
    from agent_companion import webpush
    from agent_companion.server import sessions
    assert str(sessions.data_dir()).startswith(str(Path.home()))
    assert str(webpush.push_dir()).startswith(str(Path.home()))
    assert str(sessions.claude_home()).startswith(str(Path.home()))


def test_the_page_tests_still_find_their_browsers():
    """A home of its own must not hide the installed browsers: every page test
    would skip as "chromium unavailable" and the suite would still read green."""
    browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    assert browsers and not browsers.startswith(str(Path.home()))


def test_the_leak_check_still_reads_its_deny_list():
    """The deny list lives outside every repo, in the real home: a moved home
    must not leave the leak check reading nothing."""
    terms = os.environ.get("AGENT_COMPANION_LEAK_TERMS", "")
    assert terms and not terms.startswith(str(Path.home()))
