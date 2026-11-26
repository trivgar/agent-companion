"""The chat sheet over the sessions list at 390px."""
import base64
import os
import pathlib
import threading
import time

import pytest

pytest.importorskip("fastapi")
sync_api = pytest.importorskip("playwright.sync_api")

from agent_companion import server  # noqa: E402

TOKEN = "t" * 32
OWNER = "o" * 64


def row(pid, name, **extra):
    base = {"pid": pid, "name": name, "project": name, "title": "", "status": "idle", "waiting": False,
            "model": "claude-opus-5-5", "cost_usd": 1.0, "context_pct": 42, "last_reply": f"{name} is ready.",
            "last_activity": 30, "cwd": f"/w/{name}", "branch": "", "child": False, "session_id": f"s{pid}"}
    base.update(extra)
    return base


ROWS = [
    row(1, "api", status="busy", last_activity=4, last_reply="Token bucket in place; 14 new tests passing."),
    row(2, "web", waiting=True, last_reply="Schema fix is ready."),
    row(3, "docs", status="idle", last_activity=2820, last_reply="Draft is in the changelog."),
]
AGENTS = [{"name": r["name"], "project": r["project"], "status": r["status"],
           "waiting": r["waiting"], "pid": r["pid"], "session_id": r["session_id"]} for r in ROWS]
MESSAGES = [
    {"role": "me", "at": 1700000000, "to": "api", "text": "Move the e2e job to the 8 core runner."},
    {"role": "agent", "at": 1700000060, "from": "api", "text": "Moved it. Two jobs are green on arm64."},
]
USAGE = {"five_hour": {"cost_usd": 1.5, "total_tokens": 1000}, "seven_day": {"cost_usd": 20, "total_tokens": 9000},
         "limits": {"available": False, "reason": "no credentials"}}


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    import uvicorn
    d = tmp_path_factory.mktemp("home")
    os.environ["AGENT_COMPANION_CLAUDE_HOME"] = str(d / ".claude")
    os.environ["AGENT_COMPANION_DATA_DIR"] = str(d / "data")
    orig = server.FEATURES
    server.FEATURES = ["sessions", "chat"]
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

        def make(scheme="dark", messages=None, asks=None):
            msgs = MESSAGES if messages is None else messages
            ctx = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme=scheme,
                                      service_workers="block")
            pg = ctx.new_page()
            pg.route("**/api/sessions*", lambda r: r.fulfill(json=ROWS))
            pg.route("**/api/usage*", lambda r: r.fulfill(json=USAGE))
            pg.route("**/api/cave", lambda r: r.fulfill(json={"agents": AGENTS, "messages": []}))
            pg.route("**/api/cave/chat/*", lambda r: r.fulfill(json={
                "messages": msgs, "asks": asks or [], "dialog": None, "models": [], "efforts": []}))
            pg.route("**/api/cave/seen/*", lambda r: r.fulfill(json={"ok": True}))
            pg.add_init_script(f"localStorage.setItem('cc-owner-key', '{OWNER}')")
            pg.goto(f"{base_url}/")
            pg.wait_for_selector(".scard")
            pg.wait_for_selector("#chat-log", state="attached")
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


def box(page, selector):
    return page.eval_on_selector(
        selector,
        "async e => { const s = e.closest('.sheet'); "
        "if (s) await Promise.all(s.getAnimations().map(a => a.finished)); "
        "const r = e.getBoundingClientRect(); "
        "return {x: r.x, y: r.y, w: r.width, h: r.height, top: r.top, bottom: r.bottom} }",
    )


# Task 1: the sheet in the shell


def test_a_sheet_shows_a_second_view_over_the_current_one(open_page):
    page = open_page()
    page.evaluate("CC.sheet('chat')")
    assert page.eval_on_selector('#views > section[data-id="sessions"]', "e => !e.hidden")
    assert page.eval_on_selector('#views > section[data-id="chat"]', "e => !e.hidden && e.classList.contains('sheet')")
    assert css(page, '#views > section[data-id="chat"]', "position") == "fixed"
    assert page.get_attribute('#nav button[data-id="sessions"]', "aria-current") == "true"
    assert page.get_attribute('#nav button[data-id="chat"]', "aria-current") == "false"
    assert "view=chat" not in page.url


def test_the_sheet_closes_on_its_button_on_escape_and_on_the_backdrop(open_page):
    page = open_page()
    for how in ("button", "escape", "backdrop"):
        page.evaluate("CC.sheet('chat')")
        assert page.locator(".sheet-back").count() == 1
        if how == "button":
            page.click("button.sheet-close")
        elif how == "escape":
            page.keyboard.press("Escape")
        else:
            page.click(".sheet-back", position={"x": 10, "y": 10})
        assert page.eval_on_selector('#views > section[data-id="chat"]',
                                     "e => e.hidden && !e.classList.contains('sheet')")
        assert page.locator(".sheet-back").count() == 0


def test_picking_a_view_closes_the_sheet(open_page):
    page = open_page()
    page.evaluate("CC.sheet('chat')")
    page.locator('#nav button[data-id="chat"]').dispatch_event("click")
    assert page.eval_on_selector('#views > section[data-id="chat"]',
                                 "e => !e.hidden && !e.classList.contains('sheet')")
    assert page.locator(".sheet-back").count() == 0


def test_the_open_sheet_is_refreshed_with_the_view_behind_it(open_page):
    page = open_page()
    hits = []
    page.on("request", lambda r: hits.append(r.url) if r.url.endswith("/api/cave") else None)
    page.evaluate("CC.sheet('chat')")
    page.wait_for_function("document.documentElement.dataset.sheet === 'chat'")
    page.wait_for_timeout(100)
    before = len(hits)
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_function(f"() => true")
    page.wait_for_timeout(200)
    assert len(hits) > before


def test_the_page_behind_does_not_scroll_while_a_sheet_is_open(open_page):
    page = open_page()
    assert css(page, "html", "overflow") != "hidden"
    page.evaluate("CC.sheet('chat')")
    assert css(page, "html", "overflow") == "hidden"
    page.evaluate("CC.closeSheet()")
    assert css(page, "html", "overflow") != "hidden"


def test_the_close_button_is_tall_enough_to_tap(open_page):
    page = open_page()
    roboto(page)
    page.evaluate("CC.sheet('chat')")
    b = box(page, "button.sheet-close")
    assert b["h"] >= 44 and b["w"] >= 44


# Task 2: chat in the sheet


def test_chat_open_picks_the_session_and_shows_its_window(open_page):
    page = open_page()
    chats = []
    page.on("request", lambda r: chats.append(r.url) if "/api/cave/chat/" in r.url else None)
    page.evaluate("CCChat.open('api')")
    page.wait_for_selector("#chat-log .msg")
    assert page.evaluate("document.documentElement.dataset.sheet") == "chat"
    assert page.is_checked("#chat-win")
    assert any(u.endswith("/api/cave/chat/api") for u in chats)
    assert page.locator("#chat-log .msg").count() == 2
    assert "Move the e2e job" in page.inner_text("#chat-log")
    assert "Two jobs are green" in page.inner_text("#chat-log")


def test_the_header_shows_the_figure_in_its_live_state_and_the_name(open_page):
    page = open_page()
    page.evaluate("CCChat.open('api')")
    page.wait_for_selector('#chat-title .spr[data-st="work"]')
    size = box(page, "#chat-title .spr .identity-sprite")
    assert (size["w"], size["h"]) == (36, 36)
    text = page.inner_text("#chat-title")
    assert "api" in text and "working" in text


def test_the_picker_is_hidden_in_a_sheet_and_back_in_the_tab(open_page):
    page = open_page()
    page.evaluate("CCChat.open('api')")
    page.wait_for_selector("#chat-log .msg")
    assert page.locator("#chat-to").is_hidden()
    assert page.locator('label[for="chat-to"]').is_hidden()
    page.evaluate("CC.show('chat')")
    assert page.locator("#chat-to").is_visible()
    assert page.locator('label[for="chat-to"]').is_visible()


def test_his_messages_sit_right_and_the_sessions_carry_its_hue(open_page):
    page = open_page()
    page.evaluate("CCChat.open('api')")
    page.wait_for_selector("#chat-log .msg.me")
    assert css(page, "#chat-log .msg.me", "alignSelf") == "flex-end"
    assert css(page, "#chat-log .msg.agent", "borderLeftWidth") == "2px"
    assert css(page, "#chat-log .msg.agent", "borderTopWidth") == "0px"


def test_send_from_the_sheet_posts_to_that_session(open_page):
    page = open_page()
    posts = []
    page.route("**/api/cave/send", lambda r: (posts.append(r.request.post_data_json), r.fulfill(json={"ok": True})))
    page.evaluate("CCChat.open('api')")
    page.wait_for_selector("#chat-log .msg")
    page.fill("#chat-text", "Open the PR when it is green.")
    page.click('#chat-send button[type="submit"]')
    page.wait_for_function("document.querySelector('#chat-text').value === ''")
    assert posts == [{"to": "api", "text": "Open the PR when it is green."}]


def test_a_long_log_scrolls_inside_the_sheet_and_the_box_stays_in_reach(open_page):
    many = [{"role": "me" if i % 2 == 0 else "agent", "at": 1700000000 + i,
             "to": "api", "from": "api", "text": f"Message number {i} in a long log."} for i in range(80)]
    page = open_page(messages=many)
    page.evaluate("CCChat.open('api')")
    page.wait_for_function("document.querySelectorAll('#chat-log .msg').length === 80")
    sheet_scroll = page.eval_on_selector('#views > section[data-id="chat"]',
                                         "e => ({sh: e.scrollHeight, ch: e.clientHeight})")
    assert sheet_scroll["sh"] > sheet_scroll["ch"]
    assert page.evaluate("window.scrollY") == 0
    send_box = box(page, "#chat-send")
    assert 0 <= send_box["top"] and send_box["bottom"] <= 844


def test_an_ask_stays_above_a_long_log(open_page):
    many = [{"role": "agent", "at": 1700000000 + i, "from": "api",
             "text": f"Line {i} of the transcript."} for i in range(80)]
    asks = [{"id": "a1", "tool": "Bash", "what": "npm run migrate"}]
    page = open_page(messages=many, asks=asks)
    page.evaluate("CCChat.open('api')")
    page.wait_for_selector("#chat-asks [data-cave-ask]")
    ask_box = box(page, "#chat-asks")
    assert 0 <= ask_box["top"] < 844


def test_text_is_never_markup(open_page):
    bad = [{"role": "agent", "at": 1700000000, "from": "api", "text": "<img src=x onerror=window.__hit=1>"}]
    page = open_page(messages=bad)
    page.evaluate("CCChat.open('api')")
    page.wait_for_selector("#chat-log .msg")
    assert page.locator("#chat-log img").count() == 0
    assert page.evaluate("window.__hit") is None


# Task 3: the card opens it


def card(name):
    return f'.scard:has(h3:text-is("{name}"))'


def test_a_tap_on_a_card_opens_that_sessions_chat(open_page):
    page = open_page()
    page.click(f"{card('web')} .scard-main")
    page.wait_for_selector('#views > section.sheet[data-id="chat"]')
    page.wait_for_function("document.getElementById('chat-title').textContent.includes('web')")
    assert "web" in page.inner_text("#chat-title")
    assert page.locator(f"{card('web')} .scard-more").count() == 0


def test_the_toggle_opens_details_and_not_chat(open_page):
    page = open_page()
    toggle = f"{card('api')} .scard-toggle"
    assert page.get_attribute(toggle, "aria-expanded") == "false"
    page.click(toggle)
    assert page.locator(f"{card('api')} .scard-more").count() == 1
    assert page.get_attribute(toggle, "aria-expanded") == "true"
    assert page.locator('#views > section.sheet').count() == 0


def test_the_toggle_is_tall_enough_to_tap_and_names_its_session(open_page):
    page = open_page()
    roboto(page)
    toggle = f"{card('api')} .scard-toggle"
    size = box(page, toggle)
    assert size["w"] >= 44 and size["h"] >= 44
    assert page.get_attribute(toggle, "aria-label") == "Details for api"
    assert page.get_attribute(f"{card('api')} .scard-main", "aria-label") == "Chat with api"
    assert page.get_attribute(f"{card('api')} .scard-main", "aria-expanded") is None


