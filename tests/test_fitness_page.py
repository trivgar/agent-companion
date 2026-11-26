import os
import re

import pytest

pytest.importorskip("fastapi")
sync_api = pytest.importorskip("playwright.sync_api")

from agent_companion import server  # noqa: E402

TOKEN = "t" * 32
OWNER = "o" * 64


@pytest.fixture(scope="module")
def base_url():
    import threading
    import time
    import uvicorn
    os.environ["AGENT_COMPANION_FITNESS_SOURCE"] = "demo"
    os.environ["AGENT_COMPANION_UNITS"] = "imperial"
    os.environ["AGENT_COMPANION_FITNESS_WORK_DAYS"] = "mon,tue,wed,thu,fri"
    orig = server.FEATURES
    server.FEATURES = ["fitness"]
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
    for k in ("FITNESS_SOURCE", "UNITS", "FITNESS_WORK_DAYS"):
        os.environ.pop("AGENT_COMPANION_" + k, None)


def _open(p, base_url, route=None):
    try:
        b = p.chromium.launch()
    except Exception as exc:
        pytest.skip(f"chromium unavailable: {exc}")
    pg = b.new_context(viewport={"width": 390, "height": 844}).new_page()
    if route:
        pg.route("**/api/fitness*", lambda r: r.fulfill(json=route))
    pg.add_init_script(f"localStorage.setItem('cc-owner-key', '{OWNER}')")
    pg.goto(f"{base_url}/?view=fitness")
    pg.wait_for_selector(".fit")
    return b, pg


def test_demo_page_fits_390_and_never_says_null(base_url):
    with sync_api.sync_playwright() as p:
        b, pg = _open(p, base_url)
        pg.wait_for_selector(".fit h3")
        text = pg.inner_text(".fit")
        assert pg.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not re.search(r"\b(null|NaN|undefined)\b", text), text
        assert "DEMO" in text and "nights recorded" in text and "never counted as zero" in text
        heads = pg.eval_on_selector_all(".fit h3", "h => h.map(x => x.textContent)")
        assert heads == ["Last night", "Running", "Work nights and nights off", "Resting heart rate",
                         "Steps and distance", "Weight"]
        assert " mi" in text and " lb" in text and " km" not in text
        assert pg.eval_on_selector_all(".fit svg.fchart", "s => s.length") == 4
        b.close()


def test_no_source_says_so(base_url):
    with sync_api.sync_playwright() as p:
        b, pg = _open(p, base_url, route={"ok": None, "detail": "No fitness source set up", "docs": "docs/fitness-sources.md"})
        text = pg.inner_text(".fit")
        assert "No fitness source set up" in text and "docs/fitness-sources.md" in text
        assert pg.evaluate("document.documentElement.scrollWidth <= innerWidth")
        b.close()


def test_an_all_gaps_week_draws_gaps_not_zeros(base_url):
    from agent_companion.fitness import summary
    rows = [{"date": f"2026-03-{d:02d}", "sleep_min": None, "resting_hr": None, "steps": None,
             "distance_km": None, "weight_kg": None} for d in range(1, 15)]
    view = {"ok": True, "demo": False, "source": "Home Assistant", "as_of": None, **summary.view(rows)}
    with sync_api.sync_playwright() as p:
        b, pg = _open(p, base_url, route=view)
        text = pg.inner_text(".fit")
        assert not re.search(r"\b(null|NaN|undefined)\b", text), text
        assert "0 of 14 nights recorded" in text and "Not recorded" in text and "0m" not in text
        assert pg.evaluate("document.documentElement.scrollWidth <= innerWidth")
        b.close()


def test_pictures_for_a_person_to_read(base_url):
    import base64
    import pathlib
    out = os.environ.get("LOOK_SHOTS")
    if not out:
        pytest.skip("set LOOK_SHOTS=<folder> to write fitness-dark.png and fitness-light.png")
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
            pg.goto(f"{base_url}/?view=fitness")
            pg.wait_for_selector(".fit h3")
            pg.add_style_tag(content="@font-face{font-family:PhoneRoboto;src:url(data:font/ttf;base64,"
                             + base64.b64encode(font).decode() + ")}"
                             "body{font-family:PhoneRoboto,sans-serif !important}")
            pg.evaluate("document.fonts.ready")
            pg.screenshot(path=os.path.join(out, f"fitness-{scheme}.png"))
        b.close()
