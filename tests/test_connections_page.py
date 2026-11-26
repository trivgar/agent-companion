import pytest

pytest.importorskip("fastapi")
sync_api = pytest.importorskip("playwright.sync_api")

from agent_companion import server  # noqa: E402
from agent_companion.server import connections  # noqa: E402

TOKEN = "t" * 32
OWNER = "o" * 64
ROWS = [
    {"id": "claude", "name": "Claude Code", "state": "ok", "detail": "2.1.0", "fix": ""},
    {"id": "push", "name": "Push", "state": "off", "detail": "No device subscribed " + "x" * 120,
     "fix": "Open the Push tab on your phone and press Enable (INSTALL.md step 8)."},
    {"id": "mail", "name": "Mail <b>x</b>", "state": "broken", "detail": "Last run failed", "fix": "See INSTALL.md step 11."},
]


@pytest.fixture(scope="module")
def base_url():
    import threading
    import time
    import uvicorn
    orig = server.FEATURES
    server.FEATURES = ["connections"]
    app = server.create_app(token=TOKEN, owner_key=OWNER)
    server.FEATURES = orig
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{srv.servers[0].sockets[0].getsockname()[1]}"
    srv.should_exit = True


def test_connections_page_at_phone_width(base_url):
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        pg = b.new_context(viewport={"width": 390, "height": 844}).new_page()
        posts = []
        pg.route("**/api/connections", lambda r: r.fulfill(json=ROWS))
        pg.route("**/api/connections/recheck", lambda r: (posts.append(1), r.fulfill(json={"ok": True})))
        pg.add_init_script(f"localStorage.setItem('cc-owner-key', '{OWNER}')")
        pg.goto(f"{base_url}/?view=connections")
        pg.wait_for_selector(".conn")
        assert pg.locator(".conn").count() == 3
        assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        text = pg.locator("#views").inner_text()
        for bad in ("null", "NaN", "undefined"):
            assert bad not in text
        assert pg.locator(".conn b").count() == 0          # escaped
        assert pg.locator(".conn.ok .fix").count() == 0
        assert pg.locator(".conn.off .fix").count() == 1 and pg.locator(".conn.broken .fix").count() == 1
        assert "Broken" in pg.locator(".conn.broken .state").inner_text()
        pg.locator("#conn-recheck").click()
        pg.wait_for_function("true")
        assert posts
        b.close()


def test_pictures_for_a_person_to_read(base_url):
    import base64
    import os
    import pathlib
    out = os.environ.get("LOOK_SHOTS")
    if not out:
        pytest.skip("set LOOK_SHOTS=<folder> to write connections-dark.png and connections-light.png")
    pathlib.Path(out).mkdir(parents=True, exist_ok=True)
    font = (pathlib.Path(__file__).resolve().parent / "fixtures" / "Roboto-Regular.ttf").read_bytes()
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        for scheme in ("dark", "light"):
            pg = b.new_context(viewport={"width": 390, "height": 844}, color_scheme=scheme).new_page()
            pg.route("**/api/connections", lambda r: r.fulfill(json=ROWS))
            pg.add_init_script(f"localStorage.setItem('cc-owner-key', '{OWNER}')")
            pg.goto(f"{base_url}/?view=connections")
            pg.wait_for_selector(".conn")
            pg.add_style_tag(content="@font-face{font-family:PhoneRoboto;src:url(data:font/ttf;base64,"
                             + base64.b64encode(font).decode() + ")}"
                             "body{font-family:PhoneRoboto,sans-serif !important}")
            pg.evaluate("document.fonts.ready")
            pg.screenshot(path=os.path.join(out, f"connections-{scheme}.png"))
        b.close()
