import json
import os
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("fastapi")
sync_api = pytest.importorskip("playwright.sync_api")
import uvicorn  # noqa: E402

from agent_companion import server  # noqa: E402

TOKEN = "t" * 32
OWNER = "o" * 64
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
REPOS = [{"repo": "site", "checkouts": [{"name": "site", "branch": "main"}, {"name": "site-feat", "branch": "feat"}]}]
TREES = {"": {"dirs": ["app"], "files": ["logo.png"]}, "app": {"dirs": [], "files": ["x.py"]}}
FILES = {"app/x.py": {"text": "a = 1\n", "editable": True, "sha": "sha-one", "cut": False, "crlf": False},
         "logo.png": {"binary": True, "image": True, "editable": False}}


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    root = tmp_path_factory.mktemp("files-page")
    config = root / "config.toml"
    config.write_text("")
    previous = os.environ.get("AGENT_COMPANION_CONFIG")
    os.environ["AGENT_COMPANION_CONFIG"] = str(config)
    original = server.FEATURES
    server.FEATURES = ["files"]
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
    seen = {"saves": [], "raw": [], "answers": [{"ok": False, "conflict": True, "detail": "It changed since you opened it."},
                                                {"ok": True, "committed": True, "sha": "sha-two"}]}
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        context = browser.new_context(viewport={"width": 390, "height": 844})
        page = context.new_page()

        def files_api(route):
            url = urlparse(route.request.url)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path.endswith("/save"):
                seen["saves"].append(json.loads(route.request.post_data))
                route.fulfill(json=seen["answers"].pop(0))
            elif url.path.endswith("/raw"):
                seen["raw"].append((route.request.url, route.request.headers.get("x-agent-companion-key")))
                route.fulfill(status=200, content_type="image/png", body=PNG)
            elif url.path.endswith("/tree"):
                route.fulfill(json={"ok": True, "checkout": q["checkout"], "branch": "main", "folder": q.get("path", ""),
                                    **TREES[q.get("path", "")], "commit": None, "count": 3, "readme": "", "readme_text": ""})
            elif url.path.endswith("/file"):
                route.fulfill(json={"ok": True, "checkout": q["checkout"], "path": q["path"], "bytes": 6,
                                    "status": "clean", "branch": "main", **FILES[q["path"]]})
            else:
                route.fulfill(json={"ok": True, "repos": REPOS, "agents": {}})

        page.route("**/api/files**", files_api)
        page.add_init_script(f"localStorage.setItem('cc-owner-key', '{OWNER}')")
        page.goto(f"{base_url}/")
        yield page, seen
        browser.close()


def test_a_save_sends_the_checkout_and_the_sha_and_says_a_conflict(page):
    page, seen = page
    page.wait_for_selector(".files .tree")
    assert page.locator("select[data-checkout] option").all_inner_texts() == ["site · main", "site-feat · feat"]
    page.get_by_role("button", name="app").click()
    page.get_by_role("button", name="x.py").click()
    page.locator(".files textarea").fill("a = 2\n")
    page.get_by_role("button", name="Save").click()
    page.get_by_text("It changed since you opened it.").wait_for()
    assert seen["saves"] == [{"checkout": "site", "path": "app/x.py", "text": "a = 2\n", "expect": "sha-one"}]
    assert page.locator(".files textarea").input_value() == "a = 2\n", "a refused save keeps the edit"
    page.get_by_role("button", name="Save").click()
    page.get_by_text("Saved and committed. Nothing was pushed.").wait_for()


def test_an_image_opens_read_only_with_the_key_in_a_header(page):
    page, seen = page
    page.wait_for_selector(".files .tree")
    page.get_by_role("button", name="logo.png").click()
    page.locator(".files img.shot").wait_for()
    assert page.locator(".files img.shot").get_attribute("src").startswith("blob:")
    assert page.locator(".files textarea").count() == 0 and page.get_by_role("button", name="Save").count() == 0
    url, key = seen["raw"][0]
    assert key == OWNER and "k=" not in url
