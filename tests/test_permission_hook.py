"""The hook that waits for the page's answer (agent_companion/hooks/permission_hook.py).
Everything is a fake: no server, no signals, no clock."""
import io
import json

import pytest

from agent_companion.hooks import permission_hook as h

KINDS = ["question", "permission"]


def _event(kind):
    if kind == "question":
        return {"tool_name": "AskUserQuestion", "hook_event_name": "PreToolUse", "cwd": "/w/proj", "session_id": "s1",
                "tool_input": {"questions": [{"question": "Ship?", "options": [{"label": "Yes"}]}]}}
    return {"tool_name": "Bash", "hook_event_name": "PermissionRequest", "cwd": "/w/proj", "session_id": "s1",
            "tool_input": {"command": "git push"}}


def _fake(monkeypatch, kind, states=None, ppid=None):
    """A fake clock (60 s a sleep) and a fake session."""
    clock = [0.0]
    monkeypatch.setattr(h.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(h.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + 60))
    monkeypatch.setattr(h.time, "time", lambda: 1000.0 + clock[0])
    monkeypatch.setattr(h.signal, "signal", lambda *a: None)
    monkeypatch.setattr(h, "waiting", lambda ev: "Ada")
    me = {"pid": 4242, "proc_start": "7", "kind": "interactive"}
    monkeypatch.setattr(h, "holding", lambda ev: me)
    say = states or (lambda t: ("busy", 0.0) if kind == "question" else ("waiting", 100.0))
    monkeypatch.setattr(h, "status", lambda m: (lambda v: v if isinstance(v, tuple) else (v, 0.0))(say(clock[0])))
    monkeypatch.setattr(h.os, "getppid", lambda: (ppid or (lambda t: 1000))(clock[0]))
    marks = []
    monkeypatch.setattr(h, "still_waiting", lambda ev, m=None: marks.append((clock[0], m)))
    return clock, marks


def _run(monkeypatch, kind, poll, states=None, ppid=None):
    clock, marks = _fake(monkeypatch, kind, states, ppid)
    calls = []

    def fake(path, body=None, timeout=5):
        calls.append((clock[0], path))
        if path == "/api/asks":
            return {"ok": True, "id": "ab12"}
        if path.endswith("/withdraw"):
            return {"ok": True}
        return poll(clock[0])
    monkeypatch.setattr(h, "_call", fake)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_event(kind))))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert h.main() == 0
    return out.getvalue(), calls, marks


PENDING = lambda t: {"ok": True, "status": "pending"}


def test_a_question_hands_back_the_picks_as_the_tools_own(monkeypatch):
    answers = {"Ship?": "Yes"}
    out, calls, _ = _run(monkeypatch, "question", lambda t: {"ok": True, "status": "answered", "answers": answers})
    got = json.loads(out)["hookSpecificOutput"]
    assert got["permissionDecision"] == "allow" and got["updatedInput"]["answers"] == answers
    assert got["updatedInput"]["questions"] == _event("question")["tool_input"]["questions"]


def _recorded_question(tmp_path, question="Ship?"):
    tool_use_id = "toolu_123"
    transcript = tmp_path / "transcript.jsonl"
    return {
        **_event("question"),
        "tool_use_id": tool_use_id,
        "transcript_path": str(transcript),
        "tool_input": {"questions": [{"question": question, "options": [{"label": "Yes"}]}]},
    }, transcript, tool_use_id


def _question_posted(monkeypatch, event):
    posted = []
    monkeypatch.setattr(h, "waiting", lambda ev: "")
    monkeypatch.setattr(h, "holding", lambda ev: None)
    monkeypatch.setattr(h, "still_waiting", lambda *args: None)
    monkeypatch.setattr(h.signal, "signal", lambda *args: None)
    monkeypatch.setattr(h, "_post", lambda *args: posted.append(args) or {})
    monkeypatch.setattr(h, "FLUSH_S", 0)
    monkeypatch.setattr(h.time, "sleep", lambda seconds: None)
    assert h.question(event) == 0
    return posted


def test_a_question_in_the_transcript_is_asked(monkeypatch, tmp_path):
    event, transcript, tool_use_id = _recorded_question(tmp_path)
    transcript.write_text('{"tool_use_id": "' + tool_use_id + '"}\n')
    assert _question_posted(monkeypatch, event)


def test_an_unrecorded_prompt_suggestion_is_dropped(monkeypatch, tmp_path):
    event, _, _ = _recorded_question(tmp_path, "Pick the suggestion that matches what the user might type next")
    monkeypatch.setattr(h, "RECORD_S", 0)
    assert _question_posted(monkeypatch, event) == []


def test_an_unrecorded_ordinary_question_is_asked(monkeypatch, tmp_path):
    event, _, _ = _recorded_question(tmp_path)
    monkeypatch.setattr(h, "RECORD_S", 0)
    assert _question_posted(monkeypatch, event)


def test_a_subagent_question_is_asked_without_a_transcript(monkeypatch, tmp_path):
    event, _, _ = _recorded_question(tmp_path, "Pick the suggestion that matches what the user might type next")
    event["agent_id"] = "agent-1"
    assert _question_posted(monkeypatch, event)


def test_an_unreadable_transcript_uses_the_side_query_rule(monkeypatch, tmp_path):
    event, transcript, _ = _recorded_question(tmp_path, "Pick the suggestion that matches what the user might type next")
    transcript.write_text("unrelated transcript line\n")

    def unreadable(*args, **kwargs):
        raise PermissionError("unreadable")

    monkeypatch.setattr("builtins.open", unreadable)
    assert _question_posted(monkeypatch, event) == []


def test_a_permission_hands_back_allow_or_deny(monkeypatch):
    for st, want in (("allow", "allow"), ("deny", "deny")):
        out, _, _ = _run(monkeypatch, "permission", lambda t, st=st: {"ok": True, "status": st})
        d = json.loads(out)["hookSpecificOutput"]
        assert d["hookEventName"] == "PermissionRequest" and d["decision"]["behavior"] == want


def test_a_question_is_not_a_permission_and_the_other_way_round(monkeypatch):
    for ev in ({**_event("question"), "hook_event_name": "PermissionRequest"},
               {**_event("permission"), "hook_event_name": "PreToolUse"}):
        monkeypatch.setattr(h, "_call", lambda *a, **k: pytest.fail("asked"))
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(ev)))
        assert h.main() == 0


def test_garbage_in_still_exits_zero(monkeypatch):
    for text in ("", "{", "[1]"):
        monkeypatch.setattr("sys.stdin", io.StringIO(text))
        assert h.main() == 0


@pytest.mark.parametrize("kind", KINDS)
def test_it_holds_past_nine_minutes_and_hands_over_marked_waiting(kind, monkeypatch):
    def poll(t):
        if t > 3 * 3600:                           # the dashboard goes down after three hours
            raise OSError("down")
        return {"ok": True, "status": "pending"}
    out, calls, marks = _run(monkeypatch, kind, poll)
    assert out == "", "nothing decided for the owner"
    assert max(t for t, p in calls if p == "/api/asks/ab12") > 3 * 3600
    withdrew = [t for t, p in calls if p.endswith("/withdraw")]
    assert len(withdrew) == 1 and 3 * 3600 + h.QUIET_S <= withdrew[0] <= 3 * 3600 + h.QUIET_S + 120
    assert marks[0][0] <= 60 and marks[0][1]["pid"] == 4242
    assert len(marks) > 100 and marks[-1][0] == withdrew[0]


@pytest.mark.parametrize("kind", KINDS)
def test_a_session_that_died_withdraws_and_marks_nothing(kind, monkeypatch):
    live = "busy" if kind == "question" else "waiting"
    for states, ppid in (((lambda t: "gone" if t > 600 else live), None),
                         (None, (lambda t: 1000 if t <= 600 else 398))):
        out, calls, marks = _run(monkeypatch, kind, PENDING, states=states, ppid=ppid)
        withdrew = [t for t, p in calls if p.endswith("/withdraw")]
        assert out == "" and withdrew and 600 < withdrew[0] <= 720
        assert all(t <= 600 for t, _ in marks)


def test_a_permission_answered_in_its_own_window_ends_without_a_mark(monkeypatch):
    out, calls, marks = _run(monkeypatch, "permission", PENDING,
                             states=lambda t: ("waiting", 999.9) if t < 300 else ("busy", 1000.0 + 290))
    withdrew = [t for t, p in calls if p.endswith("/withdraw")]
    assert out == "" and len(withdrew) == 1 and 300 <= withdrew[0] <= 360
    assert all(t < 300 for t, _ in marks)


def test_answered_before_the_first_look_is_settled_too(monkeypatch):
    out, calls, marks = _run(monkeypatch, "permission", PENDING, states=lambda t: ("idle", 1000.5))
    assert out == "" and [t for t, p in calls if p.endswith("/withdraw")] == [60.0] and marks == []


def test_the_next_prompt_is_not_taken_for_this_one(monkeypatch):
    out, calls, _ = _run(monkeypatch, "permission", PENDING,
                         states=lambda t: ("waiting", 999.9) if t < 300 else ("waiting", 1000.0 + 280))
    assert len([t for t, p in calls if p.endswith("/withdraw")]) == 1
    out, calls, _ = _run(monkeypatch, "permission", PENDING, states=lambda t: ("waiting", 1000.0 + 30))
    assert [t for t, p in calls if p.endswith("/withdraw")] == [60.0]


def test_a_permission_never_shown_as_a_prompt_keeps_the_old_nine_minutes(monkeypatch):
    out, calls, marks = _run(monkeypatch, "permission", PENDING, states=lambda t: ("busy", 990.0))
    withdrew = [t for t, p in calls if p.endswith("/withdraw")]
    assert out == "" and len(withdrew) == 1 and h.UNSHOWN_S < withdrew[0] <= h.UNSHOWN_S + 120 and marks == []


@pytest.mark.parametrize("kind", KINDS)
def test_it_answers_only_with_the_owners_answer(kind, monkeypatch):
    odd = "allow" if kind == "question" else "answered"
    out, calls, _ = _run(monkeypatch, kind, lambda t: {"ok": True, "status": odd if t > 120 else "pending"})
    assert out == "" and calls[-1][1] == "/api/asks/ab12/withdraw"


@pytest.mark.parametrize("kind", KINDS)
def test_the_dashboard_down_at_the_ask_still_marks_the_session(kind, monkeypatch):
    clock, marks = _fake(monkeypatch, kind)
    monkeypatch.setattr(h, "FLUSH_S", 0)
    monkeypatch.setattr(h, "_call", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_event(kind))))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert h.main() == 0 and out.getvalue() == "" and [m for _, m in marks] == [{"pid": 4242, "proc_start": "7", "kind": "interactive"}]


@pytest.mark.parametrize("kind", KINDS)
def test_ended_by_claude_code_withdraws_and_marks_nothing(kind, monkeypatch):
    clock, marks = _fake(monkeypatch, kind)
    handlers, calls = {}, []
    monkeypatch.setattr(h.signal, "signal", lambda sig, fn: handlers.__setitem__(sig, fn))
    monkeypatch.setattr(h, "_call", lambda path, body=None, timeout=5: calls.append(path) or (
        {"ok": True, "id": "ab12"} if path == "/api/asks" else {"ok": True, "status": "pending"}))
    monkeypatch.setattr(h.time, "sleep", lambda s: handlers[h.signal.SIGTERM](h.signal.SIGTERM, None))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_event(kind))))
    with pytest.raises(SystemExit) as ex:
        h.main()
    assert ex.value.code == 0 and calls[-1] == "/api/asks/ab12/withdraw" and marks == []


@pytest.mark.parametrize("kind", KINDS)
def test_ended_while_the_ask_is_posted_still_takes_it_back(kind, monkeypatch):
    clock, marks = _fake(monkeypatch, kind)
    handlers, calls = {}, []
    monkeypatch.setattr(h.signal, "signal", lambda sig, fn: handlers.__setitem__(sig, fn))

    def fake(path, body=None, timeout=5):
        calls.append(path)
        if path == "/api/asks":
            handlers[h.signal.SIGTERM](h.signal.SIGTERM, None)      # ended mid-post
            return {"ok": True, "id": "ab12"}
        return {"ok": True}
    monkeypatch.setattr(h, "_call", fake)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_event(kind))))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert h.main() == 0 and out.getvalue() == ""
    assert calls == ["/api/asks", "/api/asks/ab12/withdraw"] and marks == []


def test_a_question_nobody_can_answer_goes_straight_to_the_terminal(monkeypatch):
    clock, marks = _fake(monkeypatch, "question")
    monkeypatch.setattr(h, "FLUSH_S", 0)
    calls = []
    monkeypatch.setattr(h, "_call", lambda path, body=None, timeout=5: calls.append(path) or (
        {"ok": True, "id": "ab12", "answerable": False} if path == "/api/asks" else {"ok": True}))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_event("question"))))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert h.main() == 0 and out.getvalue() == ""
    assert calls == ["/api/asks", "/api/asks/ab12/withdraw"] and len(marks) == 1


def test_the_hook_reads_the_servers_own_token_file(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from agent_companion.server import auth
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    tok = auth.load_or_create_token()
    assert h._token() == tok


def test_the_hook_talks_to_loopback_even_when_url_is_the_phones_address(monkeypatch):
    from agent_companion.hooks import permission_hook
    monkeypatch.setenv("AGENT_COMPANION_URL", "https://box.example.org")
    monkeypatch.setenv("AGENT_COMPANION_SERVER_PORT", "9123")
    assert permission_hook._url() == "http://127.0.0.1:9123"


@pytest.mark.parametrize("var", ["AGENT_COMPANION_JOB", "COMMS_JOB"])
def test_headless_jobs_are_never_asked(monkeypatch, var):
    monkeypatch.setenv(var, "mail")
    monkeypatch.setattr(h, "_call", lambda *a, **k: pytest.fail("asked"))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_event("Bash"))))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert h.main() == 0 and out.getvalue() == ""
