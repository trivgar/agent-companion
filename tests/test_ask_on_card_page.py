"""The ask on the session card and helpers nested under their agent at 390px."""
import base64
import os
import pathlib
import threading
import time

import pytest

pytest.importorskip("fastapi")
sync_api = pytest.importorskip("playwright.sync_api")

from agent_companion import server  # noqa: E402

TOKEN = "t" * 32
OWNER = "o" * 64


def row(pid, name, **extra):
    base = {"pid": pid, "name": name, "project": name, "title": "", "status": "idle", "waiting": False,
            "model": "claude-opus-5-5", "cost_usd": 1.0, "context_pct": 42, "last_reply": f"{name} is ready.",
            "last_activity": 30, "cwd": f"/w/{name}", "branch": "", "child": False, "job": "",
            "parent_pid": None, "root_pid": None, "label": "", "session_id": f"s{pid}"}
    base.update(extra)
    return base


ROWS = [
    row(1, "web", waiting=True, last_reply="Ready to run migration.",
        ask={"id": "a1", "tool": "Bash", "what": "pytest -q tests/test_api.py"}),
    row(2, "api", status="busy", last_activity=4, last_reply="Running suite."),
    row(3, "docs", status="idle", last_activity=120, last_reply="Changelog updated."),
]
USAGE = {"five_hour": {"cost_usd": 1.5, "total_tokens": 1000}, "seven_day": {"cost_usd": 20, "total_tokens": 9000},
         "limits": {"available": False, "reason": "no credentials"}}


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    import uvicorn
    d = tmp_path_factory.mktemp("home")
    os.environ["AGENT_COMPANION_CLAUDE_HOME"] = str(d / ".claude")
    os.environ["AGENT_COMPANION_DATA_DIR"] = str(d / "data")
    orig = server.FEATURES
    server.FEATURES = ["sessions", "asks", "chat"]
    app = server.create_app(token=TOKEN, owner_key=OWNER)
    server.FEATURES = orig
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    port = srv.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    os.environ.pop("AGENT_COMPANION_CLAUDE_HOME", None)
    os.environ.pop("AGENT_COMPANION_DATA_DIR", None)


@pytest.fixture
def open_page(base_url):
    """open_page(scheme, rows=..., asks=...) gives a loaded page at 390px."""
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # browser not installed
            pytest.skip(f"chromium unavailable: {exc}")

        def make(scheme="dark", rows=None, asks=None, decide=None):
            state = {"rows": list(ROWS if rows is None else rows), "asks": list(asks or []), "decided": []}
            ctx = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme=scheme,
                                      service_workers="block")
            pg = ctx.new_page()
            pg.state = state
            pg.route("**/api/sessions*", lambda r: r.fulfill(json=state["rows"]))
            pg.route("**/api/usage*", lambda r: r.fulfill(json=USAGE))
            pg.route("**/api/asks", lambda r: r.fulfill(json={"asks": state["asks"]}))
            pg.route("**/api/cave", lambda r: r.fulfill(json={
                "agents": [{"name": x["name"], "project": x["project"], "status": x["status"],
                            "waiting": x["waiting"], "pid": x["pid"], "session_id": x["session_id"]}
                           for x in state["rows"]],
                "messages": []}))
            pg.route("**/api/cave/chat/*", lambda r: r.fulfill(json={
                "messages": [], "asks": [], "dialog": None, "models": [], "efforts": []}))
            pg.route("**/api/cave/seen/*", lambda r: r.fulfill(json={"ok": True}))

            def handle_decide(route):
                url = route.request.url
                ask_id = url.rsplit("/api/asks/", 1)[1].split("/decide", 1)[0]
                body = route.request.post_data_json
                state["decided"].append((ask_id, body))
                if decide is not None:
                    decide(route, ask_id, body, state)
                else:
                    state["rows"] = [dict(x, waiting=False, ask=None) if (x.get("ask") or {}).get("id") == ask_id else x
                                     for x in state["rows"]]
                    route.fulfill(json={"ok": True, "status": body.get("decision", "")})

            pg.route("**/api/asks/*/decide", handle_decide)
            pg.add_init_script(f"localStorage.setItem('cc-owner-key', '{OWNER}')")
            pg.goto(f"{base_url}/")
            pg.wait_for_selector(".scard")
            pg.wait_for_selector("#chat-log", state="attached")
            return pg

        yield make
        browser.close()


def roboto(page):
    """Draw the page in Roboto, the font a phone uses for system-ui."""
    font = (pathlib.Path(__file__).resolve().parent / "fixtures" / "Roboto-Regular.ttf").read_bytes()
    page.add_style_tag(content="@font-face{font-family:PhoneRoboto;src:url(data:font/ttf;base64,"
                       + base64.b64encode(font).decode() + ")}"
                       "body{font-family:PhoneRoboto,sans-serif !important}")
    page.evaluate("document.fonts.ready")


def box(page, selector):
    return page.eval_on_selector(selector, "e => { const r = e.getBoundingClientRect(); "
                                 "return {x: r.x, y: r.y, w: r.width, h: r.height, top: r.top, bottom: r.bottom} }")


def card(name):
    return f'.scard:has(h3:text-is("{name}"))'


def test_a_waiting_row_with_a_permission_ask_shows_it_on_the_closed_card(open_page):
    page = open_page()
    c = card("web")
    assert page.locator(f"{c} .scard-more").count() == 0
    assert page.locator(f"{c} .scard-ask").count() == 1
    assert page.inner_text(f"{c} .scard-ask b").strip() == "Bash"
    assert page.inner_text(f"{c} .scard-ask code").strip() == "pytest -q tests/test_api.py"
    assert page.locator(f'{c} .scard-ask button[data-ask-decide="allow"]').count() == 1
    assert page.locator(f'{c} .scard-ask button[data-ask-decide="deny"]').count() == 1
    assert page.locator(f"{c} .scard-ask button[data-ask-chat]").count() == 1


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_allow_and_deny_post_the_decision_and_refresh_the_card(open_page, decision):
    page = open_page()
    c = card("web")
    page.click(f'{c} .scard-ask button[data-ask-decide="{decision}"]')
    page.locator(f"{c} .scard-ask").wait_for(state="detached")
    assert page.state["decided"] == [("a1", {"decision": decision})]
    assert page.locator(f"{c} .scard-ask").count() == 0


def test_a_refused_decision_shows_not_sent_and_the_reason_on_the_card(open_page):
    def refuse(route, ask_id, body, state):
        route.fulfill(status=409, json={"detail": "already answered"})

    page = open_page(decide=refuse)
    c = card("web")
    page.click(f'{c} .scard-ask button[data-ask-decide="allow"]')
    page.wait_for_selector(f"{c} .scard-ask .err")
    assert "Not sent: already answered" in page.inner_text(f"{c} .scard-ask .err")


def test_a_helpers_ask_on_its_agents_card_names_the_helper(open_page):
    rows = [
        row(1, "api", waiting=True,
            ask={"id": "a2", "tool": "Bash", "what": "git status", "from": "reviewing task"}),
        row(2, "web", waiting=True,
            ask={"id": "a3", "tool": "Bash", "what": "ls", "from": ""}),
    ]
    page = open_page(rows=rows)
    assert "Its helper, reviewing task:" in page.inner_text(f"{card('api')} .scard-ask")
    assert "Its helper:" in page.inner_text(f"{card('web')} .scard-ask")


def test_a_shortened_request_disables_allow_and_see_it_all_opens_asks(open_page):
    rows = [row(1, "web", waiting=True,
                ask={"id": "a4", "tool": "Bash", "what": "echo long", "shortened": True})]
    page = open_page(rows=rows)
    c = card("web")
    allow = f'{c} .scard-ask button[data-ask-decide="allow"]'
    assert page.is_disabled(allow)
    page.click(f"{c} .scard-ask button[data-ask-all]")
    assert page.eval_on_selector('#views > section[data-id="asks"]', "e => !e.hidden")
    assert page.get_attribute('#nav button[data-id="asks"]', "aria-current") == "true"


def test_a_question_shows_its_text_and_answer_opens_asks_without_options(open_page):
    rows = [row(1, "web", waiting=True,
                ask={"id": "q1", "tool": "AskUserQuestion", "what": "Which target branch?",
                     "questions": [{"question": "Which target branch?",
                                    "options": [{"label": "main"}, {"label": "staging"}]}]})]
    page = open_page(rows=rows)
    c = card("web")
    text = page.inner_text(f"{c} .scard-ask")
    assert "Which target branch?" in text
    assert "main" not in text and "staging" not in text
    assert page.locator(f"{c} .scard-ask .opt").count() == 0
    page.click(f"{c} .scard-ask button[data-ask-answer]")
    assert page.eval_on_selector('#views > section[data-id="asks"]', "e => !e.hidden")


def test_a_tap_on_the_asks_buttons_does_not_open_chat(open_page):
    rows = [
        row(1, "web", waiting=True, ask={"id": "a1", "tool": "Bash", "what": "pytest -q"}),
        row(2, "api", waiting=True, ask={"id": "a2", "tool": "Bash", "what": "echo cut", "shortened": True}),
    ]
    page = open_page(rows=rows)
    page.click(f'{card("web")} .scard-ask button[data-ask-decide="deny"]')
    page.wait_for_function("document.querySelectorAll('.scard-ask').length === 1")
    assert page.locator('#views > section.sheet[data-id="chat"]').count() == 0
    page.click(f'{card("api")} .scard-ask button[data-ask-all]')
    assert page.locator('#views > section.sheet[data-id="chat"]').count() == 0


def test_the_command_is_escaped(open_page):
    rows = [row(1, "web", waiting=True,
                ask={"id": "a1", "tool": "Bash", "what": "<img src=x onerror=window.__hit=1>"})]
    page = open_page(rows=rows)
    assert page.locator(".scard img").count() == 0
    assert page.evaluate("window.__hit") is None
    assert "<img src=x onerror=window.__hit=1>" in page.inner_text(f"{card('web')} .scard-ask code")


def test_a_three_hundred_character_command_does_not_scroll_sideways_in_roboto(open_page):
    rows = [row(1, "web", waiting=True,
                ask={"id": "a1", "tool": "Bash", "what": "c" * 300})]
    page = open_page(rows=rows)
    roboto(page)
    assert page.locator(f"{card('web')} .scard-ask code").count() == 1
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_every_button_on_the_card_is_at_least_forty_four_pixels_tall(open_page):
    rows = [
        row(1, "web", waiting=True,
            ask={"id": "a1", "tool": "Bash", "what": "pytest -q", "shortened": True}),
        row(10, "web-h1", status="busy", child=True, parent_pid=1, root_pid=1, label="checking schema"),
    ]
    page = open_page(rows=rows)
    roboto(page)
    page.click(f"{card('web')} .scard-toggle")
    heights = page.eval_on_selector_all(f"{card('web')} button",
                                        "els => els.map(e => e.getBoundingClientRect().height)")
    assert len(heights) >= 7
    assert all(h >= 44 for h in heights), heights


def test_helpers_sit_under_their_agent_toggle_and_survive_a_refresh(open_page):
    rows = [
        row(1, "api", status="busy", last_activity=4),
        row(2, "api-h1", status="busy", child=True, parent_pid=1, root_pid=1,
            label="reviewing routes", cost_usd=0.45),
        row(3, "api-h2", status="idle", child=True, parent_pid=1, root_pid=1,
            label="updating schema", cost_usd=0.20),
    ]
    page = open_page(rows=rows)
    roboto(page)
    assert page.eval_on_selector_all(".scard h3", "els => els.map(e => e.textContent)") == ["api"]
    chip = f"{card('api')} button[data-helpers-toggle]"
    assert page.inner_text(chip).strip() == "2 helpers"
    assert page.get_attribute(chip, "aria-expanded") == "false"
    assert box(page, chip)["h"] >= 44
    assert page.locator(f"{card('api')} .scard-helper").count() == 0

    page.click(chip)
    assert page.get_attribute(chip, "aria-expanded") == "true"
    assert page.locator(f"{card('api')} .scard-helper").count() == 2
    assert page.locator('#views > section.sheet[data-id="chat"]').count() == 0
    text = page.inner_text(f"{card('api')} .scard-helper-list")
    assert "reviewing routes" in text and "working" in text and "$0.45" in text
    assert "updating schema" in text and "idle" in text and "$0.20" in text
    sprite = box(page, f"{card('api')} .scard-helper .spr .identity-sprite")
    assert (sprite["w"], sprite["h"]) == (24, 24)

    page.evaluate("CC.show('sessions')")
    page.wait_for_timeout(300)
    assert page.get_attribute(chip, "aria-expanded") == "true"
    assert page.locator(f"{card('api')} .scard-helper").count() == 2

    page.click(chip)
    assert page.get_attribute(chip, "aria-expanded") == "false"
    assert page.locator(f"{card('api')} .scard-helper").count() == 0


def test_an_orphan_helper_and_a_job_keep_their_own_cards(open_page):
    rows = [
        row(1, "api", status="busy"),
        row(2, "orphan-helper", status="busy", child=True, parent_pid=99, root_pid=99, label="checking logs"),
        row(3, "nightly-job", status="busy", child=True, job="digest", parent_pid=1, root_pid=1),
    ]
    page = open_page(rows=rows)
    assert page.eval_on_selector_all(".scard h3", "els => els.map(e => e.textContent)") == [
        "api", "orphan-helper", "nightly-job"
    ]
    assert page.locator(f"{card('api')} button[data-helpers-toggle]").count() == 0


def test_the_working_groups_count_counts_agents_not_helpers(open_page):
    rows = [
        row(1, "api", status="busy"),
        row(2, "infra", status="busy"),
        row(3, "api-h1", status="busy", child=True, parent_pid=1, root_pid=1, label="running unit tests"),
        row(4, "api-h2", status="busy", child=True, parent_pid=1, root_pid=1, label="linting"),
    ]
    page = open_page(rows=rows)
    groups = page.eval_on_selector_all(
        "h2.grp",
        "els => els.map(e => e.textContent.toLowerCase().split(/\\s+/).join(' ').trim())",
    )
    assert groups == ["working 2"]


def test_pictures_for_a_person_to_read(open_page):
    out = os.environ.get("ASK_SHOTS")
    if not out:
        pytest.skip("set ASK_SHOTS=<folder> to write ask-dark.png and ask-light.png")
    pathlib.Path(out).mkdir(parents=True, exist_ok=True)
    rows = [
        row(1, "web", waiting=True, last_reply="Migration script is ready to apply.",
            ask={"id": "a1", "tool": "Bash", "what": "pytest -q tests/test_migrations.py",
                 "from": "verifying schema"}),
        row(2, "api", status="busy", last_activity=4, last_reply="Running integration suite."),
        row(3, "api-h1", status="busy", child=True, parent_pid=2, root_pid=2,
            label="reviewing endpoints", cost_usd=0.38),
        row(4, "api-h2", status="idle", child=True, parent_pid=2, root_pid=2,
            label="checking types", cost_usd=0.14),
        row(5, "docs", status="idle", last_activity=2820, last_reply="Draft is in the changelog."),
    ]
    for scheme in ("dark", "light"):
        page = open_page(scheme, rows=rows)
        roboto(page)
        page.click(f"{card('api')} button[data-helpers-toggle]")
        page.wait_for_selector(f"{card('api')} .scard-helper")
        page.screenshot(path=os.path.join(out, f"ask-{scheme}.png"), full_page=True)
