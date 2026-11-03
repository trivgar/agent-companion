"""Speak up when a session needs permission.

Claude Code fires a PermissionRequest hook when a tool call needs approval. This
turns that into one spoken line in that session's own voice, so a blocked
session announces itself instead of sitting there looking idle.

Runs inside a hook with a 2s timeout, so every path here is fast, silent on
failure, and side-effect free. TTS is OPTIONAL: with no configured coordinator
or bundled Comms script, the hook exits 0 having done nothing.
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from agent_companion import config
from agent_companion.liveness import proc_ppid

WAITING_DIR = "session-companion-waiting"
BUNDLED_TTS_DIR = Path(__file__).resolve().parents[1] / "comms"
# The secure origin, because Web Push only exists on one and the installed app
# is registered against it. An http address here would open a second, separate
# copy of the dashboard beside the installed one.
APP_ORIGIN = str(config.get("url", "http://127.0.0.1:8770")).rstrip("/")
# Lines say "{you}" where they address the person at the keyboard: the
# `name` setting fills it, and with no name the address is dropped cleanly
# ("{you}, one quick thing?" reads "One quick thing?"). See address().
#
# Each speaker in the comms cast (comms/cast.tsv) has a tone in
# comms/personas/<Name>.md -- Juno warm, Delia dry, Vance brisk, Wren quiet --
# and a fixed sentence throws all of it away. These are the
# BODY only; speak.sh still prepends the rotating opener, so the two vary
# independently and the same pairing rarely repeats. Any other speaker gets
# the NEUTRAL line.
#
# Short on purpose: clips queue across every live session, and the whole
# message is "come here".
QUESTION_LINES: dict[str, tuple[str, ...]] = {
    "Juno":  ("{you}! Quick question when you get a sec.",
              "Hey {you}, got a question for you.",
              "{you}, one quick thing?"),
    "Delia": ("{you}, one question.",
              "{you}. A question, when convenient.",
              "There's a question for you, {you}."),
    "Vance": ("{you}. Question. Standing by.",
              "{you}. Blocked on a question.",
              "{you}. Need an answer."),
    "Wren":  ("{you}, I have a question, if that's all right.",
              "I'm not sure about something, {you}. A question.",
              "{you}, could I ask you something?"),
}
NEUTRAL_QUESTION = ("{you}, I have a question for you.",)

APPROVAL_LINES: dict[str, tuple[str, ...]] = {
    "Juno":  ("{you}, mind approving this",),
    "Delia": ("{you}, approval needed",),
    "Vance": ("{you}. Approve?",),
    "Wren":  ("{you}, could you check this one",),
}
NEUTRAL_APPROVAL = ("{you}, I need your OK",)


def lines_for(table: dict[str, tuple[str, ...]], key: str, speaker: str,
              neutral: tuple[str, ...]) -> tuple[str, ...]:
    """Your own cast's lines first: a `[question_lines]` or `[approval_lines]`
    table in the settings file, speaker name to a list of lines, so names
    stay out of this repo. Then the built-in table, then the neutral line."""
    mine = config.get(key)
    if isinstance(mine, dict):
        got = mine.get(speaker)
        if isinstance(got, str):
            got = [got]
        if isinstance(got, list):
            got = tuple(x for x in got if isinstance(x, str) and x.strip())
            if got:
                return got
    return table.get(speaker, neutral)



def address(line: str, name: str | None = None) -> str:
    """Fill "{you}" with the `name` setting, or drop it and its punctuation."""
    who = str(config.get("name", "") if name is None else name).strip()
    if who:
        return line.replace("{you}", who)
    line = re.sub(r"^\{you\}(?:\.\.\.|[,.!]| \u2014)\s*", "", line)
    line = re.sub(r",? \{you\}(?=[.,!?])", "", line)
    return line[:1].upper() + line[1:]


MAX_DETAIL = 80
MAX_SENTENCE = 200
MAX_DEPTH = 12

# Where the interesting part of a tool call lives, by tool.
DETAIL_KEYS = ("command", "file_path", "path", "url", "pattern")


# Words that are true of a call without saying what it does. `cd` is the one
# that matters: skipping it naively speaks its ARGUMENT, so "cd ~/.claude/tts &&
# rm -f held/*" announced itself as "tts".
_NOISE = {"sudo", "command", "exec", "cd", "env", "time", "nohup", "timeout",
          "then", "do", "builtin", "source", "."}


def _program(command: str) -> str:
    """The program a shell command actually runs."""
    import re
    for segment in re.split(r"&&|\|\||[;|&]", command):
        words = segment.split()
        i = 0
        while i < len(words):
            w = words[i]
            if "=" in w.split("/")[0]:            # VAR=value prefix
                i += 1; continue
            if w in _NOISE:
                i += 1
                # `cd x` and `timeout 30` are followed by an argument that is
                # not a program; without this, "timeout 30 ssh ..." announced
                # itself as "30".
                if w in ("cd", "source", ".") or (
                        w == "timeout" and i < len(words)
                        and words[i].rstrip("smhd").replace(".", "").isdigit()):
                    i += 1
                continue
            prog = w.rsplit("/", 1)[-1].strip("\"'()${}")
            if prog:
                return prog
            i += 1
    return ""


def detail_for(tool_name: str, tool_input) -> str:
    """The one bit of a tool call worth saying out loud.

    NOT the command. This used to read the raw value -- so a Bash approval came
    out of the phone as "I need your OK: Bash, cd tilde slash dot claude slash
    tts and and rm dash f held slash star two greater-than slash dev slash
    null". Piper cannot speak shell, and nobody should have to parse it by
    ear. The full text is in the notification and on the dashboard; the clip
    says which program, and that is enough to know whether you care.
    """
    if not isinstance(tool_input, dict):
        return ""

    command = tool_input.get("command")
    if isinstance(command, str) and command.strip():
        return _program(command)[:MAX_DETAIL]

    for key in DETAIL_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            value = " ".join(value.split())
            # A path read aloud is character soup. The last component is the
            # part that identifies it.
            if key in ("file_path", "path", "notebook_path") or "/" in value:
                value = value.rsplit("/", 1)[-1] or value
            return value[:MAX_DETAIL]
    return ""


def owning_session(pid: int, sessions: list[dict], ppid_of=proc_ppid,
                   cwd: str = "") -> dict | None:
    """Which session spawned this hook.

    The hook is a descendant of the claude process, not necessarily its child,
    so walk the ppid chain until a pid matches the registry. Bounded, and
    loop-safe against a malformed /proc.
    """
    by_pid = {s["pid"]: s for s in sessions}
    seen: set[int] = set()
    current = pid
    for _ in range(MAX_DEPTH):
        if current is None or current in seen or current <= 1:
            break
        seen.add(current)
        if current in by_pid:
            return by_pid[current]
        current = ppid_of(current)

    if cwd:
        for s in sessions:
            if s.get("cwd") == cwd:
                return s
    return None


def compose(payload: dict, speaker: str, project: str) -> str:
    """The sentence. Short: clips queue across every live session.

    Addressed to you rather than about you -- these are spoken aloud, and a
    clip that says "Vance needs permission" is describing a situation to nobody.

    A question is not an approval. AskUserQuestion is the tool that blocks for
    an ANSWER, and in bypassPermissions sessions it is the only thing that ever
    fires this hook -- 16 of the 21 announcements so far were questions being
    called permissions.
    """
    who = speaker or project or "A session"
    where = f" in {project}" if project and speaker else ""
    tool = payload.get("tool_name") or ""
    detail = detail_for(tool, payload.get("tool_input"))

    # speak.sh prepends "<Name> here, on <project>." to every clip, so naming
    # either again is pure repetition -- heard aloud it came out as "Wren
    # here, on my project. Wren has a question for you in
    # my-project." First person when the opener will introduce us;
    # third only when there is no speaker to do it.
    first_person = bool(speaker)

    if tool == "AskUserQuestion":
        if not first_person:
            return address(f"{{you}}, {who} has a question for you{where}.")[:MAX_SENTENCE]
        return address(random.choice(lines_for(QUESTION_LINES, "question_lines", speaker, NEUTRAL_QUESTION)))[:MAX_SENTENCE]

    if first_person:
        text = address(random.choice(lines_for(APPROVAL_LINES, "approval_lines", speaker, NEUTRAL_APPROVAL)))
    else:
        text = address(f"{{you}}, {who} needs your OK{where}")
    if tool:
        text += f": {tool}"
        if detail:
            text += f", {detail}"
    elif text[-1:] not in ".?!":
        # Vance's line already ends in a question mark; "Approve?." is not
        # a sentence.
        text += "."
    return text[:MAX_SENTENCE]


def speak(text: str, tts_dir: Path | None = None, speaker: str = "") -> bool:
    """Fire the clip and return. False when TTS is not installed.

    Arguments go as argv, never through a shell -- tool detail is attacker-
    adjacent text and must not reach a command line.

    `speaker` matters more than it looks. This runs detached (start_new_session)
    so a hook never blocks its session, but detaching reparents the process away
    from `claude`, and the coordinator identifies a speaker by walking the
    process tree to the owning claude pid. From a detached child that walk fails
    and falls back to a frozen environment id, which registers a SECOND roster
    row under a different auto-assigned name -- a phantom speaker that talks
    with settings nobody is editing. We already know who we are by this point,
    so say so explicitly via TTS_SPEAKER and let the coordinator skip the walk.
    """
    tts_dir = Path(tts_dir) if tts_dir else Path(str(config.get("tts_dir") or BUNDLED_TTS_DIR))
    script = tts_dir / "speak.sh"
    if not script.is_file():
        return False
    env = dict(os.environ)
    if speaker:
        env["TTS_SPEAKER"] = speaker
    try:
        subprocess.Popen(
            [str(script), text],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except OSError:
        return False
    return True


def mark_waiting(home: Path, pid: int) -> Path | None:
    """Drop a marker saying this session is blocked on you.

    It clears itself: the reader treats a session as waiting only while the
    marker is NEWER than the transcript. The moment the session gets an answer
    and carries on, the transcript grows past the marker and the flag goes away
    with nothing to clean up.
    """
    try:
        d = Path(home) / WAITING_DIR
        d.mkdir(parents=True, exist_ok=True)
        marker = d / str(pid)
        marker.touch()
        return marker
    except OSError:
        return None


def holding_session(payload: dict) -> dict | None:
    """The session a holding hook belongs to, found exactly: by the event's
    session_id, else up the process tree. Never by directory, as announce()
    may: a hook that outlived its session would otherwise mark whichever
    session works in the same folder, every minute, for as long as it held."""
    try:
        from agent_companion.registry import live_sessions

        home = Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude"))
        sessions = live_sessions(home / "sessions")
        sid = str(payload.get("session_id") or "")
        for s in sessions:
            if sid and s.get("session_id") == sid:
                return s
        return owning_session(os.getpid(), sessions)
    except Exception:
        return None


def session_status(session: dict) -> tuple[str, float]:
    """What Claude Code says the session is doing now, from its own registry
    entry, and when it last changed (epoch seconds, 0 if unsaid): "waiting"
    while a permission prompt or a question is open in its window (measured
    2026-10-12: from the moment the prompt shows, while a PermissionRequest
    hook still runs), "busy", "idle". ("gone", 0) once its process has ended;
    ("", 0) when it cannot be read."""
    try:
        from agent_companion.liveness import is_alive

        if not is_alive(session["pid"], session.get("proc_start")):
            return "gone", 0.0
        home = Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude"))
        rec = json.loads((home / "sessions" / f"{int(session['pid'])}.json").read_text())
        if not isinstance(rec, dict):
            return "", 0.0
        at = rec.get("statusUpdatedAt")
        return str(rec.get("status") or ""), float(at) / 1000 if isinstance(at, (int, float)) else 0.0
    except Exception:
        return "", 0.0


def session_state(session: dict) -> str:
    """session_status() without the time."""
    return session_status(session)[0]


def still_waiting(payload: dict, session: dict | None = None) -> bool:
    """Mark the session waiting again, without a word: for a hook that holds
    its session at a question or a permission, while it holds and as it hands
    the session to its own dialog. `session` is the one holding_session()
    found when the hook began; without it, it is looked up the same way.

    One mark at the start is not enough. The marker counts only while it is
    newer than the transcript, and Claude Code drains transcript writes on a
    100 ms timer, so the line that asked can land after the hook's first
    mark: on 2026-10-12 the marker went down at 05:50:08.12, the transcript
    moved past it, and the session read as not waiting for the ten minutes
    its question sat open.
    """
    try:
        home = Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude"))
        session = session or holding_session(payload)
        return bool(session) and mark_waiting(home, session["pid"]) is not None
    except Exception:
        return False


def _app_url(speaker: str = "") -> str:
    """Where a tap should land: that agent's own chat window on the dashboard,
    never the Claude app. No speaker, the dashboard itself.
    """
    import re
    from urllib.parse import quote
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{1,20}", speaker or ""):
        return f"{APP_ORIGIN}/?agent={quote(speaker)}"
    return APP_ORIGIN + "/"


def notify(text: str, speaker: str = "", session=None) -> None:
    """A tappable notification for a session that is blocked.

    Best-effort and fire-and-forget: this runs inside a hook with a 2s budget,
    so it must not wait on the phone. Failure is silent -- the spoken clip has
    already gone out and a missing notification is not worth breaking a session
    over.
    """
    url = _app_url(speaker)
    who = speaker or "A session"
    # From the dashboard itself, so Android labels it with our name and icon
    # rather than whichever app we borrowed. In-process: this module already has
    # the package importable, so there is no HTTP round trip to fail.
    try:
        from agent_companion.webpush import PushStore, push_dir
        ps = PushStore(push_dir())
        # Not into the Push page's feed: the chat tab already says it.
        if ps.send(f"{who} needs you", text[:300], url=url, tag=f"needs-{who}"):
            return
    except Exception:
        pass
    # Nobody has subscribed a device yet, or the push failed. The old route is
    # still there rather than nothing.
    tts = Path.home() / ".claude" / "tts"
    helper = tts / "notify-attention.sh"
    if not helper.is_file():
        return
    try:
        subprocess.Popen([str(helper), who, text[:300], url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception:
        pass


def main() -> int:
    """Hook entry point. Never raises, never blocks, always exits 0."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        announce(payload)
    except Exception:
        pass          # a hook must never break the session it runs in
    return 0


def announce(payload: dict, push: bool = True) -> str:
    """Mark the session waiting, say so, and (push) notify. Returns the
    speaker. deploy/permission_hook.py calls it with push=False: it pushes its
    own notice, one that opens the dashboard's Allow and Deny, not the Claude
    app."""
    speaker = ""
    try:
        from agent_companion.reader import SessionReader
        from agent_companion.registry import live_sessions

        home = Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude"))
        sessions = live_sessions(home / "sessions")
        session = owning_session(os.getpid(), sessions,
                                 cwd=payload.get("cwd") or "")

        speaker, project = "", ""
        if session:
            project = session.get("project", "")
            by_key, by_pane = SessionReader(home)._speakers()
            speaker = (by_key.get(f"p{session['pid']}.{session['proc_start']}")
                       or by_pane.get(session.get("tmux", ""), ""))

        if session:
            mark_waiting(home, session["pid"])
        line = compose(payload, speaker, project)
        speak(line, speaker=speaker)
        # A clip is heard once and only if you are in the room. A blocked
        # session is the one thing here that is genuinely waiting on you, so it
        # also gets something persistent to tap -- on the attention channel,
        # opening that session in the Claude app.
        if push:
            notify(line, speaker=speaker, session=session)
    except Exception:
        pass          # a hook must never break the session it runs in
    return speaker


if __name__ == "__main__":
    sys.exit(main())
