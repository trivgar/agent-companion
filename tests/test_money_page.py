import os

import pytest

pytest.importorskip("fastapi")
sync_api = pytest.importorskip("playwright.sync_api")

from agent_companion import money, server  # noqa: E402
from agent_companion.money import demo  # noqa: E402

TOKEN, OWNER = "t" * 32, "o" * 64


class Gappy(demo.DemoSource):
    """Demo figures with one balance and one month's spending unknown."""
    def accounts(self):
        a = super().accounts()
        a[0]["balance"] = None
        return a

    def _total(self):
        return 22900

    def cash_flow(self, months=6):
        r = super().cash_flow(months)
        r[-1]["spending"] = None
        return r


@pytest.fixture(scope="module")
def base_url():
    import threading
    import time
    import uvicorn
    money.register("gappy", Gappy)
    orig, prev = server.FEATURES, os.environ.get("AGENT_COMPANION_MONEY_SOURCE")
    server.FEATURES = ["money"]
    app = server.create_app(token=TOKEN, owner_key=OWNER)
    server.FEATURES = orig
    os.environ["AGENT_COMPANION_MONEY_SOURCE"] = "demo"
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{srv.servers[0].sockets[0].getsockname()[1]}"
    srv.should_exit = True
    if prev is None:
        os.environ.pop("AGENT_COMPANION_MONEY_SOURCE", None)
    else:
        os.environ["AGENT_COMPANION_MONEY_SOURCE"] = prev


def _page(p, base_url):
    try:
        b = p.chromium.launch()
    except Exception as exc:
        pytest.skip(f"chromium unavailable: {exc}")
    pg = b.new_context(viewport={"width": 390, "height": 844}).new_page()
    pg.add_init_script(f"localStorage.setItem('cc-owner-key', '{OWNER}')")
    return b, pg


def _clean(pg):
    body = pg.inner_text("section[data-id=money]")
    for bad in ("null", "NaN", "undefined", "$0.00"):
        assert bad not in body, bad
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    return body


def test_demo_panel_fits_and_is_labelled(base_url):
    with sync_api.sync_playwright() as p:
        b, pg = _page(p, base_url)
        pg.goto(f"{base_url}/")
        pg.wait_for_selector(".mnw svg")
        body = _clean(pg)
        assert pg.locator(".mdemo").inner_text() == "DEMO"
        for want in ("Net worth", "Everyday checking", "Spending this month", "Housing", "Recurring", "Coming up"):
            assert want in body
        assert pg.locator(".mflow svg rect").count() == 12
        b.close()


def test_a_missing_figure_is_a_dash(base_url):
    with sync_api.sync_playwright() as p:
        b, pg = _page(p, base_url)
        os.environ["AGENT_COMPANION_MONEY_SOURCE"] = "gappy"
        try:
            pg.goto(f"{base_url}/")
            pg.wait_for_selector(".mnw svg")
            _clean(pg)
            row = pg.locator(".mrow", has_text="Everyday checking")
            assert row.locator(".amt").inner_text() == "–"
            assert pg.locator(".mflow svg text", has_text="–").count() == 1
        finally:
            os.environ["AGENT_COMPANION_MONEY_SOURCE"] = "demo"
        b.close()


def test_unset_source_points_at_the_docs(base_url):
    with sync_api.sync_playwright() as p:
        b, pg = _page(p, base_url)
        os.environ["AGENT_COMPANION_MONEY_SOURCE"] = ""
        try:
            pg.goto(f"{base_url}/")
            pg.wait_for_selector("section[data-id=money] .empty")
            body = _clean(pg)
            assert "No money source set up" in body and "docs/money-sources.md" in body
        finally:
            os.environ["AGENT_COMPANION_MONEY_SOURCE"] = "demo"
        b.close()


def test_pictures_for_a_person_to_read(base_url):
    import base64
    import pathlib
    out = os.environ.get("LOOK_SHOTS")
    if not out:
        pytest.skip("set LOOK_SHOTS=<folder> to write money-dark.png and money-light.png")
    pathlib.Path(out).mkdir(parents=True, exist_ok=True)
    font = (pathlib.Path(__file__).resolve().parent / "fixtures" / "Roboto-Regular.ttf").read_bytes()
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        for scheme in ("dark", "light"):
            pg = b.new_context(viewport={"width": 390, "height": 844}, color_scheme=scheme).new_page()
            pg.add_init_script(f"localStorage.setItem('cc-owner-key', '{OWNER}')")
            pg.goto(f"{base_url}/")
            pg.wait_for_selector(".mnw svg")
            pg.add_style_tag(content="@font-face{font-family:PhoneRoboto;src:url(data:font/ttf;base64,"
                             + base64.b64encode(font).decode() + ")}"
                             "body{font-family:PhoneRoboto,sans-serif !important}")
            pg.evaluate("document.fonts.ready")
            pg.screenshot(path=os.path.join(out, f"money-{scheme}.png"))
        b.close()
