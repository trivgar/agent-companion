"""The sessions tab's cards and the figure at 390px."""
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
    row(1, "web", waiting=True),
    row(2, "api", status="busy", last_activity=4),
    row(3, "infra", status="busy", context_pct=72, branch="ci/arm64", title="Runners", last_activity=180,
        bg_job_list=[{"cmd": "make e2e", "age_s": 61}]),
    row(4, "docs", last_activity=2820, last_reply="Draft is in the changelog."),
    row(5, "mobile", last_activity=7200, last_reply="Pushed the fix."),
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
    """open_page(scheme, rows=..., reduced=...) gives a loaded page at 390px."""
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # browser not installed
            pytest.skip(f"chromium unavailable: {exc}")

        def make(scheme="dark", rows=None, reduced="no-preference", wait=True):
            rows = ROWS if rows is None else rows
            ctx = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme=scheme,
                                      reduced_motion=reduced, service_workers="block")
            pg = ctx.new_page()
            pg.route("**/api/sessions*", lambda r: r.fulfill(json=rows))
            pg.route("**/api/usage*", lambda r: r.fulfill(json=USAGE))
            pg.add_init_script(f"localStorage.setItem('cc-owner-key', '{OWNER}')")
            pg.goto(f"{base_url}/")
            pg.wait_for_selector("#nav button[data-id]")
            if wait:
                pg.wait_for_selector(".scard")
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
    return page.eval_on_selector(selector, "e => { const r = e.getBoundingClientRect(); "
                                 "return {x: r.x, w: r.width, h: r.height, bottom: r.bottom} }")


def card(name):
    return f'.scard:has(h3:text-is("{name}"))'


# Task 1: the figure


def test_the_same_name_draws_the_same_figure(open_page):
    page = open_page()
    same = page.evaluate("CCSprite.svg('api') === CCSprite.svg('api')")
    svg = page.evaluate("CCSprite.svg('api')")
    assert same is True
    assert 'viewBox="0 0 32 32"' in svg
    assert 'class="identity-sprite"' in svg


def test_two_names_can_differ_by_more_than_hue(open_page):
    page = open_page()
    count = page.evaluate("""() => {
      const seen = new Set();
      for (const c of 'abcdefghijklmnopqrstuvwxyz') {
        seen.add(CCSprite.svg(c).replace(/hsl\\([^)]*\\)/g, 'X'));
      }
      return seen.size;
    }""")
    assert count > 8


def test_a_pinned_colour_moves_the_body(open_page):
    page = open_page()
    svg = page.evaluate("(CCIdentity.setOverrides({api: '#ff0000'}), CCSprite.svg('api'))")
    assert "hsl(0 " in svg


def test_state_is_wait_work_full_sleep_idle_in_that_order(open_page):
    page = open_page()
    got = page.evaluate("""() => [
      {waiting: true, status: 'busy', context_pct: 90},
      {status: 'busy', context_pct: 90},
      {status: 'idle', context_pct: 85, last_activity: 9000},
      {status: 'idle', context_pct: 10, last_activity: 3601},
      {status: 'idle', context_pct: 10, last_activity: 3600},
      {status: 'idle', context_pct: 84, last_activity: 5},
    ].map(r => CCSprite.state(r, 0))""")
    assert got == ["wait", "work", "full", "sleep", "idle", "idle"]


def test_the_label_names_the_session_and_escapes_it(open_page):
    page = open_page()
    svg = page.evaluate("CCSprite.svg('<b>x</b>')")
    assert 'aria-label="Session sprite for &lt;b&gt;x&lt;/b&gt;"' in svg


# Task 2: the card


def names(page):
    return page.eval_on_selector_all(".scard h3", "els => els.map(e => e.textContent)")


def groups(page):
    return page.eval_on_selector_all("h2.grp", "els => els.map(e => e.textContent.toLowerCase().split(/\\s+/).join(' ').trim())")


def test_cards_are_grouped_needs_you_working_idle(open_page):
    page = open_page()
    assert groups(page) == ["needs you 1", "working 2", "idle 2"]
    assert names(page) == ["web", "api", "infra", "docs", "mobile"]


def test_an_empty_group_is_not_drawn(open_page):
    page = open_page(rows=ROWS[3:])
    assert groups(page) == ["idle 2"]


def test_a_closed_card_shows_its_figure_name_project_and_last_word_only(open_page):
    page = open_page()
    for part in (".spr .identity-sprite", "h3", ".proj", ".said"):
        assert page.locator(f"{card('infra')} {part}").count() == 1
    assert page.locator(f"{card('infra')} .scard-more").count() == 0
    assert page.get_attribute(f"{card('infra')} .scard-main", "aria-expanded") == "false"


def test_a_tap_opens_the_details_and_a_refresh_keeps_them_open(open_page):
    page = open_page()
    main = f"{card('infra')} .scard-main"
    page.click(main)
    for part in (".scard-more .meta", ".bar", ".bg-jobs", '[data-card-action="stop"]'):
        assert page.locator(f"{card('infra')} {part}").count() == 1
    assert page.get_attribute(main, "aria-expanded") == "true"
    page.evaluate("CC.show('sessions')")
    page.wait_for_timeout(400)
    assert page.get_attribute(main, "aria-expanded") == "true"
    assert page.locator(f"{card('infra')} .scard-more").count() == 1
    page.click(main)
    assert page.get_attribute(main, "aria-expanded") == "false"
    assert page.locator(f"{card('infra')} .scard-more").count() == 0


def test_enter_opens_a_focused_card(open_page):
    page = open_page()
    main = f"{card('api')} .scard-main"
    page.focus(main)
    page.keyboard.press("Enter")
    assert page.get_attribute(main, "aria-expanded") == "true"
    page.keyboard.press("Space")
    assert page.get_attribute(main, "aria-expanded") == "false"


def test_context_shows_in_the_details_only_past_seventy(open_page):
    page = open_page()
    page.click(f"{card('infra')} .scard-main")
    page.click(f"{card('api')} .scard-main")
    assert "context 72%" in page.inner_text(f"{card('infra')} .meta")
    assert "context" not in page.inner_text(f"{card('api')} .meta")


def test_an_old_last_word_carries_its_age(open_page):
    page = open_page()
    assert page.inner_text(f"{card('docs')} .said .when").strip() == "47m ago"
    assert page.locator(f"{card('api')} .said .when").count() == 0


def test_a_waiting_card_takes_the_alert_edge_and_says_needs_you(open_page):
    page = open_page()
    alert = page.evaluate("(() => { const d = document.createElement('i'); d.style.color='var(--alert)'; "
                          "document.body.appendChild(d); return getComputedStyle(d).color })()")
    badge = ".scard.waiting .wbadge"
    assert page.inner_text(badge).strip().lower() == "needs you"
    assert css(page, badge, "color") == alert
    assert css(page, ".scard.waiting", "borderTopColor") == alert


def test_an_idle_card_steps_back_without_dimming_its_text(open_page):
    page = open_page()
    assert "saturate" in css(page, card("docs"), "filter")
    assert css(page, card("docs"), "opacity") == "1"
    assert float(css(page, f"{card('docs')} .spr .identity-sprite", "opacity")) < 1


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_a_card_is_tinted_in_its_own_hue_in_both_themes(open_page, scheme):
    page = open_page(scheme)
    raised = page.evaluate("(() => { const d = document.createElement('i'); d.style.background='var(--raised)'; "
                           "document.body.appendChild(d); return getComputedStyle(d).backgroundColor })()")
    api, infra = css(page, card("api"), "backgroundColor"), css(page, card("infra"), "backgroundColor")
    assert api != infra
    assert raised not in (api, infra)


def test_the_figure_is_forty_pixels_and_the_card_is_tall_enough_to_tap(open_page):
    page = open_page()
    roboto(page)
    for name in ("web", "api", "infra", "docs", "mobile"):
        assert box(page, f"{card(name)} .scard-main")["h"] >= 44
        size = box(page, f"{card(name)} .spr .identity-sprite")
        assert (size["w"], size["h"]) == (40, 40)


def test_overlays_follow_state(open_page):
    page = open_page()
    got = {n: page.get_attribute(f"{card(n)} .spr", "data-st") for n in ("web", "api", "docs", "mobile")}
    assert got == {"web": "wait", "api": "work", "docs": "idle", "mobile": "sleep"}


def test_reduced_motion_stills_the_working_figure(open_page):
    sel = '.spr[data-st="work"] .identity-sprite'
    assert css(open_page(reduced="reduce"), sel, "animationName") == "none"
    assert css(open_page(), sel, "animationName") != "none"


def test_names_and_replies_are_escaped(open_page):
    rows = [row(9, "beta <b>x</b>", last_reply="<img src=x onerror=window.__hit=1>")]
    page = open_page(rows=rows)
    assert page.locator(".scard b").count() == 0
    assert page.locator(".scard img").count() == 0
    assert page.evaluate("window.__hit") is None
    assert "beta <b>x</b>" in page.inner_text(".scard h3")


def test_the_page_does_not_scroll_sideways_with_long_names_in_roboto(open_page):
    rows = [row(9, "n" * 60, project="p" * 60, last_reply="r" * 120)]
    page = open_page(rows=rows)
    roboto(page)
    page.click(".scard-main")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_pictures_for_a_person_to_read(open_page):
    out = os.environ.get("CARD_SHOTS")
    if not out:
        pytest.skip("set CARD_SHOTS=<folder> to write cards-dark.png and cards-light.png")
    for scheme in ("dark", "light"):
        page = open_page(scheme)
        roboto(page)
        page.click(f"{card('infra')} .scard-main")
        page.wait_for_selector(f"{card('infra')} .scard-more")
        page.screenshot(path=os.path.join(out, f"cards-{scheme}.png"), full_page=True)
