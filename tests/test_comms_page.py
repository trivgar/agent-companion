"""The Comms tab on a fresh install: no coordinator, only the Piper clips
comms/install.sh sets up. The page must play those, not say "not set up"."""
import io
import json
import time
import wave

import pytest

pytest.importorskip("fastapi")
sync_api = pytest.importorskip("playwright.sync_api")

from agent_companion import server  # noqa: E402

TOKEN = "t" * 32
OWNER = "o" * 64


def _wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 1600)   # 0.1 s of silence
    return buf.getvalue()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import threading

    import uvicorn
    clips = tmp_path / "clips"
    clips.mkdir()
    monkeypatch.setenv("COMMS_CLIPS_DIR", str(clips))
    monkeypatch.setenv("COMMS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AGENT_COMPANION_TTS_DIR", str(tmp_path / "no-coordinator"))
    monkeypatch.setattr(server, "FEATURES", ["comms"])
    app = server.create_app(token=TOKEN, owner_key=OWNER)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    port = srv.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", clips
    srv.should_exit = True


def test_a_fresh_install_plays_piper_clips(setup):
    base, clips = setup
    cid = f"{time.time_ns()}-Juno"
    (clips / f"{cid}.wav").write_bytes(_wav())
    (clips / f"{cid}.json").write_text(json.dumps({"speaker": "Juno", "text": "Testing one two.", "at": int(time.time())}))
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        except Exception as exc:  # browser not installed
            pytest.skip(f"chromium unavailable: {exc}")
        pg = b.new_page(viewport={"width": 390, "height": 844})
        pg.add_init_script(f"localStorage.setItem('cc-owner-key', '{OWNER}')")
        pg.goto(f"{base}/?view=comms")
        # Before Play: who is waiting, and no "not set up".
        pg.wait_for_function("document.body.innerText.includes('Testing one two.')")
        text = pg.inner_text("body")
        assert "not set up" not in text and "503" not in text
        assert "Juno" in text
        assert pg.locator(".who .identity-sprite").count() == 1
        pg.click("#tts-toggle")
        for _ in range(100):
            if (clips / f"{cid}.played").exists():
                break
            time.sleep(0.1)
        assert (clips / f"{cid}.played").exists(), pg.inner_text("body")
        b.close()


def test_pictures_for_a_person_to_read(setup):
    import base64
    import os
    import pathlib
    out = os.environ.get("LOOK_SHOTS")
    if not out:
        pytest.skip("set LOOK_SHOTS=<folder> to write comms-dark.png and comms-light.png")
    pathlib.Path(out).mkdir(parents=True, exist_ok=True)
    font = (pathlib.Path(__file__).resolve().parent / "fixtures" / "Roboto-Regular.ttf").read_bytes()
    base, clips = setup
    cid = f"{time.time_ns()}-Juno"
    (clips / f"{cid}.wav").write_bytes(_wav())
    (clips / f"{cid}.json").write_text(json.dumps({"speaker": "Juno", "text": "Testing one two.", "at": int(time.time())}))
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        for scheme in ("dark", "light"):
            pg = b.new_context(viewport={"width": 390, "height": 844}, color_scheme=scheme).new_page()
            pg.add_init_script(f"localStorage.setItem('cc-owner-key', '{OWNER}')")
            pg.goto(f"{base}/?view=comms")
            pg.wait_for_function("document.body.innerText.includes('Testing one two.')")
            pg.add_style_tag(content="@font-face{font-family:PhoneRoboto;src:url(data:font/ttf;base64,"
                             + base64.b64encode(font).decode() + ")}"
                             "body{font-family:PhoneRoboto,sans-serif !important}")
            pg.evaluate("document.fonts.ready")
            pg.screenshot(path=os.path.join(out, f"comms-{scheme}.png"))
        b.close()
