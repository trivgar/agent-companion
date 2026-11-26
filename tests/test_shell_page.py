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


def ids(page, selector):
    return page.eval_on_selector_all(selector, "els => els.map(e => e.dataset.id)")


def box(page, selector):
    return page.eval_on_selector(selector, "e => { const r = e.getBoundingClientRect(); "
                                 "return {x: r.x, w: r.width, h: r.height, bottom: r.bottom} }")


def test_six_items_sit_in_the_nav_and_the_rest_under_more(open_page):
    page = open_page()
    assert ids(page, "#nav button[data-id]") == NAV_IDS
    assert page.locator("#morelink").is_visible()
    assert page.locator("#morebar").is_hidden()
    assert ids(page, "#morebar button[data-id]") == MORE_IDS


def test_few_features_all_sit_in_the_nav(open_page):
    page = open_page(feats=[{"id": "sessions", "title": "Sessions"}, {"id": "pages", "title": "Pages"}])
    assert ids(page, "#nav button[data-id]") == ["sessions", "pages"]
    assert ids(page, "#morebar button[data-id]") == []


def test_nav_items_are_text_and_tall_enough_to_tap(open_page):
    page = open_page()
    roboto(page)
    for i in NAV_IDS:
        sel = f'#nav button[data-id="{i}"]'
        assert css(page, sel, "borderTopWidth") == "0px"
        assert css(page, sel, "backgroundColor") == "rgba(0, 0, 0, 0)"
        assert box(page, sel)["h"] >= 44
    assert box(page, "#morelink")["h"] >= 44


def test_the_page_does_not_scroll_sideways_in_a_phones_font(open_page):
    page = open_page()
    roboto(page)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_the_underline_sits_under_the_current_item_and_follows_a_tap(open_page):
    page = open_page()
    page.wait_for_selector("#nav .ink.on")
    cur, ink = box(page, '#nav button[data-id="sessions"]'), box(page, "#nav .ink")
    assert abs(ink["x"] - cur["x"]) <= 1 and abs(ink["w"] - cur["w"]) <= 1
    page.click('#nav button[data-id="chat"]')
    page.wait_for_timeout(600)
    cur, ink = box(page, '#nav button[data-id="chat"]'), box(page, "#nav .ink")
    assert abs(ink["x"] - cur["x"]) <= 1 and abs(ink["w"] - cur["w"]) <= 1
    assert css(page, '#nav button[data-id="chat"]', "borderBottomColor") == "rgba(0, 0, 0, 0)"


def test_more_opens_a_menu_and_a_pick_shows_that_view(open_page):
    page = open_page()
    page.click("#morelink")
    assert page.locator("#morebar").is_visible()
    assert page.get_attribute("#morelink", "aria-expanded") == "true"
    page.click('#morebar button[data-id="specs"]')
    assert page.locator("#morebar").is_hidden()
    assert page.eval_on_selector('#views > section[data-id="specs"]', "e => !e.hidden")
    assert page.locator('#views > section[data-id="sessions"]').is_hidden()
    assert "on" in page.get_attribute("#morelink", "class").split()
    assert page.locator("#nav .ink.on").count() == 0
    assert "view=specs" in page.url


def test_the_menu_closes_on_escape_and_on_a_tap_outside(open_page):
    page = open_page()
    page.click("#morelink")
    page.keyboard.press("Escape")
    assert page.locator("#morebar").is_hidden()
    page.click("#morelink")
    page.click("main")
    assert page.locator("#morebar").is_hidden()
    assert page.get_attribute("#morelink", "aria-expanded") == "false"


def test_a_count_on_a_hidden_item_shows_on_more(open_page):
    page = open_page()
    page.evaluate("CC.badge('specs', 2); CC.badge('pages', 1)")
    assert page.inner_text("#morelink .n") == "3"
    assert page.inner_text('#morebar button[data-id="specs"] .n') == "2"
    page.evaluate("CC.badge('specs', 0); CC.badge('pages', 0)")
    assert page.inner_text("#morelink .n") == ""


def test_a_view_named_in_the_address_opens_even_from_the_menu(open_page, base_url):
    page = open_page()
    page.goto(f"{base_url}/?view=pages")
    page.wait_for_selector("#nav button[data-id]")
    assert page.eval_on_selector('#views > section[data-id="pages"]', "e => !e.hidden")


def test_the_strip_is_one_span_per_reading_with_the_figures_in_bold(open_page):
    page = open_page()
    page.wait_for_function("document.querySelectorAll('#strip span').length >= 3")
    spans = page.eval_on_selector_all("#strip span", "els => els.map(e => e.textContent)")
    assert spans[:3] == ["5h $1.50", "7d $20.00", "limits: no credentials"]
    assert page.eval_on_selector_all("#strip b", "els => els.map(e => e.textContent)")[:2] == ["$1.50", "$20.00"]
    assert " | " not in page.inner_text("#strip")


def test_a_reading_it_does_not_have_is_said_in_words_never_as_a_zero(open_page):
    page = open_page()
    page.wait_for_function("document.querySelectorAll('#strip span').length >= 3")
    text = page.inner_text("#strip")
    assert "no credentials" in text
    assert "0%" not in text


def test_the_strip_escapes_what_the_server_says(open_page):
    page = open_page()
    page.route("**/api/usage*", lambda r: r.fulfill(json={
        "five_hour": {"cost_usd": 1}, "seven_day": {"cost_usd": 2},
        "limits": {"available": False, "reason": "<img src=x onerror=window.__hit=1>"}}))
    page.evaluate("CC.show('sessions')")
    page.wait_for_function("document.getElementById('strip').textContent.includes('<img')")
    assert page.locator("#strip img").count() == 0
    assert page.evaluate("window.__hit") is None


def test_the_switch_stops_motion_and_is_remembered(open_page, base_url):
    page = open_page()
    assert page.evaluate("document.documentElement.hasAttribute('data-still')") is False
    page.click('#nav button[data-id="chat"]')
    assert css(page, "#nav .ink", "transitionDuration") != "0s"
    page.evaluate("CCStill.set(true)")
    assert page.evaluate("document.documentElement.hasAttribute('data-still')") is True
    assert css(page, "#nav .ink", "transitionDuration") == "0s"
    page.goto(f"{base_url}/")
    page.wait_for_selector("#nav button[data-id]")
    assert page.evaluate("document.documentElement.hasAttribute('data-still')") is True
    assert page.evaluate("CCStill.get()") is True


def test_the_menu_carries_the_switch_and_says_its_state(open_page):
    page = open_page()
    page.click("#morelink")
    toggle = page.locator("#still-toggle")
    assert toggle.inner_text().strip().lower() == "animations: on"
    assert toggle.get_attribute("aria-checked") == "true"
    toggle.click()
    assert toggle.inner_text().strip().lower() == "animations: off"
    assert toggle.get_attribute("aria-checked") == "false"
    assert page.locator("#morebar").is_visible()
    assert page.evaluate("CCStill.get()") is True


def test_the_switch_alone_is_enough_to_show_more(open_page):
    page = open_page(feats=[{"id": "sessions", "title": "Sessions"}])
    assert page.locator("#morelink").is_visible()
    page.click("#morelink")
    assert page.locator("#still-toggle").is_visible()
