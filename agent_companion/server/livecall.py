"""Ring delivery and the owner-only live-call API."""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse

from .. import addons, agentvibes, fcm, livecall, ring
from . import asks, auth, chat, comms, push, sessions

router = APIRouter()
_CLOSED = {"ok": False, "closed": True, "detail": "No call is open."}


def _rows() -> list[dict]:
    try:
        return sessions.order(sessions._parts()["reader"].snapshot())
    except Exception:
        return []


def _projects() -> Path:
    return chat.claude_home() / "projects"


def _known() -> dict[str, dict]:
    """Only the direct-line targets that are safe to name to Gemini."""
    return {key: target for key, target in chat.known().items() if livecall.visible_target(target)}


def _branch(cwd: str) -> str:
    try:
        return str((sessions._parts()["git"].for_repo(cwd) or {}).get("branch") or "")
    except Exception:
        return ""


def _hold(name: str) -> None:
    """Hold a speaker's clips while the call talks to it, when a coordinator
    is set up (agentvibes.call_hold); each poll renews it."""
    tts = comms.tts_dir()
    if name and agentvibes.available(tts):
        agentvibes.call_hold(tts, name)


def _release(names) -> None:
    """Let go of the speakers the call no longer holds: quiet.sh speaks what
    they said meanwhile."""
    tts = comms.tts_dir()
    if names and agentvibes.available(tts):
        for name in names:
            agentvibes.call_release(tts, name)


def _send_push(title: str, body: str, url: str, tag: str, actions: list[dict]) -> bool:
    return push.store().send(title, body, url=url, tag=tag, actions=actions) > 0


def _send_ring(ring_id: str) -> bool:
    return fcm.send_ring(ring_id)


def start(app) -> None:
    """Start the one ring watcher when the server, not pytest, starts."""
    ring.Watcher(asks.pending, _rows, None, _send_push, _send_ring).start()


def _brief(ask_ids, rows: list[dict], now: float) -> tuple[list[dict], str]:
    """The answered ring's still-pending, non-private asks in ring order."""
    by_session = {row.get("session_id"): row for row in rows if row.get("session_id")}
    active = []
    for ask_id in ask_ids or []:
        ask = asks.get(ask_id)
        if not ask or ask.get("status") != "pending":
            continue
        row = by_session.get(ask.get("session_id")) or {}
        if livecall.private_place(row.get("cwd") or "", ask.get("project") or ""):
            continue
        active.append(ask)

    def replies_of(ask):
        row = by_session.get(ask.get("session_id")) or {}
        return livecall.last_replies(livecall.transcript_path(row, _projects()))

    return active, livecall.brief_lines(active, now, replies_of)


def _answer(call: livecall.Call, tool: str, args: dict) -> dict:
    """Take one safe answer from the ring brief, after its voice guards."""
    try:
        number = int(args.get("ask"))
    except (TypeError, ValueError):
        number = 0
    if not call.ring_asks:
        return {"done": False, "ask": number, "detail": "This call has no stuck asks to answer."}
    if not 1 <= number <= len(call.ring_asks):
        return {"done": False, "ask": number, "detail": f"There is no ask {number}: this call has {len(call.ring_asks)}."}
    ask_id = call.ring_asks[number - 1]
    ask = asks.get(ask_id) or {}

    def done(word: str, result: dict) -> dict:
        call.outcomes.append({"ask": number, "did": word})
        return result

    stopped = asks.not_waiting(ask_id)
    if stopped or ask.get("status") != "pending":
        return done("refused", {"done": False, "ask": number, "detail": stopped or "That ask is no longer waiting."})
    # A shortened request cannot be faithfully read back, so neither allow nor
    # deny travels through voice. The dashboard's full view remains the path.
    if ask.get("shortened"):
        return done("refused", {"done": False, "ask": number,
                                "detail": "That request is shortened here and needs your tap in the dashboard."})
    confirmed = args.get("confirmed") is True
    if tool == "answer_question":
        if not ask.get("questions"):
            return {"done": False, "ask": number, "detail": "That ask is a permission: use answer_permission."}
        answers, why = livecall.pick_answers(ask, args.get("picks"), True)
        if answers is None:
            return {"done": False, "ask": number, "detail": why}
        if livecall.pick_answers(ask, args.get("picks"), False)[0] is None:
            words = "; ".join(answers.values())
            if not confirmed:
                line = f"Answer {ask.get('speaker') or 'the session'}: {words}. Is that right?"
                if not livecall.speakable(line):
                    return {"done": False, "ask": number, "detail": "Those words cannot be read back whole. Answer in the dashboard."}
                livecall.ask_readback(call, number, words)
                return {"done": False, "ask": number, "read_back": line}
            ok, why = livecall.said_yes(call, number, words)
            if not ok:
                return {"done": False, "ask": number, "detail": why}
        got = asks.answer(ask_id, answers)
        if got:
            addons.on_ask("answered", got)
            return done("answered", {"done": True, "ask": number, "status": "answered"})
        return done("refused", {"done": False, "ask": number,
                                "detail": asks.not_waiting(ask_id) or "The answers did not fit it."})
    if ask.get("questions"):
        return {"done": False, "ask": number, "detail": "That ask is a question: use answer_question."}
    decision = str(args.get("decision") or "")
    if decision not in asks.DECISIONS:
        return {"done": False, "ask": number, "detail": "allow or deny"}
    if decision == "allow":
        cwd = next((row.get("cwd") or "" for row in _rows() if row.get("session_id") == ask.get("session_id")), "")
        why = livecall.needs_tap(ask.get("tool") or "", ask.get("what") or "", cwd)
        if why:
            return done("refused", {"done": False, "ask": number, "detail": why})
        words = f"allow {ask.get('tool') or ''}: {ask.get('what') or ''}"
        if not confirmed:
            line = f"Allow {ask.get('speaker') or 'the session'} to run {ask.get('tool') or ''}: {ask.get('what') or ''}?"
            if not livecall.speakable(line):
                return done("refused", {"done": False, "ask": number,
                                        "detail": "It cannot be read aloud on a call. That one needs your tap in the dashboard."})
            livecall.ask_readback(call, number, words)
            return {"done": False, "ask": number, "read_back": line}
        ok, why = livecall.said_yes(call, number, words)
        if not ok:
            return {"done": False, "ask": number, "detail": why}
    got = asks.decide(ask_id, decision)
    if got:
        addons.on_ask("decided", got)
        return done(decision, {"done": True, "ask": number, "status": decision})
    return done("refused", {"done": False, "ask": number,
                            "detail": asks.not_waiting(ask_id) or "That ask is no longer waiting."})


def _ask_session(call: livecall.Call, known: dict[str, dict], args: dict) -> dict:
    """Use chat.send directly so its foreground and waiting guards are kept.

    Deliberately do not call chat.record: caller words must not persist.
    """
    name = str(args.get("name") or "").strip()[:40]
    words = " ".join(str(args.get("text") or "").split())
    if not words:
        return {"sent": False, "name": name, "detail": "There was nothing to say."}
    target = known.get(name.lower())
    if not target:
        return {"sent": False, "name": name, "detail": f"No session called {name or 'that'} is one the call can reach."}
    livecall.watch(call, target, _projects(), asked=False)
    ok, why = chat.send(target, livecall.relay_words(words))
    if ok:
        livecall.watch(call, target, _projects(), asked=True)
        _hold(target["name"])
    return {"sent": ok, "name": target["name"], "detail": why or f"Typed into {target['name']}'s window. No reply yet."}


@router.post("/api/cave/ring/decide", dependencies=[Depends(auth.require_write)])
def ring_decide(body: dict = Body(default={})):
    """Take an alert button's one-time key before deciding its ask."""
    ask_id, decision = str(body.get("ask") or ""), str(body.get("decision") or "")
    if decision not in asks.DECISIONS:
        raise HTTPException(400, "allow or deny")
    ask = asks.get(ask_id)
    if decision == "allow" and ask and ask.get("shortened"):
        raise HTTPException(409, "open it and show all, then allow")
    if not ring.consume(ask_id, str(body.get("n") or "")):
        raise HTTPException(403, "that button was already used, or is not from an alert")
    got = asks.decide(ask_id, decision)
    if not got:
        raise HTTPException(409, asks.not_waiting(ask_id) or "that ask is no longer waiting")
    addons.on_ask("decided", got)
    return {"ok": True, "status": got["status"]}


@router.get("/api/ring/{ring_id}", dependencies=[Depends(auth.require_token)])
def ring_brief(ring_id: str):
    brief = ring.brief(ring_id)
    if not brief:
        raise HTTPException(404, "no such ring")
    return JSONResponse(brief, headers={"Cache-Control": "no-store"})


@router.post("/api/call/answer", dependencies=[Depends(auth.require_write)])
def call_answer(body: dict = Body(default={})):
    who = ring.answer(str(body.get("ring") or ""), str(body.get("n") or ""))
    if who is None:
        raise HTTPException(403, "that ring was already answered, or is not yours")
    return {"ok": True, "agent": who}


# The phone registers once, at comms' POST /api/tts/app, which also records it
# here as a ringer (fcm.save_device) when it says it can ring.


@router.post("/api/call/token", dependencies=[Depends(auth.require_write)])
def call_token(body: dict = Body(default={})):
    body = body if isinstance(body, dict) else {}
    known = _known()
    target = known.get(str(body.get("agent") or "").strip().lower())
    call_id, ring_id = str(body.get("call_id") or ""), str(body.get("ring") or "")
    current, _ = livecall.current(call_id) if call_id else (None, [])
    if current is not None:
        ask_ids, ring_id = list(current.ring_asks), current.ring
    else:
        # Claimed before the token is minted, so two starts at once cannot
        # both carry the brief; released if the call does not open.
        ask_ids = ring.claim(ring_id) or [] if ring_id else []
    claimed = current is None and bool(ring_id) and bool(ask_ids)
    active, brief = _brief(ask_ids, _rows(), time.time()) if ask_ids else ([], "")
    try:
        out, released = livecall.open_call(target["name"] if target else "", call_id, known, _projects(),
                                            ring=ring_id if active else "", ring_asks=[ask["id"] for ask in active],
                                            brief=brief)
    except livecall.CallRefused as exc:
        _release(exc.release)
        if claimed:
            ring.release(ring_id)
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=409, headers={"Cache-Control": "no-store"})
    _release(released)
    if out["agent"]:
        _hold(out["agent"])
    return JSONResponse({"ok": True, **out, "brief": len(active)}, headers={"Cache-Control": "no-store"})


@router.post("/api/call/tool/{tool}", dependencies=[Depends(auth.require_write)])
def call_tool(tool: str, body: dict = Body(default={})):
    if tool not in livecall.TOOL_NAMES:
        raise HTTPException(404, "no such tool")
    body = body if isinstance(body, dict) else {}
    call, released = livecall.current(str(body.get("call_id") or ""))
    _release(released)
    if not call:
        return JSONResponse(_CLOSED, status_code=409)
    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    known = _known()
    if tool == "list_sessions":
        result = livecall.list_sessions(_rows(), _branch)
    elif tool == "ask_session":
        result = _ask_session(call, known, args)
    elif tool in ("answer_question", "answer_permission"):
        result = _answer(call, tool, args)
    else:
        name = str(args.get("name") or "").strip()[:40]
        target = known.get(name.lower())
        result = (livecall.session_update(call, target, _projects()) if target else
                  {"name": name, "replies": [], "detail": f"No session called {name or 'that'} is one the call can reach."})
    return JSONResponse({"ok": True, "result": result}, headers={"Cache-Control": "no-store"})


@router.post("/api/call/updates", dependencies=[Depends(auth.require_write)])
def call_updates(body: dict = Body(default={})):
    call, released = livecall.current(str((body or {}).get("call_id") or ""))
    _release(released)
    if not call:
        return JSONResponse(_CLOSED, status_code=409)
    updates = livecall.updates(call)
    for name in livecall.held_names(call):
        _hold(name)
    return JSONResponse({"ok": True, "updates": updates}, headers={"Cache-Control": "no-store"})


@router.post("/api/call/heard", dependencies=[Depends(auth.require_write)])
def call_heard(body: dict = Body(default={})):
    call, released = livecall.current(str((body or {}).get("call_id") or ""))
    _release(released)
    if not call:
        return JSONResponse(_CLOSED, status_code=409)
    livecall.hear(call, str((body or {}).get("text") or ""))
    return {"ok": True}


@router.post("/api/call/end", dependencies=[Depends(auth.require_write)])
def call_end(body: dict = Body(default={})):
    _release(livecall.end(str((body or {}).get("call_id") or "")))
    return {"ok": True}
