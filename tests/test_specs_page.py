import os
import socket
import threading
import time

import pytest

pytest.importorskip("fastapi")
sync_api = pytest.importorskip("playwright.sync_api")
import uvicorn  # noqa: E402

from agent_companion import server  # noqa: E402

TOKEN = "t" * 32
OWNER = "o" * 64
DOCS = [
    {"id": "api/specs/example.md", "project": "api", "kind": "specs", "name": "example.md",
     "title": "Example <i>spec</i>", "date": "2026-05-01", "modified": 1, "bytes": 80, "repo": "api"},
    {"id": "web/plans/work.md", "project": "web", "kind": "plans", "name": "work.md",
     "title": "Work plan", "date": "2026-04-01", "modified": 1, "bytes": 9, "repo": "web"},
]
TEXT = "<img src=x onerror=alert(1)>\n# Heading\n```html\n<b>literal</b>\n```\n"


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    root = tmp_path_factory.mktemp("specs-page")
    config = root / "config.toml"
    config.write_text("")
    previous = os.environ.get("AGENT_COMPANION_CONFIG")
    os.environ["AGENT_COMPANION_CONFIG"] = str(config)
    original = server.FEATURES
    server.FEATURES = ["specs"]
    app = server.create_app(token=TOKEN, owner_key=OWNER)
    server.FEATURES = original
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        pytest.skip(f"loopback sockets unavailable: {exc}")
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=srv.run, args=([listener],), daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=5)
    listener.close()
    if previous is None:
        os.environ.pop("AGENT_COMPANION_CONFIG", None)
    else:
        os.environ["AGENT_COMPANION_CONFIG"] = previous


@pytest.fixture
def page(base_url):
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        context = browser.new_context(viewport={"width": 390, "height": 844})
        page = context.new_page()

        def specs_api(route):
            if "/doc?" in route.request.url:
                assert "id=api%2Fspecs%2Fexample.md" in route.request.url
                route.fulfill(json={"ok": True, **DOCS[0], "text": TEXT, "cut": True, "agents": []})
            else:
                route.fulfill(json={"ok": True, "docs": DOCS, "agents": {}})

        page.route("**/api/specs**", specs_api)
        page.add_init_script(f"localStorage.setItem('cc-owner-key', '{OWNER}')")
        page.goto(f"{base_url}/")
        yield page
        browser.close()


def test_document_text_is_escaped_and_filters_are_available(page):
    page.wait_for_selector(".spec-row")
    assert page.locator(".spec-row").count() == 2
    page.get_by_role("button", name="plans", exact=True).click()
    assert page.locator(".spec-row").count() == 1
    page.get_by_role("button", name="All kinds").click()
    page.locator(".spec-row").first.click()
    page.locator(".spec-document").wait_for()
    assert page.locator(".spec-document img").count() == 0
    assert page.locator(".spec-document b").count() == 0
    assert page.locator(".spec-document").inner_text() == TEXT
    assert page.get_by_text("Only the start of this document is shown.").is_visible()
