"""Terminal dialogs that ordinary session hooks may not report.

Some terminal clients draw numbered choices directly in a pane without an
input box.  This module reads a choice list from the bottom of that pane and
allows the chat window to answer it only when the exact same dialog is still
there and the session remains in the foreground.  A dialog cut off at the top
of a short pane is never answered affirmatively without its missing context.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import time

_OPTION = re.compile(r"^\s*(?:│\s*)?(❯\s*)?(\d{1,2})\.\s+(.*\S)\s*$")
# Only a solid rule ends a dialog: a dotted rule can split a permission
# prompt's command from its reason, and both remain part of the question.
_RULE = re.compile(r"^\s*[─━▔═]{40,}\s*$")
_EMPTY_BOX = re.compile(r"^\s*❯\s*$")
_FOOTER = re.compile(r"Esc to cancel|Enter to (?:select|confirm)")
_BOX = re.compile(r"^\s*│\s?")
_CHECK = re.compile(r"^\[[ ✔✓x]\]\s*")
_TERMINAL_ONLY = re.compile(r"^(?:Type something|Chat about this)\b", re.I)
_NO = re.compile(r"^No\b", re.I)


def parse(screen: str) -> dict | None:
    """Return a bottom-of-pane dialog, or ``None`` when one is not drawn."""
    lines = [ln.rstrip() for ln in (screen or "").splitlines() if ln.strip()]
    if not lines:
        return None
    if any(_EMPTY_BOX.match(x) for x in lines[-8:]):
        return None                       # an input box is drawn: no dialog is up
    tail = len(lines)
    while tail and _FOOTER.search(lines[tail - 1]) and not _OPTION.match(lines[tail - 1]):
        tail -= 1
    footer = " ".join(lines[tail:])
    last = next((i for i in range(tail - 1, max(-1, tail - 7), -1) if _OPTION.match(lines[i])), None)
    if last is None:
        return None
    # Walk up through the run: n, n-1, ... 1, past descriptions and the rule
    # that splits off terminal-only choices.
    opts, want, i, first = [], None, last, None
    while i >= 0:
        m = _OPTION.match(lines[i])
        if m and (want is None or int(m.group(2)) == want):
            n = int(m.group(2))
            opts.append((n, m.group(3), bool(m.group(1))))
            if n == 1:
                first = i
                break
            want = n - 1
        i -= 1
    if first is None or len(opts) < 2:
        return None
    opts.reverse()
    top = first - 1
    question: list[str] = []
    while top >= 0 and not _RULE.match(lines[top]) and len(question) < 30:
        question.append(_BOX.sub("", lines[top]).strip())
        top -= 1
    # Cut unless its top is a full rule: off the top of the pane, or too long.
    cut = top < 0 or len(question) >= 30
    question.reverse()
    options, multi = [], False
    for n, label, selected in opts:
        if _CHECK.match(label):
            multi = True
            label = _CHECK.sub("", label)
        options.append({"n": n, "label": label, "selected": selected,
                        "terminal_only": bool(_TERMINAL_ONLY.match(label))})
    text = "\n".join(q for q in question if q)
    # The key is the whole dialog as drawn (descriptions too), less the
    # selection marker, which moves while it remains the same dialog.
    whole = "\n".join(re.sub(r"❯", " ", x) for x in lines[top + 1:tail])
    key = hashlib.sha1(whole.encode()).hexdigest()[:16]
    # "Enter to select" lists take the digit as the answer; any other list
    # is moved to and confirmed.
    confirm = "Enter to select" not in footer
    return {"question": text, "options": options, "multi": multi, "cut": cut, "key": key, "confirm": confirm}


def _capture(pane: str, run) -> str:
    try:
        return run(["tmux", "capture-pane", "-p", "-t", pane],
                   capture_output=True, text=True, timeout=5).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def answerable(dialog: dict, n: int) -> bool:
    option = next((option for option in dialog["options"] if option["n"] == n), None)
    if not option or option["terminal_only"] or dialog["multi"]:
        return False
    return not dialog["cut"] or bool(_NO.match(option["label"]))  # cut: never answer Yes blind


def read(target: dict, run=subprocess.run) -> dict | None:
    """The dialog now drawn in a target pane, with answerable choices marked."""
    if not target.get("pane"):
        return None
    dialog = parse(_capture(target["pane"], run))
    if dialog:
        for option in dialog["options"]:
            option["ok"] = answerable(dialog, option["n"])
    return dialog


def _gone(pane: str, key: str, run, sleep, waits: int = 8, every: float = 0.4) -> bool:
    """Whether dialog ``key`` has left the screen within roughly three seconds."""
    for _ in range(waits):
        sleep(every)
        dialog = parse(_capture(pane, run))
        if not dialog or dialog["key"] != key:
            return True
    return False


def answer(target: dict, key: str, n: int, run=subprocess.run, sleep=time.sleep,
           in_front=None) -> tuple[bool, str]:
    """Answer choice ``n`` only when the same dialog is still safe to answer."""
    if in_front is None:
        from agent_companion.server.chat import in_front
    pane = target.get("pane")
    if not pane:
        return False, "no window to answer in"
    dialog = parse(_capture(pane, run))
    if not dialog or dialog["key"] != key:
        return False, "that choice is no longer on its screen"
    if not answerable(dialog, n):
        return False, "answer that one in its terminal"
    front, why = in_front(target["pid"], pane, run=run)
    if not front:
        return False, why
    if not dialog["confirm"]:
        # The digit is the answer: one key, and never an Enter after it.
        try:
            run(["tmux", "send-keys", "-t", pane, str(n)], check=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False, "the key did not go"
        return (True, "") if _gone(pane, key, run, sleep) else (False, "pressed, but its screen did not change")
    # A confirm dialog: move with the arrows (they never submit), check it is
    # still this dialog with that choice selected, then Enter.  The dialog that
    # gets Enter is the one just checked.
    current = next((option["n"] for option in dialog["options"] if option["selected"]), None)
    if current is None:
        return False, "cannot tell which choice is selected"
    if current != n:
        try:
            keys = ["Down"] * (n - current) if n > current else ["Up"] * (current - n)
            run(["tmux", "send-keys", "-t", pane, *keys], check=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False, "the keys did not go"
        sleep(0.4)
        dialog = parse(_capture(pane, run))
        if not dialog or dialog["key"] != key or not any(
                option["selected"] and option["n"] == n for option in dialog["options"]):
            return False, "its screen did not move to that choice; nothing confirmed"
        front, why = in_front(target["pid"], pane, run=run)
        if not front or not answerable(dialog, n):
            return False, why or "answer that one in its terminal"
    try:
        run(["tmux", "send-keys", "-t", pane, "Enter"], check=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False, "Enter did not go"
    return (True, "") if _gone(pane, key, run, sleep) else (False, "confirmed, but the dialog is still up")
