"""The Pages feature is present in the dashboard nav and seals its frame."""
from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager

import pytest

pytest.importorskip("fastapi")
sync_api = pytest.importorskip("playwright.sync_api")
import uvicorn  # noqa: E402

from agent_companion import pages, server  # noqa: E402


TOKEN = "t" * 32
OWNER = "o" * 64


@contextmanager
def _dashboard(tmp_path, monkeypatch, html, title):
    """One published page and a dashboard that has only the Pages feature, at
    phone width: yields the browser page, the server's address and the record."""
    root = tmp_path / "pages"
    monkeypatch.setenv("AGENT_COMPANION_PAGES_DIR", str(root))
    record, _ = pages.Store(root).publish(
        html=html, title=title,
        folder=tmp_path, source=tmp_path / "page.html", speaker="agent", project="project",
        household=False,
    )
    original = server.FEATURES
    server.FEATURES = ["pages"]
    app = server.create_app(token=TOKEN, owner_key=OWNER)
    server.FEATURES = original
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        pytest.skip(f"loopback socket unavailable: {exc}")
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    address = f"http://127.0.0.1:{listener.getsockname()[1]}"
    service = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=service.run, args=([listener],), daemon=True)
    thread.start()
    for _ in range(100):
        if service.started:
            break
        time.sleep(0.05)
    try:
        with sync_api.sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as exc:
                pytest.skip(f"chromium unavailable: {exc}")
            page = browser.new_context(viewport={"width": 390, "height": 844}).new_page()
            page.add_init_script(f"localStorage.setItem('cc-owner-key', '{OWNER}')")
            yield page, address, record
            browser.close()
    finally:
        service.should_exit = True
        thread.join(timeout=5)
        listener.close()


def test_pages_nav_direct_link_frame_and_delete_at_phone_width(tmp_path, monkeypatch):
    with _dashboard(tmp_path, monkeypatch, "<h1>Published page</h1>",
                    "A very long published page title " * 8) as (page, address, record):
        page.goto(f"{address}/?view=pages&page={record['id']}")
        page.wait_for_selector(".pg-frame")
        assert page.locator('#nav button[data-id="pages"]').count() == 1
        frame = page.locator(".pg-frame")
        assert frame.get_attribute("sandbox") == "allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms"
        assert frame.get_attribute("referrerpolicy") == "no-referrer"
        page.goto(f"{address}/?view=pages")
        page.wait_for_selector(".pg-card")
        page.click(".pg-card")
        page.wait_for_selector(".pg-frame")
        page.click(".pg-back")
        assert page.locator(".pg-card").count() == 1
        page.click(".pg-card")
        page.click(".pg-delete")
        assert page.locator(".pg-delete").inner_text() == "Delete again"
        page.click(".pg-delete")
        page.wait_for_selector(".pg-empty")
        assert "No pages yet. An agent makes one with page publish." in page.locator(".pages").inner_text()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_an_open_page_keeps_its_place_through_the_shells_refresh(tmp_path, monkeypatch):
    """The shell refreshes the showing view every five seconds. Each refresh
    wrote the open page's frame again, so a long page jumped back to its top
    a few lines into reading it."""
    tall = "<body style='margin:0'>" + "".join(f"<p style='height:40px;margin:0'>line {n}</p>" for n in range(200))
    with _dashboard(tmp_path, monkeypatch, tall, "A long page") as (page, address, record):
        page.clock.install()
        page.goto(f"{address}/?view=pages&page={record['id']}")
        page.wait_for_selector(".pg-frame")
        frame = page.query_selector(".pg-frame").content_frame()
        frame.wait_for_selector("p")
        frame.evaluate("() => window.scrollTo(0, 1200)")
        assert frame.evaluate("() => window.scrollY") == 1200
        # Mark this frame, and collect every list request so a refresh can be waited for.
        page.evaluate("""() => {
            document.querySelector('.pg-frame').dataset.mine = '1';
            const real = CC.get;
            window.__lists = [];
            CC.get = (...args) => {
                const asked = real(...args);
                if (args[0] === '/api/pages') window.__lists.push(asked);
                return asked;
            };
        }""")
        for _ in range(2):
            page.clock.run_for(5000)          # the shell's own timer comes due
            asked = page.evaluate("""async () => {
                const lists = window.__lists.splice(0);
                await Promise.all(lists);
                await Promise.resolve();
                return lists.length;
            }""")
            assert asked >= 1, "the shell did not refresh the Pages view"
        assert page.get_attribute(".pg-frame", "data-mine") == "1"      # the same frame, not a new one
        assert frame.evaluate("() => window.scrollY") == 1200
        # Arming Delete changes the button, not the page under it.
        page.click(".pg-delete")
        assert page.locator(".pg-delete").inner_text() == "Delete again"
        assert page.get_attribute(".pg-frame", "data-mine") == "1"
        assert frame.evaluate("() => window.scrollY") == 1200


def test_pictures_for_a_person_to_read(tmp_path, monkeypatch):
    import base64
    import os
    import pathlib
    out = os.environ.get("LOOK_SHOTS")
    if not out:
        pytest.skip("set LOOK_SHOTS=<folder> to write pages-dark.png and pages-light.png")
    pathlib.Path(out).mkdir(parents=True, exist_ok=True)
    font = (pathlib.Path(__file__).resolve().parent / "fixtures" / "Roboto-Regular.ttf").read_bytes()
    with _dashboard(tmp_path, monkeypatch, "<h1>Published page</h1>", "Quarterly benchmark report") as (page, address, _):
        browser = page.context.browser
        for scheme in ("dark", "light"):
            pg = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme=scheme).new_page()
            pg.add_init_script(f"localStorage.setItem('cc-owner-key', '{OWNER}')")
            pg.goto(f"{address}/?view=pages")
            pg.wait_for_selector(".pg-card")
            pg.add_style_tag(content="@font-face{font-family:PhoneRoboto;src:url(data:font/ttf;base64,"
                             + base64.b64encode(font).decode() + ")}"
                             "body{font-family:PhoneRoboto,sans-serif !important}")
            pg.evaluate("document.fonts.ready")
            pg.screenshot(path=os.path.join(out, f"pages-{scheme}.png"))
