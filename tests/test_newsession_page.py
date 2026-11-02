"""The New session sheet reads the listing's keys and labels and posts a key
and a first message. Both routes are answered by the browser: nothing starts."""
import json
import re

import pytest

pytest.importorskip("fastapi")
sync_api = pytest.importorskip("playwright.sync_api")

from agent_companion import server  # noqa: E402

TOKEN = "t" * 32
OWNER = "o" * 64
PROJECTS = {"ok": True, "default_model": "", "projects": [
    {"key": "site", "label": "Web site", "sessions": [{"speaker": "Ada", "pid": 1}], "note": "Ada is in Web site",
     "available": True, "reason": ""},
    {"key": "api", "label": "API", "sessions": [], "note": "", "available": True, "reason": ""},
    {"key": "gone", "label": "Gone", "sessions": [], "note": "", "available": False,
     "reason": "Folder does not exist"}]}


@pytest.fixture(scope="module")
def base_url():
    import socket
    import threading
    import time
    import uvicorn

    original = server.FEATURES
    server.FEATURES = ["sessions"]
    app = server.create_app(token=TOKEN, owner_key=OWNER)
    server.FEATURES = original
    try:
        sock = socket.socket()
    except PermissionError as exc:
        pytest.skip(f"sandbox cannot open the browser-test socket: {exc}")
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    service = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    threading.Thread(target=lambda: service.run(sockets=[sock]), daemon=True).start()
    for _ in range(100):
        if service.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    service.should_exit = True


def _json(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def test_the_sheet_posts_a_key_and_a_message_and_says_what_happened(base_url):
    posted = []
    answers = [({"ok": False, "tmux": "claude-site", "pid": None,
                 "detail": "No Claude started in claude-site within 12 seconds."}, 500),
               ({"ok": True, "tmux": "claude-site-2", "pid": 5, "detail": ""}, 200)]

    def start(route):
        posted.append(json.loads(route.request.post_data))
        _json(route, *answers.pop(0))

    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_context(viewport={"width": 390, "height": 844}).new_page()
        page.route("**/api/sessions", lambda route: _json(route, []))
        page.route(re.compile(r"/api/newsession/projects(\?|$)"), lambda route: _json(route, PROJECTS))
        page.route(re.compile(r"/api/newsession(\?|$)"), start)
        page.add_init_script(f"localStorage.setItem('cc-owner-key', '{OWNER}')")
        page.goto(f"{base_url}/?view=sessions")
        page.click("[data-new-session]")
        page.wait_for_selector(".new-session-sheet select[name=project]")
        options = page.eval_on_selector_all(".new-session-sheet option", "els => els.map(o => [o.value, o.textContent, o.disabled])")
        assert options == [["site", "Web site", False], ["api", "API", False], ["gone", "Gone (Folder does not exist)", True]]
        assert page.text_content("[data-new-session-note]") == "Ada is in Web site"
        page.select_option(".new-session-sheet select[name=project]", "api")
        assert page.text_content("[data-new-session-note]") == ""
        page.select_option(".new-session-sheet select[name=project]", "site")
        page.fill(".new-session-sheet textarea[name=message]", "fix the build")
        page.click(".new-session-sheet button[type=submit]")
        page.wait_for_selector("text=Not started: No Claude started in claude-site within 12 seconds.")
        page.click(".new-session-sheet button[type=submit]")
        page.wait_for_selector("text=Started claude-site-2.")
        assert posted == [{"project": "site", "message": "fix the build"}] * 2
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        browser.close()
