import re

import pytest

pytest.importorskip("fastapi")
sync_api = pytest.importorskip("playwright.sync_api")

from agent_companion import server  # noqa: E402
from agent_companion.server import auth  # noqa: E402

TOKEN = "t" * 32
OWNER = "o" * 64


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    import os
    import threading
    import time
    import uvicorn
    d = tmp_path_factory.mktemp("settings")
    (d / "tts").mkdir()
    (d / "tts" / "cast.tsv").write_text("Juno\ten_US-amy-medium\nWren\ten_GB-alba-medium\n")
    (d / "config.toml").write_text('collab_repo = "example/app"\nname = "alex"\n')
    env = {"AGENT_COMPANION_TTS_DIR": str(d / "tts"), "COMMS_STATE_DIR": str(d / "state"),
           "COMMS_CLIPS_DIR": str(d / "clips"), "AGENT_COMPANION_CONFIG": str(d / "config.toml"),
           "AGENT_COMPANION_OWNER_KEYS": str(d / "owner-keys.json"), "HOME": str(d / "home")}
    old = {k: os.environ.get(k) for k in (*env, "COMMS_DATA_DIR", "PIPER_BIN")}
    os.environ.update(env)
    os.environ.pop("COMMS_DATA_DIR", None)
    from agent_companion.server import settings as settings_mod
    old_which, settings_mod._which = settings_mod._which, (lambda name: None)
    os.environ.pop("PIPER_BIN", None)
    auth.add_device("phone", OWNER)
    orig = server.FEATURES
    server.FEATURES = ["settings", "comms"]
    app = server.create_app(token=TOKEN, owner_key=OWNER)
    server.FEATURES = orig
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{srv.servers[0].sockets[0].getsockname()[1]}"
    settings_mod._which = old_which
    srv.should_exit = True
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture
def page(base_url):
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        pg = b.new_context(viewport={"width": 390, "height": 844}).new_page()
        pg.add_init_script(f"localStorage.setItem('cc-owner-key', '{OWNER}')")
        pg.goto(f"{base_url}/?view=settings")
        yield pg
        b.close()


def test_settings_page_fits_a_phone_and_shows_no_junk(page):
    page.wait_for_selector(".set .card")
    for h in ("Comms", "Devices", "Features", "Collab", "About"):
        assert page.locator(".set h2", has_text=h).count() == 1
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    text = page.locator(".set").inner_text()
    assert not re.search(r"\b(null|NaN|undefined)\b", text)
    assert "unset" in text                      # empty collab fields say so
    assert "Comms is not installed yet: INSTALL.md step 10." in text
    assert page.locator("button[data-test]").count() == 0
    assert page.locator("a[href='https://github.com/example/app/blob/main/INSTALL.md']").count() == 1
    assert "(this device)" in text


def test_volume_slider_saves_after_a_pause(page):
    page.wait_for_selector("#s-vol")
    page.evaluate("(() => { const s = document.getElementById('s-vol'); s.value = 30; s.dispatchEvent(new Event('input', {bubbles: true})); })()")
    page.wait_for_function("document.getElementById('s-msg').textContent === 'Volume saved.'")
    assert page.locator("#s-volo").inner_text() == "30%"


def test_pictures_for_a_person_to_read(base_url):
    import base64
    import os
    import pathlib
    out = os.environ.get("LOOK_SHOTS")
    if not out:
        pytest.skip("set LOOK_SHOTS=<folder> to write settings-dark.png and settings-light.png")
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
            pg.goto(f"{base_url}/?view=settings")
            pg.wait_for_selector(".set .card")
            pg.add_style_tag(content="@font-face{font-family:PhoneRoboto;src:url(data:font/ttf;base64,"
                             + base64.b64encode(font).decode() + ")}"
                             "body{font-family:PhoneRoboto,sans-serif !important}")
            pg.evaluate("document.fonts.ready")
            pg.screenshot(path=os.path.join(out, f"settings-{scheme}.png"))
        b.close()
