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
