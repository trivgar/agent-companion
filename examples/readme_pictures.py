"""Take the twelve phone-width dashboard pictures in docs/images/.

    python3 examples/readme_pictures.py docs/images

Starts a local dashboard server against a throwaway directory, routes the tab
APIs with made-up data from a week of work (and the built-in demo sources for
Money and Fitness), and saves each picture at 390 px wide, device scale factor
2, light theme, in Roboto.
"""
from __future__ import annotations

import base64
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.pop("DISPLAY", None)
os.environ.pop("WAYLAND_DISPLAY", None)

import pytest  # noqa: E402,F401  (keeps server.create_app from starting background watchers)
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from agent_companion import pages, server  # noqa: E402

TOKEN = "t" * 32
OWNER = "o" * 64
FONT = (ROOT / "tests" / "fixtures" / "Roboto-Regular.ttf").read_bytes()
FONT_B64 = base64.b64encode(FONT).decode()
FONT_CSS = (
    f"@font-face{{font-family:PhoneRoboto;src:url(data:font/ttf;base64,{FONT_B64})}}"
    "body,button,input,select,textarea{font-family:PhoneRoboto,sans-serif !important}"
)

NOW = int(time.time())

ROWS = [
    {
        "pid": 101,
        "name": "web-checkout",
        "project": "web",
        "title": "Checkout form validation",
        "status": "idle",
        "waiting": True,
        "model": "claude-sonnet-5-5",
        "cost_usd": 0.94,
        "context_pct": 6,
        "last_reply": "Migration script is ready. I need your OK to run it against the staging copy.",
        "last_activity": 120,
        # The server puts a session's pending ask on its row; the card draws it.
        "ask": {"id": "ask-1", "tool": "Bash", "what": "npm run migrate -- --env staging"},
        "cwd": "/home/you/code/web",
        "branch": "fix/postcode",
        "child": False,
        "session_id": "s-web",
    },
    {
        "pid": 102,
        "name": "api-1",
        "project": "api",
        "title": "Rate limiter for the public API",
        "status": "busy",
        "waiting": False,
        "model": "claude-opus-5-5",
        "cost_usd": 5.26,
        "context_pct": 18,
        "last_reply": "Token bucket in place; 14 new tests passing. Wiring it into the router now.",
        "last_activity": 180,
        "cwd": "/home/you/code/api",
        "branch": "feat/rate-limit",
        "child": False,
        "session_id": "s-api",
    },
    {
        "pid": 103,
        "name": "infra",
        "project": "infra",
        "title": "Migrate CI to the new runners",
        "status": "busy",
        "waiting": False,
        "model": "claude-opus-5-5",
        "cost_usd": 6.50,
        "context_pct": 14,
        "last_reply": "Two jobs green on arm64; the e2e job needs a bigger runner.",
        "last_activity": 180,
        "cwd": "/home/you/code/infra",
        "branch": "ci/arm64",
        "child": False,
        "session_id": "s-infra",
    },
    {
        "pid": 104,
        "name": "docs",
        "project": "docs",
        "title": "Changelog for 2.4",
        "status": "idle",
        "waiting": False,
        "model": "claude-haiku-5-5",
        "cost_usd": 0.02,
        "context_pct": 3,
        "last_reply": "Draft is in CHANGELOG.md, 23 entries, grouped by area.",
        "last_activity": 2820,
        "cwd": "/home/you/code/docs",
        "branch": "docs/2.4",
        "child": False,
        "session_id": "s-docs",
    },
]

USAGE = {
    "five_hour": {"cost_usd": 12.72, "total_tokens": 378_000},
    "seven_day": {"cost_usd": 12.72, "total_tokens": 378_000},
    "limits": {"available": False, "reason": "no credentials"},
}

HEALTH = {
    "cpu": {"percent": 8.0},
    "disks": [{"path": "/", "percent": 28.0}],
}

AGENTS = [
    {
        "name": r["name"],
        "project": r["project"],
        "status": r["status"],
        "waiting": r["waiting"],
        "pid": r["pid"],
        "session_id": r["session_id"],
    }
    for r in ROWS
]

CHAT_API = {
    "messages": [
        {
            "role": "me",
            "at": NOW - 420,
            "text": "Add a per-key rate limiter and cover it with tests.",
        },
        {
            "role": "work",
            "tools": ["Edit", "Bash"],
            "last": "pytest tests/test_rate_limit.py -q",
        },
        {
            "role": "agent",
            "at": NOW - 240,
            "from": "api-1",
            "text": "Token bucket in place; 14 new tests passing. Wiring it into the router now.",
        },
        {
            "role": "me",
            "at": NOW - 190,
            "text": "Use a 60 second window for the public routes.",
        },
        {
            "role": "agent",
            "at": NOW - 180,
            "from": "api-1",
            "text": "Set to 60s with a burst of 120 requests. Running the router suite next.",
        },
    ],
    "asks": [],
    "dialog": None,
    "models": [],
    "efforts": [],
}

CHAT_DIALOG = {
    "messages": [
        {
            "role": "me",
            "at": NOW - 180,
            "text": "Run the migration on the staging copy",
        },
        {
            "role": "agent",
            "at": NOW - 120,
            "from": "web-checkout",
            "text": "Migration script is ready. Running it now against the staging copy.",
        },
    ],
    "asks": [],
    "dialog": {
        "key": "dlg-staging",
        "question": "Do you want to run this migration against staging?\n\n  npm run migrate -- --env staging",
        "multi": False,
        "cut": False,
        "options": [
            {"n": 1, "label": "Yes", "ok": True},
            {"n": 2, "label": "Yes, and don't ask again this session", "ok": True},
            {"n": 3, "label": "No, and tell Claude what to do differently", "ok": True},
        ],
    },
    "models": [],
    "efforts": [],
}

ASKS = {
    "asks": [
        {
            "id": "ask-1",
            "speaker": "web-checkout",
            "project": "web",
            "session_id": "s-web",
            "tool": "Bash",
            "what": "npm run migrate -- --env staging",
            "shortened": False,
        }
    ]
}

MAIL = {
    "counts": {"pending": 2},
    "drafts": [
        {
            "id": "d1",
            "subject": "Invoice 1042 is overdue",
            "from": "Billing <billing@example.com>",
            "why": "Asks when payment will arrive.",
            "reply": "Thanks for the nudge. Payment goes out on Monday; I'll confirm once it clears.",
            "state": "pending",
        },
        {
            "id": "d2",
            "subject": "Moving the Thursday sync",
            "from": "Sam <sam@example.com>",
            "why": "Sam asked whether Friday works instead.",
            "reply": "Friday at 2pm works for me. I'll move the invite and send the agenda the day before.\n\nThanks,\nAlex",
            "state": "pending",
        },
    ],
}

PHONE = {
    "live": [
        {
            "id": "job-1",
            "agent": "phone-agent",
            "device": "emulator",
            "state": "waiting",
            "task": "Open Settings and turn on dark mode",
            "prompt": {"what": "Allow a change to Display settings"},
        }
    ],
    "pending": [
        {
            "device_id": "emu-5f6a7b",
            "model": "Pixel 8",
            "registered": NOW - 300,
        }
    ],
    "history": [
        {
            "id": "h1",
            "task": "Read the battery level",
            "agent": "phone-agent",
            "device": "emulator",
            "outcome": "done",
            "ended": NOW - 1800,
        },
        {
            "id": "h2",
            "task": "Install the test build and open it",
            "agent": "phone-agent",
            "device": "emulator",
            "outcome": "done",
            "ended": NOW - 7200,
        },
        {
            "id": "h3",
            "task": "Sign in to the demo account",
            "agent": "phone-agent",
            "device": "emulator",
            "outcome": "stopped by the holder",
            "ended": NOW - 10800,
        },
    ],
}

COMMS_NEXT = {
    "clip": {
        "id": "1-Juno",
        "speaker": "Juno",
        "text": "Testing one two.",
        "at": NOW - 4,
        "age": 4,
        "volume": 1.0,
    },
    "held": False,
}

VOICELAB = {
    "now": {"kind": "pipeline", "text": "1 voice in ~/.local/share/agent-companion/voices"},
    "needs": {"ok": True, "items": []},
    "running": {"ok": True, "builds": []},
    "voices": {
        "ok": False,
        "voices": [],
        "why": "Tiers and test lines need a speech coordinator; this install gives a speaker its voice in comms/cast.tsv.",
    },
    "other": {"ok": True, "builds": []},
    "pipeline": {
        "root": "/home/you/.local/share/agent-companion/voices",
        "voices": [
            {"name": "demo", "stage": "served", "bytes": 1024, "audition": []},
        ],
    },
}

PAGE_REPORT_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<style>
@font-face {{
  font-family: PhoneRoboto;
  src: url(data:font/ttf;base64,{FONT_B64});
}}
body {{
  margin: 0;
  padding: 14px;
  font: 14px/1.45 PhoneRoboto, system-ui, sans-serif;
  color: #191b20;
  background: #fffefb;
}}
h1 {{
  margin: 0 0 4px;
  font-size: 1.05rem;
  font-weight: 700;
}}
p.sub {{
  margin: 0 0 14px;
  color: #5f6470;
  font-size: 0.84rem;
}}
.row {{
  margin: 0 0 12px;
}}
.name {{
  font-family: ui-monospace, monospace;
  font-size: 0.82rem;
  margin-bottom: 4px;
}}
.bar {{
  height: 10px;
  border-radius: 5px;
  background: #2f7d4f;
}}
.time {{
  margin-top: 2px;
  color: #5f6470;
  font-size: 0.78rem;
  font-variant-numeric: tabular-nums;
}}
</style>
</head>
<body>
  <h1>Slowest tests this week</h1>
  <p class="sub">Full runs of the merge queue, median duration.</p>
  <div class="row"><div class="name">test_checkout_e2e</div><div class="bar" style="width:100%"></div><div class="time">41.2s</div></div>
  <div class="row"><div class="name">test_rate_limit_burst</div><div class="bar" style="width:45%"></div><div class="time">18.7s</div></div>
  <div class="row"><div class="name">test_search_index</div><div class="bar" style="width:30%"></div><div class="time">12.3s</div></div>
  <div class="row"><div class="name">test_export_csv</div><div class="bar" style="width:24%"></div><div class="time">9.8s</div></div>
  <div class="row"><div class="name">test_login_flow</div><div class="bar" style="width:15%"></div><div class="time">6.1s</div></div>
</body>
</html>
"""


def _attach_routes(page) -> None:
    page.route("**/api/sessions*", lambda r: r.fulfill(json=ROWS))
    page.route("**/api/usage*", lambda r: r.fulfill(json=USAGE))
    page.route("**/api/restore*", lambda r: r.fulfill(json={"ok": True, "sessions": []}))
    page.route("**/api/health*", lambda r: r.fulfill(json=HEALTH))
    page.route("**/api/asks*", lambda r: r.fulfill(json=ASKS))
    page.route("**/api/mail*", lambda r: r.fulfill(json=MAIL))
    page.route("**/api/phone/jobs*", lambda r: r.fulfill(json=PHONE))
    page.route("**/api/connections*", lambda r: r.fulfill(json=[]))
    page.route("**/api/tts/outbox*", lambda r: r.fulfill(json={"set_up": False, "clips": []}))
    page.route("**/api/comms/next*", lambda r: r.fulfill(json=COMMS_NEXT))
    page.route("**/api/voicelab*", lambda r: r.fulfill(json=VOICELAB))
    page.route("**/api/push/key*", lambda r: r.fulfill(json={"key": "BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U", "devices": 0}))
    page.route("**/api/cave", lambda r: r.fulfill(json={"agents": AGENTS, "messages": []}))
    page.route("**/api/cave/seen/*", lambda r: r.fulfill(json={"ok": True}))

    def cave_chat(route):
        path = urlparse(route.request.url).path
        if path.endswith("/web-checkout"):
            route.fulfill(json=CHAT_DIALOG)
        else:
            route.fulfill(json=CHAT_API)

    page.route("**/api/cave/chat/*", cave_chat)


def _prepare(page) -> None:
    page.add_style_tag(content=FONT_CSS)
    page.wait_for_function("document.getElementById('strip').textContent.includes('$12.72')")
    page.wait_for_selector("#chat-log", state="attached")
    page.evaluate("window.scrollTo(0, 0)")
    page.evaluate("document.querySelector('#nav')?.scrollTo(0, 0)")
    page.evaluate("document.fonts.ready")
    page.evaluate(
        """() => Promise.all(
            document.getAnimations({subtree: true})
                .filter(a => a.effect && a.effect.getComputedTiming().iterations !== Infinity)
                .map(a => a.finished.catch(() => {}))
        )"""
    )


def _shoot(page, path: Path) -> None:
    _prepare(page)
    # One phone screen, never the full page: the README sets these side by side,
    # and pictures of different heights make a ragged row.
    page.screenshot(path=str(path), full_page=False)


def take_pictures(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cc-readme-pics-") as tmp_dir:
        tmp = Path(tmp_dir)
        os.environ["AGENT_COMPANION_CLAUDE_HOME"] = str(tmp / ".claude")
        os.environ["AGENT_COMPANION_DATA_DIR"] = str(tmp / "data")
        os.environ["AGENT_COMPANION_CONFIG"] = str(tmp / "config.toml")
        os.environ["AGENT_COMPANION_OWNER_KEYS"] = str(tmp / "owner-keys.json")
        os.environ["AGENT_COMPANION_PUSH_DIR"] = str(tmp / "push")
        os.environ["AGENT_COMPANION_PAGES_DIR"] = str(tmp / "pages")
        os.environ["AGENT_COMPANION_PHONE_DIR"] = str(tmp / "phone")
        os.environ["AGENT_COMPANION_VOICES_DIR"] = str(tmp / "voices")
        os.environ["AGENT_COMPANION_TTS_DIR"] = str(tmp / "no-coordinator")
        os.environ["AGENT_COMPANION_PHONE_DRIVER"] = "true"
        os.environ["AGENT_COMPANION_MONEY_SOURCE"] = "demo"
        os.environ["AGENT_COMPANION_FITNESS_SOURCE"] = "demo"
        os.environ["AGENT_COMPANION_UNITS"] = "imperial"
        os.environ["AGENT_COMPANION_FITNESS_WORK_DAYS"] = "mon,tue,wed,thu,fri"

        record, _ = pages.Store(tmp / "pages").publish(
            html=PAGE_REPORT_HTML,
            title="Slowest tests this week",
            folder=tmp,
            source=tmp / "slowest-tests.html",
            speaker="infra",
            project="infra",
            household=False,
        )

        app = server.create_app(token=TOKEN, owner_key=OWNER)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        srv = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        thread = threading.Thread(target=srv.run, args=([listener],), daemon=True)
        thread.start()
        for _ in range(100):
            if srv.started:
                break
            time.sleep(0.05)

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()

                def new_page():
                    ctx = browser.new_context(
                        viewport={"width": 390, "height": 844},
                        device_scale_factor=2,
                        color_scheme="light",
                        service_workers="block",
                    )
                    pg = ctx.new_page()
                    pg.add_init_script(f"localStorage.setItem('cc-owner-key', '{OWNER}')")
                    _attach_routes(pg)
                    return ctx, pg

                # 1. tab-sessions.png: one needing you, two working, one idle; one card's details open.
                ctx, pg = new_page()
                pg.goto(f"{base_url}/")
                pg.wait_for_selector(".scard")
                pg.click('.scard:has(h3:text-is("api-1")) .scard-toggle')
                pg.wait_for_selector('.scard:has(h3:text-is("api-1")) .scard-more')
                _shoot(pg, out / "tab-sessions.png")
                ctx.close()

                # 2. tab-chat.png: the chat sheet open over the sessions list.
                ctx, pg = new_page()
                pg.goto(f"{base_url}/")
                pg.wait_for_selector(".scard")
                pg.click('.scard:has(h3:text-is("api-1")) .scard-main')
                pg.wait_for_selector('#views > section.sheet[data-id="chat"] #chat-log .msg')
                _shoot(pg, out / "tab-chat.png")
                ctx.close()

                # 3. tab-dialog.png: the chat with a terminal dialog waiting and its buttons.
                ctx, pg = new_page()
                pg.add_init_script("localStorage.setItem('cc-chat-to', 'web-checkout')")
                pg.goto(f"{base_url}/?view=chat")
                pg.wait_for_selector("#chat-to option[value='web-checkout']", state="attached")
                pg.check("#chat-win")
                pg.wait_for_selector(".cv-term [data-cvdialog]")
                _shoot(pg, out / "tab-dialog.png")
                ctx.close()

                # 4. tab-pages.png: an agent's report of the slowest tests, in a sandbox.
                ctx, pg = new_page()
                pg.goto(f"{base_url}/?view=pages&page={record['id']}")
                pg.wait_for_selector(".pg-frame")
                frame = pg.query_selector(".pg-frame").content_frame()
                frame.wait_for_selector(".bar")
                frame.evaluate("document.fonts.ready")
                _shoot(pg, out / "tab-pages.png")
                ctx.close()

                # 5. tab-phone.png: phone driver waiting on a prompt, pending device, recent jobs.
                ctx, pg = new_page()
                pg.goto(f"{base_url}/?view=phone")
                pg.wait_for_selector(".phone-live")
                pg.click(".phone-history summary")
                pg.wait_for_selector(".phone-history[open] li")
                _shoot(pg, out / "tab-phone.png")
                ctx.close()

                # 6. tab-money.png: net worth and accounts, demo data.
                ctx, pg = new_page()
                pg.goto(f"{base_url}/?view=money")
                pg.wait_for_selector(".mnw svg")
                _shoot(pg, out / "tab-money.png")
                ctx.close()

                # 7. tab-fitness.png: sleep over the last 90 nights, demo data.
                ctx, pg = new_page()
                pg.goto(f"{base_url}/?view=fitness")
                pg.wait_for_selector(".fit svg.fchart")
                _shoot(pg, out / "tab-fitness.png")
                ctx.close()

                # 8. tab-mail.png: two drafted replies with Approve, Save edit and Skip.
                ctx, pg = new_page()
                pg.goto(f"{base_url}/?view=mail")
                pg.wait_for_selector("article.mail textarea")
                _shoot(pg, out / "tab-mail.png")
                ctx.close()

                # 9. tab-asks.png: pending permission request in Asks.
                ctx, pg = new_page()
                pg.goto(f"{base_url}/?view=asks")
                pg.wait_for_selector(".card[data-ask]")
                _shoot(pg, out / "tab-asks.png")
                ctx.close()

                # 10. tab-comms.png: Play button and Juno clip ready to play.
                ctx, pg = new_page()
                pg.goto(f"{base_url}/?view=comms")
                pg.wait_for_selector("#views > section[data-id='comms'] .card .who")
                _shoot(pg, out / "tab-comms.png")
                ctx.close()

                # 11. tab-push.png: Notifications card.
                ctx, pg = new_page()
                pg.goto(f"{base_url}/?view=push")
                pg.wait_for_selector("#push-on")
                _shoot(pg, out / "tab-push.png")
                ctx.close()

                # 12. tab-voicelab.png: Voice lab pipeline view.
                ctx, pg = new_page()
                pg.goto(f"{base_url}/?view=voicelab")
                pg.wait_for_selector("#views > section[data-id='voicelab'] .card")
                _shoot(pg, out / "tab-voicelab.png")
                ctx.close()

                browser.close()
        finally:
            srv.should_exit = True
            thread.join(timeout=5)
            listener.close()


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "images"
    take_pictures(target)
