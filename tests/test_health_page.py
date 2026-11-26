import pytest

pytest.importorskip("fastapi")
sync_api = pytest.importorskip("playwright.sync_api")

from agent_companion import server  # noqa: E402

TOKEN = "t" * 32
OWNER = "o" * 64
HEALTH = {
    "cpu": {"percent": 34.0}, "load": {"one": 0.4, "five": 0.5, "fifteen": 0.6},
    "memory": {"percent": 61.0}, "swap": {"reason": "No swap is configured."},
    "temperature": {"reason": "not visible inside WSL"},
    "disks": [{"path": "/", "percent": 72.0, "used_bytes": 72 * 1024, "total_bytes": 100 * 1024}],
    "gpu": {"reason": "No NVIDIA or AMD GPU command was found on PATH."},
    "processes": [{"command": "safe-long-command-name-that-must-not-widen-the-card", "percent": 12.0}],
}


@pytest.fixture(scope="module")
def base_url():
    import socket
    import threading
    import time
    import uvicorn

    original = server.FEATURES
    server.FEATURES = ["health"]
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


def test_machine_card_fits_a_390px_page_and_explains_missing_readings(base_url):
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_context(viewport={"width": 390, "height": 844}).new_page()
        page.route("**/api/health", lambda route: route.fulfill(json=HEALTH))
        page.add_init_script(f"localStorage.setItem('cc-owner-key', '{OWNER}')")
        page.goto(f"{base_url}/?view=health")
        page.wait_for_selector(".machine")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        text = page.locator(".machine").inner_text()
        assert "CPU use" in text and "34%" in text and "not visible inside WSL" in text
        assert "safe-long-command-name-that-must-not-widen-the-card" in text
        assert page.locator(".machine b").count() == 0
        browser.close()


def test_pictures_for_a_person_to_read(base_url):
    import base64
    import os
    import pathlib
    out = os.environ.get("LOOK_SHOTS")
    if not out:
        pytest.skip("set LOOK_SHOTS=<folder> to write health-dark.png and health-light.png")
    pathlib.Path(out).mkdir(parents=True, exist_ok=True)
    font = (pathlib.Path(__file__).resolve().parent / "fixtures" / "Roboto-Regular.ttf").read_bytes()
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        for scheme in ("dark", "light"):
            page = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme=scheme).new_page()
            page.route("**/api/health", lambda route: route.fulfill(json=HEALTH))
            page.add_init_script(f"localStorage.setItem('cc-owner-key', '{OWNER}')")
            page.goto(f"{base_url}/?view=health")
            page.wait_for_selector(".machine")
            page.add_style_tag(content="@font-face{font-family:PhoneRoboto;src:url(data:font/ttf;base64,"
                               + base64.b64encode(font).decode() + ")}"
                               "body{font-family:PhoneRoboto,sans-serif !important}")
            page.evaluate("document.fonts.ready")
            page.screenshot(path=os.path.join(out, f"health-{scheme}.png"))
        browser.close()
