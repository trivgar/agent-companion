"""Is the chat window's model picker still Anthropic's lineup?

When a new model ships, a picker set by hand keeps offering the old one, and
a price table without the new id bills it by its longest prefix (a new
point release priced as its family's first model).

Once a day `main()` reads the models overview page (its Markdown form, whose
"Claude API ID" row is the current lineup), compares it with the
`chat_models` setting and the price table, and tells the owner once per
difference: a push by default, or the host's own `file_fn` (`run()` takes
one). It never edits the setting or the prices itself: a scraped page does
not get to change what sessions run on or what the meters say.
Pure except fetch(), push() and main().
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

URL = "https://platform.claude.com/docs/en/about-claude/models/overview.md"
_ID = re.compile(r"`(claude-[a-z]+-[0-9][a-z0-9-]*)`")


def state_path() -> Path:
    return Path(os.environ.get("AGENT_COMPANION_MODELWATCH_STATE")
                or Path.home() / ".local/state/agent-companion/modelwatch.json")


def lineup(md: str) -> list[str]:
    """The ids in the overview table's "Claude API ID" row, in page order.
    ValueError when the row is gone: the page changed shape, which is itself
    worth a look, not a quiet pass."""
    for line in md.splitlines():
        if line.lstrip("| ").startswith("Claude API ID"):
            ids = _ID.findall(line)
            if ids:
                return ids
    raise ValueError("no 'Claude API ID' row on the models overview page")


def compare(current: list[str], offered: list[str], priced: set[str]) -> dict:
    """new: on the page, not in the picker; gone: in the picker, not on the
    page; unpriced: on the page with no exact row in the core's prices (a
    prefix match is how Fable 5.2 was billed as Fable 5)."""
    return {"new": [m for m in current if m not in offered],
            "gone": [m for m in offered if m not in current],
            "unpriced": [m for m in current if m not in priced]}


def entry(current: list[str], offered: list[str], diff: dict) -> str:
    parts = [f"{k}: {', '.join(v)}" for k, v in diff.items() if v]
    return (f"Anthropic's lineup on {URL} is {', '.join(current)}; chat_models is "
            f"{', '.join(offered) or 'empty'}. " + "; ".join(parts) + ". To fix: add exact rows for new "
            "models to agent_companion/pricing.py (RATES, CACHE_READ_MULTIPLIERS) from each model's page, "
            "then set chat_models in your config.toml and restart the server. Retired models come out "
            "of chat_models too.")


def fetch(url: str = URL, timeout: float = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "agent-companion-modelwatch"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(2_000_000).decode("utf-8", "replace")


def push(text: str) -> None:
    """The default: one push to the owner's devices."""
    from .webpush import PushStore, push_dir
    PushStore(push_dir()).send("Model picker out of date", text[:900], tag="modelwatch")


def run(md: str, offered: list[str], priced: set[str], state: dict, file_fn=push,
        entry_fn=entry) -> tuple[str, dict]:
    """(what happened, the new state). Files once per distinct finding."""
    try:
        current = lineup(md)
    except ValueError as exc:
        key = "shape"
        if state.get("filed") != key:
            file_fn(f"Model picker watch is blind: {exc} ({URL}). Fix lineup() in "
                    "agent_companion/modelwatch.py against the page as it is now (or report it upstream).")
            return "filed: page shape", {**state, "filed": key}
        return "page shape still unreadable (filed already)", state
    diff = compare(current, offered, priced)
    if not any(diff.values()):
        return "picker matches " + ", ".join(current), {**state, "filed": None, "lineup": current}
    key = json.dumps(diff, sort_keys=True)
    if state.get("filed") == key:
        return "same difference as already filed", state
    file_fn(entry_fn(current, offered, diff))
    return "filed: " + key, {**state, "filed": key, "lineup": current}


def main(file_fn=push, entry_fn=entry) -> int:
    from . import pricing
    from .server.chat import picker_values
    path = state_path()
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {}
    try:
        md = fetch()
    except Exception as exc:          # offline, a timeout: try again tomorrow
        print(f"modelwatch: could not read {URL}: {exc}")
        return 0
    said, state = run(md, picker_values("chat_models"), set(pricing.RATES), state, file_fn, entry_fn)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)
    print("modelwatch:", said)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
