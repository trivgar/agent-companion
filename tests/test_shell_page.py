"""The shell at 390px: the sky, the title and strip, the nav and its menu."""
import base64
import os
import pathlib
import re
import threading
import time

import pytest

pytest.importorskip("fastapi")
sync_api = pytest.importorskip("playwright.sync_api")

from agent_companion import server  # noqa: E402

TOKEN = "t" * 32
OWNER = "o" * 64
ROWS = [
    {"pid": 1, "name": "alpha", "project": "api", "title": "Rate limiter", "status": "busy", "waiting": False,
     "model": "claude-opus-5-5", "cost_usd": 3.21, "context_pct": 42.5, "last_reply": "Token bucket in place.",
     "last_activity": 4, "cwd": "/w/api", "branch": "main", "child": False, "session_id": "s1"},
]
USAGE = {"five_hour": {"cost_usd": 1.5, "total_tokens": 1000}, "seven_day": {"cost_usd": 20, "total_tokens": 9000},
         "limits": {"available": False, "reason": "no credentials"}}
# Nine features: six fit the nav, three go under "more". Only sessions is real;
# the rest are answered with an empty script so nothing else runs.
FEATS = [{"id": i, "title": t} for i, t in [
    ("sessions", "Sessions"), ("health", "Machine"), ("scoreboard", "Scoreboard"), ("files", "Files"),
    ("specs", "Specs"), ("pages", "Pages"), ("asks", "Asks"), ("chat", "Chat"), ("mail", "Mail")]]
STUBBED = re.compile(r"/static/(health|scoreboard|files|specs|pages|asks|chat|mail)\.js")
NAV_IDS = ["sessions", "asks", "chat", "mail", "files", "health"]
MORE_IDS = ["scoreboard", "specs", "pages"]


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    import uvicorn
    d = tmp_path_factory.mktemp("home")
    os.environ["AGENT_COMPANION_CLAUDE_HOME"] = str(d / ".claude")
    os.environ["AGENT_COMPANION_DATA_DIR"] = str(d / "data")
    orig = server.FEATURES
    server.FEATURES = ["sessions"]
    app = server.create_app(token=TOKEN, owner_key=OWNER)
    server.FEATURES = orig
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    port = srv.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    os.environ.pop("AGENT_COMPANION_CLAUDE_HOME", None)
    os.environ.pop("AGENT_COMPANION_DATA_DIR", None)


@pytest.fixture
def open_page(base_url):
    """open_page(scheme) gives a loaded page at 390px; every page closes with the test."""
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # browser not installed
            pytest.skip(f"chromium unavailable: {exc}")

        def make(scheme="dark", feats=FEATS):
            ctx = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme=scheme,
                                      service_workers="block")
            pg = ctx.new_page()
            pg.route("**/api/features", lambda r: r.fulfill(json=feats))
            pg.route("**/api/sessions*", lambda r: r.fulfill(json=ROWS))
            pg.route("**/api/usage*", lambda r: r.fulfill(json=USAGE))
            pg.route(STUBBED, lambda r: r.fulfill(body="", content_type="application/javascript"))
            pg.add_init_script(f"localStorage.setItem('cc-owner-key', '{OWNER}')")
            pg.goto(f"{base_url}/")
            pg.wait_for_selector("#nav button[data-id]")
            return pg

        yield make
        browser.close()


def roboto(page):
    """Draw the page in Roboto, the font a phone uses for system-ui."""
    font = (pathlib.Path(__file__).resolve().parent / "fixtures" / "Roboto-Regular.ttf").read_bytes()
    page.add_style_tag(content="@font-face{font-family:PhoneRoboto;src:url(data:font/ttf;base64,"
                       + base64.b64encode(font).decode() + ")}"
                       "body{font-family:PhoneRoboto,sans-serif !important}")
    page.evaluate("document.fonts.ready")


def css(page, selector, prop):
    return page.eval_on_selector(selector, f"e => getComputedStyle(e).{prop}")


def test_the_dark_sky_is_on_html_and_the_body_lets_it_through(open_page):
    page = open_page("dark")
    assert "linear-gradient" in css(page, "html", "backgroundImage")
    assert css(page, "html", "backgroundAttachment").startswith("fixed")
    assert css(page, "body", "backgroundColor") == "rgba(0, 0, 0, 0)"


def test_the_light_ground_is_paper(open_page):
    page = open_page("light")
    assert css(page, "html", "backgroundColor") == "rgb(250, 248, 244)"
    assert css(page, "html", "backgroundImage") == "none"


def test_the_header_paints_the_top_of_the_sky_so_rows_scroll_under_it(open_page):
    page = open_page("dark")
    assert css(page, "header", "position") == "sticky"
    assert css(page, "header", "backgroundColor") == "rgb(16, 19, 25)"


def test_the_title_is_small_caps_and_quiet(open_page):
    page = open_page("dark")
    assert css(page, "header h1", "textTransform") == "uppercase"
    assert float(css(page, "header h1", "fontSize").rstrip("px")) < 14
    dim = page.evaluate("(() => { const d = document.createElement('i'); d.style.color='var(--dim)'; "
                        "document.body.appendChild(d); return getComputedStyle(d).color })()")
    assert css(page, "header h1", "color") == dim


def test_the_title_and_the_strip_share_a_row_container(open_page):
    page = open_page("dark")
    assert page.locator("header .head-row > h1").count() == 1
    assert page.locator("header .head-row > #strip").count() == 1
