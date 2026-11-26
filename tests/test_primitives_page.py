"""The shared primitives and controls in base.css at 390px, dark and light."""
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
    """open_page(scheme) gives a loaded page at 390px; every page closes with the test."""
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # browser not installed
            pytest.skip(f"chromium unavailable: {exc}")

        def make(scheme="dark"):
            ctx = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme=scheme,
                                      service_workers="block")
            pg = ctx.new_page()
            pg.route("**/api/sessions*", lambda r: r.fulfill(json=[]))
            pg.route("**/api/usage*", lambda r: r.fulfill(json=USAGE))
            pg.add_init_script(f"localStorage.setItem('cc-owner-key', '{OWNER}')")
            pg.goto(f"{base_url}/")
            pg.wait_for_selector("#nav button[data-id]")
            roboto(pg)
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
                                 "return {x: r.x, y: r.y, w: r.width, h: r.height, top: r.top, bottom: r.bottom} }")


def token_color(page, token):
    return page.evaluate(f"""(() => {{
      const d = document.createElement('i');
      d.style.color = 'var({token})';
      document.body.appendChild(d);
      const c = getComputedStyle(d).color;
      d.remove();
      return c;
    }})()""")


def inject(page, html):
    page.evaluate("(h) => { document.querySelector('main').innerHTML = h; }", html)


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_sec_head_is_uppercase_under_twelve_pixels_and_faint(open_page, scheme):
    page = open_page(scheme)
    inject(page, '<div class="sec-head">Group <span class="n">3</span></div>')
    assert css(page, ".sec-head", "textTransform") == "uppercase"
    assert float(css(page, ".sec-head", "fontSize").rstrip("px")) < 12
    assert css(page, ".sec-head", "color") == token_color(page, "--faint")
    assert css(page, ".sec-head .n", "color") == token_color(page, "--dim")


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_row_wraps_a_long_body_and_keeps_trail_on_the_first_line_at_the_right(open_page, scheme):
    page = open_page(scheme)
    long_text = "x" * 300
    inject(page, f'<div class="row"><span class="lead">api</span>'
                 f'<span class="body">{long_text}</span>'
                 f'<span class="trail">42ms</span></div>')
    row_b = box(page, ".row")
    body_b = box(page, ".row > .body")
    trail_b = box(page, ".row > .trail")
    assert body_b["h"] > trail_b["h"] * 1.5
    assert abs(trail_b["y"] - body_b["y"]) < 6
    assert abs((row_b["x"] + row_b["w"]) - (trail_b["x"] + trail_b["w"])) <= 1
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_chip_does_not_wrap_and_uses_the_strong_line(open_page, scheme):
    page = open_page(scheme)
    inject(page, '<div style="width:40px"><span class="chip">branch name here</span></div>')
    assert css(page, ".chip", "whiteSpace") == "nowrap"
    assert css(page, ".chip", "borderTopColor") == token_color(page, "--line-strong")


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_meter_fill_uses_dim_fg_and_alert_by_level(open_page, scheme):
    page = open_page(scheme)
    inject(page, '<div class="meter" id="m-plain"><i style="width:30%"></i></div>'
                 '<div class="meter high" id="m-high"><i style="width:80%"></i></div>'
                 '<div class="meter needs" id="m-needs"><i style="width:95%"></i></div>')
    assert css(page, "#m-plain > i", "backgroundColor") == token_color(page, "--dim")
    assert css(page, "#m-high > i", "backgroundColor") == token_color(page, "--fg")
    assert css(page, "#m-needs > i", "backgroundColor") == token_color(page, "--alert")


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_act_buttons_and_fields_are_forty_four_pixels_tall_with_focus_outline(open_page, scheme):
    page = open_page(scheme)
    inject(page, '<button class="act" id="b-act">Cancel</button>'
                 '<button class="act primary" id="b-pri">Save</button>'
                 '<input type="text" id="f-in" value="x">'
                 '<textarea id="f-ta" rows="1">x</textarea>'
                 '<select id="f-sel"><option>one</option></select>')
    for sel in ("#b-act", "#b-pri", "#f-in", "#f-ta", "#f-sel"):
        assert box(page, sel)["h"] >= 44, sel
    assert css(page, "#b-pri", "backgroundColor") == token_color(page, "--fg")
    page.keyboard.press("Tab")
    page.focus("#b-act")
    assert page.eval_on_selector("#b-act", "e => e.matches(':focus-visible')") is True
    assert css(page, "#b-act", "outlineWidth") == "2px"
    assert css(page, "#b-act", "outlineStyle") == "solid"


def _rgb(color_str):
    nums = [int(n) for n in re.findall(r"\d+", color_str)[:3]]
    return nums[0], nums[1], nums[2]


def _luminance(rgb):
    def chan(c):
        v = c / 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def _contrast(c1, c2):
    l1, l2 = _luminance(_rgb(c1)), _luminance(_rgb(c2))
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_text_tokens_meet_wcag_aa_on_every_ground(open_page, scheme):
    page = open_page(scheme)
    pairs = [
        ("--dim", "--raised"),
        ("--dim", "--sunk"),
        ("--faint", "--raised"),
        ("--faint", "--sunk"),
        ("--dim", "--sky-top"),
    ]
    for fg, bg in pairs:
        ratio = _contrast(token_color(page, fg), token_color(page, bg))
        assert ratio >= 4.5, f"{scheme} {fg} on {bg}: {ratio:.2f}"
