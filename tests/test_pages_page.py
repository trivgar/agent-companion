"""The Pages feature is present in the dashboard nav and seals its frame."""
from __future__ import annotations

import socket
import threading
import time

import pytest

pytest.importorskip("fastapi")
sync_api = pytest.importorskip("playwright.sync_api")
import uvicorn  # noqa: E402

from agent_companion import pages, server  # noqa: E402


TOKEN = "t" * 32
OWNER = "o" * 64


def test_pages_nav_direct_link_frame_and_delete_at_phone_width(tmp_path, monkeypatch):
    root = tmp_path / "pages"
    monkeypatch.setenv("AGENT_COMPANION_PAGES_DIR", str(root))
    record, _ = pages.Store(root).publish(
        html="<h1>Published page</h1>", title="A very long published page title " * 8,
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
            browser.close()
    finally:
        service.should_exit = True
        thread.join(timeout=5)
        listener.close()
