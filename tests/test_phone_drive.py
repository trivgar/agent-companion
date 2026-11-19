"""The phone driver's relay (app/phonedrive.py): jobs, the long poll, one
command at a time, the lazy timers and the files. A fake device and a fake
push, short timings, nothing real."""
import asyncio
import base64
import json
import stat

import pytest

from agent_companion.phone import drive as P

DID = "emu-0123456789abcdef"
PID = "phone-0123456789abcdef"
FAST = dict(tick=0.005, pickup_resend_s=0.1, pickup_give_up_s=0.25, starting_s=0.3, cmd_wait_s=0.3,
            poll_hold_s=0.15, stall_s=10.0, lock_wait_s=10.0, lost_s=10.0, sweep_every_s=3600.0,
            forget_s=3600.0)
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


def make(tmp_path, **kw):
    pushes, notes = [], []
    clock = kw.pop("clock", None)
    r = P.Relay(tmp_path / "phone", push=lambda dev, job: pushes.append((dev["fcm"], job)) or "ok",
                notify=lambda title, body: notes.append(body), key_path=tmp_path / "agent.key",
                **({"clock": clock} if clock else {}), **{**FAST, **kw})
    r.pushes, r.notes = pushes, notes
    return r


def register(r, did=DID, kind="emulator", driver_on=True, model="test-emulator"):
    return r.register({"device_id": did, "kind": kind, "model": model, "android": "15",
                       "fcm": "tok-" + did[:5], "driver_on": driver_on, "app_version": "1.1700-emu"})


async def started(r, device="emulator", task="open Settings", agent="agent-one", did=DID, key=""):
    """Start a job and play the device until it runs: (the start's reply, the job id)."""
    t = asyncio.create_task(r.start(device, task, agent, key=key))
    got = await r.next(did)
    assert got["type"] == "job", got
    jid = got["job"]["id"]
    r.event({"device_id": did, "job": jid, "kind": "started"})
    return await t, jid


def rows(r, jid):
    return [json.loads(x) for x in (r.data / "jobs" / jid / "actions.jsonl").read_text().splitlines()]


class Clock:
    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_devices_pair_once_with_a_hashed_secret_and_refuse_every_later_call(tmp_path, caplog):
    r = make(tmp_path)
    body = {"device_id": DID, "kind": "emulator", "model": "test-emulator", "android": "15",
            "fcm": "tok", "driver_on": True, "app_version": "1.1700-emu"}
    paired = r.register(body)
    secret = paired["device_secret"]
    stored = r.devices()["devices"][DID]
    assert stored["secret_sha256"] == P._secret_hash(secret) and secret not in json.dumps(stored)
    assert r.register(body)["code"] == "bad_device_secret"
    assert r.register(body, secret="wrong")["code"] == "bad_device_secret"
    assert r.register(body, secret=secret)["ok"] is True
    assert asyncio.run(r.next(DID, secret=""))["code"] == "bad_device_secret"
    assert r.result({"device_id": DID, "job": "missing", "cmd_id": "c-000001", "result": {}},
                    secret="wrong")["code"] == "bad_device_secret"
    assert r.event({"device_id": DID, "job": "missing", "kind": "started"},
                   secret="wrong")["code"] == "bad_device_secret"
    assert "phone device secret refused" in caplog.text


def test_a_job_capability_is_hashed_and_refuses_missing_wrong_or_another_jobs_cap(tmp_path):
    r = make(tmp_path)
    register(r)

    async def one(task):
        t = asyncio.create_task(r.start("emulator", task, "agent-one"))
        job = await r.next(DID)
        jid = job["job"]["id"]
        r.event({"device_id": DID, "job": jid, "kind": "started"})
        return jid, await t

    async def go():
        first, started_first = await one("open Settings")
        cap = started_first["job_cap"]
        missing = await r.command(first, "c-000001", "look", {}, cap="")
        wrong = await r.command(first, "c-000001", "look", {}, cap="wrong")
        assert r.end(first, cap=cap)["ok"]
        await r.next(DID)
        second, started_second = await one("open Calendar")
        other = await r.command(second, "c-000001", "look", {}, cap=cap)
        good = r.end(second, cap=started_second["job_cap"])
        return first, cap, missing, wrong, other, good

    first, cap, missing, wrong, other, good = asyncio.run(go())
    saved = (r.data / "jobs" / first / "job.json").read_text()
    assert cap not in saved and "cap_sha256" in saved
    assert missing["code"] == wrong["code"] == other["code"] == "no_cap"
    assert good["ok"] is True


def test_a_job_wakes_the_device_with_its_id_alone_and_runs(tmp_path):
    r = make(tmp_path)
    register(r)
    reply, jid = asyncio.run(started(r))
    assert reply["ok"] and reply["job"]["state"] == "running" and reply["job"]["agent"] == "agent-one"
    assert r.pushes == [("tok-emu-0", jid)]
    kinds = [x["kind"] for x in rows(r, jid)]
    assert kinds[0] == "start" and sorted(kinds[1:3]) == ["fetched", "push"]


def test_the_push_waits_in_a_thread_never_on_the_event_loop(tmp_path):
    import threading
    seen = {}
    r = make(tmp_path)
    r.push = lambda dev, job: seen.update(thread=threading.get_ident()) or "ok"
    register(r)

    async def go():
        seen["loop"] = threading.get_ident()
        return await started(r)
    asyncio.run(go())
    assert seen["thread"] != seen["loop"]


def test_a_gone_push_never_ends_a_job_the_device_already_took(tmp_path):
    import time as _time
    r = make(tmp_path)

    def slow_gone(dev, job):
        _time.sleep(0.05)
        return "gone"
    r.push = slow_gone
    register(r)
    reply, jid = asyncio.run(started(r))
    assert reply["ok"] and reply["job"]["state"] == "running"
    assert [x["result"] for x in rows(r, jid) if x["kind"] == "push"] == ["gone"]


def test_one_live_job_per_device(tmp_path):
    r = make(tmp_path)
    register(r)

    async def go():
        await started(r)
        return await r.start("emulator", "another", "agent-two")
    second = asyncio.run(go())
    assert second["code"] == "busy" and "open Settings" in second["text"]


def test_refusals_say_why(tmp_path):
    r = make(tmp_path)
    assert asyncio.run(r.start("tablet", "x", "a"))["code"] == "bad_args"
    assert asyncio.run(r.start("emulator", "   ", "a"))["code"] == "bad_args"
    assert asyncio.run(r.start("emulator", "x", "a"))["code"] == "no_device"
    register(r, driver_on=False)
    off = asyncio.run(r.start("emulator", "x", "a"))
    assert off["code"] == "driver_off" and "Accessibility" in off["text"]


def test_the_phone_is_the_phone_agents_alone(tmp_path):
    r = make(tmp_path)
    register(r, did=PID, kind="phone")
    assert r.approve_device(PID)["ok"] is True
    assert asyncio.run(r.start("phone", "x", "agent-one"))["code"] == "no_key"
    (tmp_path / "agent.key").write_text("k" * 40 + "\n")
    assert asyncio.run(r.start("phone", "x", "agent-one", key="wrong"))["code"] == "no_key"
    reply, jid = asyncio.run(started(r, device="phone", did=PID, key="k" * 40))
    assert reply["ok"]
    assert asyncio.run(r.command(jid, "c-000001", "look", {}))["code"] == "no_key"
    assert r.end(jid)["code"] == "no_key"


def test_one_resend_then_the_phone_isnt_answering(tmp_path):
    r = make(tmp_path)
    register(r, did=PID, kind="phone")
    assert r.approve_device(PID)["ok"] is True
    (tmp_path / "agent.key").write_text("k" * 40)
    out = asyncio.run(r.start("phone", "read the battery", "phone-agent", key="k" * 40))
    assert out["code"] == "expired" and out["text"] == "the phone isn't answering"
    assert len(r.pushes) == 2


def test_a_gone_push_address_fails_at_once(tmp_path):
    r = make(tmp_path)
    r.push = lambda dev, job: "gone"
    register(r)
    out = asyncio.run(r.start("emulator", "x", "agent-one"))
    assert out["code"] == "failed" and "push address" in out["text"]


def test_a_command_goes_to_the_device_and_its_answer_comes_back(tmp_path):
    r = make(tmp_path)
    register(r)

    async def go():
        _, jid = await started(r)
        t = asyncio.create_task(r.command(jid, "c-000001", "tap", {"n": 4}))
        got = await r.next(DID)
        assert got == {"type": "cmd", "job": jid, "cmd": {"cmd_id": "c-000001", "verb": "tap", "args": {"n": 4}}}
        assert r.result({"device_id": DID, "job": jid, "cmd_id": "c-000001",
                         "result": {"ok": True, "text": "Settings\n1 field Search settings",
                                    "target": "button Search"}})["ok"]
        return jid, await t
    jid, out = asyncio.run(go())
    assert out == {"ok": True, "code": "", "text": "Settings\n1 field Search settings", "target": "button Search"}
    assert (r.data / "jobs" / jid / "looks" / "c-000001.txt").read_text().startswith("Settings")
    res = [x for x in rows(r, jid) if x["kind"] == "result"][0]
    assert res["target"] == "button Search" and "text" not in res     # screen text lives in looks/, swept
    assert r.jobs[jid]["last_action"] == "tap: button Search"


def test_one_command_at_a_time_and_a_resent_one_is_never_run_twice(tmp_path):
    r = make(tmp_path)
    register(r)

    async def go():
        _, jid = await started(r)
        t = asyncio.create_task(r.command(jid, "c-000001", "look", {}))
        await asyncio.sleep(0.02)
        busy = await r.command(jid, "c-000002", "look", {})
        await r.next(DID)
        r.result({"device_id": DID, "job": jid, "cmd_id": "c-000001", "result": {"ok": True, "text": "a"}})
        first = await t
        again = await r.command(jid, "c-000001", "look", {})
        idle = await r.next(DID)
        return busy, first, again, idle
    busy, first, again, idle = asyncio.run(go())
    assert busy["code"] == "busy"
    assert again == first
    assert idle == {"type": "idle"}


def test_a_command_id_is_its_jobs_alone(tmp_path):
    r = make(tmp_path)
    register(r)

    async def run_one(text):
        _, jid = await started(r)
        t = asyncio.create_task(r.command(jid, "c-000001", "look", {}))
        got = await r.next(DID)
        assert got["type"] == "cmd" and got["job"] == jid
        r.result({"device_id": DID, "job": jid, "cmd_id": "c-000001", "result": {"ok": True, "text": text}})
        out = await t
        r.end(jid)
        assert (await r.next(DID))["type"] == "stop"
        return out

    async def go():
        return await run_one("first screen"), await run_one("second screen")
    a, b = asyncio.run(go())
    assert a["text"] == "first screen" and b["text"] == "second screen"


def test_a_command_or_its_answer_lost_goes_out_again_and_nothing_wedges(tmp_path):
    r = make(tmp_path)
    register(r)

    async def go():
        _, jid = await started(r)
        t = asyncio.create_task(r.command(jid, "c-000001", "look", {}))
        took = await r.next(DID)                   # the device takes it; its answer never arrives
        first = await t                            # so the agent hears nothing in cmd_wait_s
        t2 = asyncio.create_task(r.command(jid, "c-000001", "look", {}))   # and sends it again
        again = await r.next(DID)                  # the device asks again: the same command
        r.result({"device_id": DID, "job": jid, "cmd_id": "c-000001", "result": {"ok": True, "text": "a"}})
        second = await t2
        t3 = asyncio.create_task(r.command(jid, "c-000002", "look", {}))
        nxt = await r.next(DID)
        r.result({"device_id": DID, "job": jid, "cmd_id": "c-000002", "result": {"ok": True, "text": "b"}})
        return jid, took, first, again, second, nxt, await t3
    jid, took, first, again, second, nxt, third = asyncio.run(go())
    assert first["code"] == "timeout"
    assert again == took and again["cmd"]["cmd_id"] == "c-000001"
    assert second["ok"] is True and second["text"] == "a"
    assert nxt["cmd"]["cmd_id"] == "c-000002" and third["text"] == "b"    # not busy: nothing wedged
    assert [x["cmd_id"] for x in rows(r, jid) if x["kind"] == "again"] == ["c-000001"]


def test_a_watch_poll_hears_a_stop_mid_command(tmp_path):
    r = make(tmp_path)
    register(r)

    async def go():
        _, jid = await started(r)
        command = asyncio.create_task(r.command(jid, "c-000001", "look", {}))
        took = await r.next(DID)
        watch = asyncio.create_task(r.next(DID, holds=jid, running="c-000001"))
        await asyncio.sleep(FAST["tick"] * 2)
        r.stop(jid)
        return took, await watch, await command

    took, stop, command = asyncio.run(go())
    assert took["type"] == "cmd" and took["cmd"]["cmd_id"] == "c-000001"
    assert stop["type"] == "stop" and stop["job"] == took["job"] and stop["why"] == "stopped"
    assert command["code"] == "ended"


def test_a_watch_poll_never_takes_a_job_or_a_command(tmp_path):
    r = make(tmp_path, poll_hold_s=0.02)
    register(r)

    async def go():
        _, jid = await started(r)
        command = asyncio.create_task(r.command(jid, "c-000001", "look", {}))
        took = await r.next(DID)
        idle = await r.next(DID, holds=jid, running="c-000001")
        r.result({"device_id": DID, "job": jid, "cmd_id": "c-000001", "result": {"ok": True, "text": "a"}})
        await command
        r.end(jid)
        second = asyncio.create_task(r.start("emulator", "open Calendar", "agent-one"))
        first_stop = await r.next(DID, holds=jid, running="c-000001")
        second_job = await r.next(DID)
        r.event({"device_id": DID, "job": second_job["job"]["id"], "kind": "started"})
        return took, idle, first_stop, second_job, await second

    took, idle, first_stop, second_job, second = asyncio.run(go())
    assert took["type"] == "cmd"
    assert idle == {"type": "idle"}
    assert first_stop["type"] == "stop" and first_stop["job"] == took["job"]
    assert second_job["type"] == "job" and second["ok"]


def test_a_watch_poll_keeps_the_device_heard(tmp_path):
    clock = Clock()
    r = make(tmp_path, clock=clock, lost_s=10.0, poll_hold_s=100.0, stall_s=100.0, cmd_wait_s=100.0)
    register(r)

    async def go():
        _, jid = await started(r)
        command = asyncio.create_task(r.command(jid, "c-000001", "look", {}))
        await r.next(DID)
        watch = asyncio.create_task(r.next(DID, holds=jid, running="c-000001"))
        for _ in range(3):
            await asyncio.sleep(FAST["tick"] * 2)
            clock.advance(6.0)
        await asyncio.sleep(FAST["tick"] * 2)
        state = r.jobs[jid]["state"]
        r.stop(jid)
        return state, await watch, await command

    state, stop, command = asyncio.run(go())
    assert state == "running"
    assert stop["type"] == "stop" and command["code"] == "ended"


def test_a_paused_job_answers_at_once_but_holds_wait(tmp_path):
    r = make(tmp_path, clock=Clock(), cmd_wait_s=10.0)
    register(r)

    async def go():
        _, jid = await started(r)
        r.event({"device_id": DID, "job": jid, "kind": "takeover"})
        paused = await asyncio.wait_for(r.command(jid, "c-000001", "look", {}), timeout=0.05)
        waiting = asyncio.create_task(r.command(jid, "c-000002", "wait", {"s": 1}))
        await asyncio.sleep(FAST["tick"] * 2)
        r.event({"device_id": DID, "job": jid, "kind": "resume"})
        took = await r.next(DID)
        r.result({"device_id": DID, "job": jid, "cmd_id": "c-000002", "result": {"ok": True, "text": "done"}})
        return paused, took, await waiting

    paused, took, waiting = asyncio.run(go())
    assert paused["code"] == "paused" and "the job goes on when he taps Resume" in paused["text"]
    assert took["type"] == "cmd" and took["cmd"]["verb"] == "wait"
    assert waiting["ok"] and waiting["text"] == "done"


def test_a_command_queued_before_a_pause_answers_paused_at_once(tmp_path):
    # 2026-11-16 on the emulator: the agent's type was queued, the device
    # paused before taking it, and the agent heard [waiting] a minute later.
    r = make(tmp_path, clock=Clock(), cmd_wait_s=10.0)
    register(r)

    async def go():
        _, jid = await started(r)
        typing = asyncio.create_task(r.command(jid, "c-000001", "type", {"n": 1, "text": "98402"}))
        await asyncio.sleep(FAST["tick"] * 2)
        r.event({"device_id": DID, "job": jid, "kind": "takeover"})
        got = await asyncio.wait_for(typing, timeout=FAST["tick"] * 10)
        return got, jid

    got, jid = asyncio.run(go())
    assert got["code"] == "paused" and "taps Resume" in got["text"]
    assert jid not in r.pending, "a command the pause refused is never handed to the device"


def test_a_malformed_watch_never_takes_a_command(tmp_path):
    # Codex's review: any poll that says `running` is a watch, so a malformed
    # one is idle, and the command waiting for the device stays for its poll.
    r = make(tmp_path, poll_hold_s=0.02)
    register(r)

    async def go():
        _, jid = await started(r)
        command = asyncio.create_task(r.command(jid, "c-000001", "look", {}))
        await asyncio.sleep(0)
        bad = await r.next(DID, holds=jid, running="x")
        took = await r.next(DID, holds=jid)
        r.result({"device_id": DID, "job": jid, "cmd_id": "c-000001", "result": {"ok": True, "text": "a"}})
        await command
        return bad, took

    bad, took = asyncio.run(go())
    assert bad == {"type": "idle"}
    assert took["type"] == "cmd" and took["cmd"]["cmd_id"] == "c-000001"


def test_a_waiting_job_answers_at_once(tmp_path):
    r = make(tmp_path, clock=Clock(), cmd_wait_s=10.0)
    register(r)

    async def go():
        _, jid = await started(r)
        r.event({"device_id": DID, "job": jid, "kind": "locked"})
        locked = await asyncio.wait_for(r.command(jid, "c-000001", "tap", {"n": 1}), timeout=0.05)
        r.event({"device_id": DID, "job": jid, "kind": "call_wait"})
        calling = await asyncio.wait_for(r.command(jid, "c-000002", "tap", {"n": 1}), timeout=0.05)
        return locked, calling

    locked, calling = asyncio.run(go())
    assert locked["code"] == "waiting" and "until" in locked["text"]
    assert calling["code"] == "waiting" and "the device holder is in a call" in calling["text"]


def test_a_call_pause_says_so(tmp_path):
    r = make(tmp_path)
    register(r)
    _, jid = asyncio.run(started(r))
    r.event({"device_id": DID, "job": jid, "kind": "takeover", "why": "call"})
    calling = r.jobs[jid]["detail"]
    r.event({"device_id": DID, "job": jid, "kind": "resume"})
    r.event({"device_id": DID, "job": jid, "kind": "takeover"})
    touching = r.jobs[jid]["detail"]
    r.event({"device_id": DID, "job": jid, "kind": "resume"})
    r.event({"device_id": DID, "job": jid, "kind": "takeover", "why": "other"})
    other = r.jobs[jid]["detail"]
    assert calling == "paused: the device holder is in a call"
    assert touching == other == "paused: the device holder is using the phone"
    assert [row["why"] for row in rows(r, jid) if row.get("event") == "takeover"] == ["call", "touch", "touch"]


def test_a_pause_ends_after_15_minutes_and_is_never_a_stall(tmp_path):
    clock = Clock()
    r = make(tmp_path, clock=clock, stall_s=5.0, lock_wait_s=10.0, lost_s=100.0)
    register(r)
    _, jid = asyncio.run(started(r))
    r.event({"device_id": DID, "job": jid, "kind": "takeover"})
    clock.advance(6.0)
    r.tidy()
    mid = r.jobs[jid]["state"]
    clock.advance(5.0)
    r.tidy()
    job = r.jobs[jid]
    assert mid == "paused"
    assert job["outcome"] == "expired"
    assert job["detail"] == "the device holder did not hand the phone back in 15 minutes"


def test_a_poll_whose_device_hung_up_takes_nothing(tmp_path):
    r = make(tmp_path)
    register(r)

    def hung_up():
        return True

    async def go():
        t = asyncio.create_task(r.start("emulator", "open Settings", "agent-one"))
        await asyncio.sleep(0.02)
        dead_job = await r.next(DID, gone=hung_up)       # the app died mid-poll: not the job
        jid = (await r.next(DID))["job"]["id"]           # its next poll gets it
        r.event({"device_id": DID, "job": jid, "kind": "started"})
        await t
        c = asyncio.create_task(r.command(jid, "c-000001", "look", {}))
        await asyncio.sleep(0.02)
        dead_cmd = await r.next(DID, gone=hung_up)       # nor the command
        live = await r.next(DID)
        r.result({"device_id": DID, "job": jid, "cmd_id": "c-000001", "result": {"ok": True, "text": "a"}})
        out = await c
        r.end(jid)
        dead_stop = await r.next(DID, gone=hung_up)      # nor the stop
        stop = await r.next(DID)
        return dead_job, dead_cmd, live, out, dead_stop, stop
    dead_job, dead_cmd, live, out, dead_stop, stop = asyncio.run(go())
    assert dead_job == dead_cmd == dead_stop == {"type": "idle"}
    assert live["type"] == "cmd" and live["cmd"]["cmd_id"] == "c-000001" and out["text"] == "a"
    assert stop["type"] == "stop"


def test_a_starting_job_is_handed_out_again_only_when_its_poll_does_not_hold_it(tmp_path):
    r = make(tmp_path, poll_hold_s=0.01)
    register(r)

    async def go():
        start = asyncio.create_task(r.start("emulator", "open Settings", "agent-one"))
        first = await r.next(DID)
        jid = first["job"]["id"]
        held = await r.next(DID, holds=jid)
        again = await r.next(DID)
        r.event({"device_id": DID, "job": jid, "kind": "started"})
        return jid, first, held, again, await start
    jid, first, held, again, reply = asyncio.run(go())
    assert held == {"type": "idle"}
    assert again == first
    assert reply["ok"]
    assert any(row.get("kind") == "again" and row.get("what") == "job" for row in rows(r, jid))


def test_a_held_ended_job_is_told_to_stop_again_even_after_the_relay_forgot_it(tmp_path):
    r = make(tmp_path)
    register(r)

    async def go():
        _, jid = await started(r)
        r.end(jid)
        first = await r.next(DID)
        r.jobs.pop(jid)
        again = await r.next(DID, holds=jid)
        return jid, first, again
    jid, first, again = asyncio.run(go())
    assert first == again == {"type": "stop", "job": jid, "why": "done", "text": "agent-one ended the job"}


def test_an_unknown_held_job_is_told_to_stop(tmp_path):
    r = make(tmp_path)
    register(r)
    held = "20261116-063000-abcdef"
    got = asyncio.run(r.next(DID, holds=held))
    assert got == {"type": "stop", "job": held, "why": "unknown", "text": "agent-companion has no such job"}


def test_a_held_job_of_another_device_is_unknown_to_this_one(tmp_path):
    r = make(tmp_path)
    register(r)
    other = "emu-fedcba9876543210"

    async def go():
        _, jid = await started(r)
        register(r, did=other)
        return jid, await r.next(other, holds=jid)
    jid, got = asyncio.run(go())
    assert got == {"type": "stop", "job": jid, "why": "unknown", "text": "agent-companion has no such job"}
    assert r.jobs[jid]["state"] == "running"


def test_a_malformed_held_job_is_ignored(tmp_path):
    r = make(tmp_path)
    register(r)

    async def go():
        start = asyncio.create_task(r.start("emulator", "open Settings", "agent-one"))
        first = await r.next(DID)
        jid = first["job"]["id"]
        again = await r.next(DID, holds="../../etc")
        r.event({"device_id": DID, "job": jid, "kind": "started"})
        return first, again, await start
    first, again, reply = asyncio.run(go())
    assert again == first
    assert reply["ok"]


def test_a_poll_without_a_job_leaves_a_running_job_running(tmp_path):
    r = make(tmp_path, poll_hold_s=0.01)
    register(r)

    async def go():
        _, jid = await started(r)
        before = r.view(r.jobs[jid])
        got = await r.next(DID)
        return jid, before, got
    jid, before, got = asyncio.run(go())
    assert got == {"type": "idle"}
    assert r.view(r.jobs[jid]) == before


def test_a_result_nobody_asked_for_is_refused_and_kept_nowhere(tmp_path):
    r = make(tmp_path)
    register(r)
    _, jid = asyncio.run(started(r))
    got = r.result({"device_id": DID, "job": jid, "cmd_id": "c-zzzzzz", "result": {"ok": True, "text": "x" * 1000}})
    assert got["ok"] is False and got["code"] == "not_asked"
    assert (jid, "c-zzzzzz") not in r.results
    assert not (r.data / "jobs" / jid / "looks").exists()
    assert [x for x in rows(r, jid) if x["kind"] == "result"] == []


def test_a_prompt_the_device_never_let_go_lapses_and_the_stall_clock_runs_again(tmp_path):
    r = make(tmp_path, prompt_s=0.05, stall_s=0.1)
    register(r)

    async def go():
        _, jid = await started(r)
        r.event({"device_id": DID, "job": jid, "kind": "prompt", "cmd_id": "c-1x2y3z", "what": "Pay $12.40 in DoorDash"})
        await asyncio.sleep(0.08)
        r.tidy()
        mid = (r.jobs[jid]["state"], r.jobs[jid]["prompt"])
        await asyncio.sleep(0.15)
        r.tidy()
        return jid, mid
    jid, mid = asyncio.run(go())
    assert mid == ("running", None)
    lapsed = [x for x in rows(r, jid) if x.get("event") == "answer"]
    assert lapsed and lapsed[-1]["answer"] == "timeout" and lapsed[-1]["by"] == "relay"
    assert r.jobs[jid]["outcome"] == "stalled"


def test_a_turn_user_never_takes_ends_the_job(tmp_path):
    r = make(tmp_path, lock_wait_s=0.05, stall_s=0.01)
    register(r)

    async def go():
        _, jid = await started(r)
        r.event({"device_id": DID, "job": jid, "kind": "your_turn", "what": "the Wi-Fi password"})
        await asyncio.sleep(0.03)
        r.tidy()
        mid = r.jobs[jid]["state"]
        await asyncio.sleep(0.05)
        r.tidy()
        return jid, mid
    jid, mid = asyncio.run(go())
    assert mid == "running", "a turn is never a stall"
    assert r.jobs[jid]["outcome"] == "expired" and "his turn" in r.jobs[jid]["detail"]


def test_an_ended_job_is_forgotten_even_if_the_device_never_heard(tmp_path):
    r = make(tmp_path, forget_s=0.05)
    register(r)
    _, jid = asyncio.run(started(r))
    r.end(jid)
    assert r.jobs[jid]["told"] is False
    asyncio.run(asyncio.sleep(0.08))
    r.tidy()
    assert jid not in r.jobs
    assert r.detail(jid)["outcome"] == "done", "it is still on disk"


@pytest.mark.parametrize("kind", ["emulator ", "Phone", "", None, 3])
def test_a_device_says_exactly_what_it_is(tmp_path, kind):
    r = make(tmp_path)
    register(r, did=PID, kind="phone")
    got = r.register({"device_id": DID, "kind": kind, "model": "x", "fcm": "t", "driver_on": True})
    assert got["ok"] is False and got["code"] == "bad_args"
    assert r.devices()["phone"] is None and DID not in r.devices()["devices"]


def test_commands_wait_while_the_phone_is_locked_and_say_so(tmp_path):
    r = make(tmp_path)
    register(r)

    async def go():
        t = asyncio.create_task(r.start("emulator", "x", "agent-one"))
        got = await r.next(DID)
        r.event({"device_id": DID, "job": got["job"]["id"], "kind": "locked"})
        return await t, await r.command(got["job"]["id"], "c-000001", "look", {})
    s, c = asyncio.run(go())
    assert s["ok"] and s["job"]["state"] == "waiting" and "locked" in s["text"]
    assert c["code"] == "waiting" and "unlock" in c["text"]


def test_a_stalled_job_ends_and_the_device_is_told_once(tmp_path):
    r = make(tmp_path, stall_s=0.1)
    register(r)

    async def go():
        _, jid = await started(r)
        await asyncio.sleep(0.15)
        return jid, await r.next(DID), await r.next(DID)
    jid, stop, idle = asyncio.run(go())
    assert stop == {"type": "stop", "job": jid, "why": "stalled", "text": r.jobs[jid]["detail"]}
    assert idle == {"type": "idle"}


def test_a_held_prompt_is_never_a_stall(tmp_path):
    r = make(tmp_path, stall_s=0.05)
    register(r)

    async def go():
        _, jid = await started(r)
        r.event({"device_id": DID, "job": jid, "kind": "prompt", "cmd_id": "c-1x2y3z", "what": "Pay $12.40 in DoorDash"})
        await asyncio.sleep(0.1)
        r.tidy()
        return jid
    jid = asyncio.run(go())
    assert r.jobs[jid]["state"] == "running"


def test_a_device_that_stops_asking_loses_the_job(tmp_path):
    r = make(tmp_path, lost_s=0.05)
    register(r)
    _, jid = asyncio.run(started(r))
    asyncio.run(asyncio.sleep(0.08))
    r.tidy()
    assert r.jobs[jid]["outcome"] == "lost"


def test_user_stops_it_from_the_card(tmp_path):
    r = make(tmp_path)
    register(r)

    async def go():
        _, jid = await started(r)
        r.stop(jid)
        return jid, await r.next(DID), await r.command(jid, "c-000009", "look", {})
    jid, stop, after = asyncio.run(go())
    assert stop["type"] == "stop" and stop["why"] == "stopped"
    assert r.jobs[jid]["by"] == "card"
    assert after["code"] == "ended"


def test_the_banner_stop_ends_it_and_says_who(tmp_path):
    r = make(tmp_path)
    register(r)

    async def go():
        _, jid = await started(r)
        r.event({"device_id": DID, "job": jid, "kind": "stop", "by": "banner"})
        return jid, await r.next(DID)
    jid, nxt = asyncio.run(go())
    job = r.jobs[jid]
    assert job["outcome"] == "stopped" and job["by"] == "banner" and "banner" in job["detail"]
    assert nxt == {"type": "idle"}          # the device stopped it: nothing to tell


def test_prompts_and_his_answers_are_logged(tmp_path):
    r = make(tmp_path)
    register(r)

    async def go():
        _, jid = await started(r)
        r.event({"device_id": DID, "job": jid, "kind": "prompt", "cmd_id": "c-000001",
                 "what": "Send to recipient: On my way"})
        held = dict(r.jobs[jid]["prompt"])
        r.event({"device_id": DID, "job": jid, "kind": "answer", "cmd_id": "c-000001", "answer": "deny"})
        return jid, held
    jid, held = asyncio.run(go())
    assert held["what"] == "Send to recipient: On my way"
    assert r.jobs[jid]["prompt"] is None
    ans = [x for x in rows(r, jid) if x.get("event") == "answer"][0]
    assert ans["answer"] == "deny" and ans["what"] == "Send to recipient: On my way"


def test_a_screenshot_is_kept_private_and_a_bad_one_refused(tmp_path):
    r = make(tmp_path)
    register(r)

    async def go():
        _, jid = await started(r)
        out = []
        for cmd_id, png in (("c-000001", base64.b64encode(PNG).decode()), ("c-000002", "bm90IGEgcG5n")):
            t = asyncio.create_task(r.command(jid, cmd_id, "shot", {}))
            await r.next(DID)
            r.result({"device_id": DID, "job": jid, "cmd_id": cmd_id, "result": {"ok": True, "text": "", "png": png}})
            out.append(await t)
        return jid, out
    jid, (good, bad) = asyncio.run(go())
    p = r.data / "jobs" / jid / "shots" / "c-000001.png"
    assert good["ok"] and good["shot"] == str(p) and p.read_bytes() == PNG
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert not bad["ok"] and "screenshot" in bad["text"]
    assert r.shot_path(jid) == p


def test_the_folders_are_his_alone(tmp_path):
    r = make(tmp_path)
    register(r)
    _, jid = asyncio.run(started(r))
    assert stat.S_IMODE(r.data.stat().st_mode) == 0o700
    assert stat.S_IMODE((r.data / "jobs" / jid).stat().st_mode) == 0o700
    for f in ("job.json", "actions.jsonl"):
        assert stat.S_IMODE((r.data / "jobs" / jid / f).stat().st_mode) == 0o600
    assert stat.S_IMODE((r.data / "devices.json").stat().st_mode) == 0o600


def test_a_restart_keeps_live_jobs_and_answers_a_resent_command(tmp_path):
    r = make(tmp_path)
    register(r)

    async def go():
        _, jid = await started(r)
        t = asyncio.create_task(r.command(jid, "c-000001", "look", {}))
        await r.next(DID)
        r.result({"device_id": DID, "job": jid, "cmd_id": "c-000001", "result": {"ok": True, "text": "Home screen"}})
        await t
        return jid
    jid = asyncio.run(go())
    r2 = make(tmp_path)
    assert r2.jobs[jid]["state"] == "running"
    again = asyncio.run(r2.command(jid, "c-000001", "look", {}))
    assert again["ok"] and again["text"] == "Home screen"


def test_a_new_phone_waits_for_users_approval_and_never_takes_over(tmp_path):
    r = make(tmp_path)
    old = register(r, did=PID, kind="phone", model="first-device")
    assert old["role"] == "pending"
    assert r.approve_device(PID)["ok"] is True
    new_id = "phone-fedcba9876543210"
    new = register(r, did=new_id, kind="phone", model="second-device")
    assert new["role"] == "pending" and new["device_secret"]
    assert r.devices()["phone"] == PID
    assert r.devices()["devices"][new_id]["approved"] is False
    assert r.notes == [
        "A new phone wants to drive: first-device. Approve it in agent-companion's phone section, or forget it.",
        "Phone driver: first-device approved",
        "A new phone wants to drive: second-device. Approve it in agent-companion's phone section, or forget it.",
    ]
    again = r.register({"device_id": new_id, "kind": "phone", "model": "second-device", "android": "15",
                        "fcm": "tok-phone", "driver_on": True, "app_version": "1.1700"},
                       secret=new["device_secret"])
    assert again["role"] == "pending" and r.notes[-1].startswith("A new phone wants")
    assert r.register({"device_id": "x/../y", "kind": "phone"})["code"] == "bad_args"


def test_a_pending_phone_receives_no_job_until_approved(tmp_path):
    r = make(tmp_path)
    paired = register(r, did=PID, kind="phone", model="test-phone")
    assert asyncio.run(r.next(PID)) == {"type": "idle"}
    (tmp_path / "agent.key").write_text("k" * 40)
    assert asyncio.run(r.start("phone", "read the battery", "phone-agent", key="k" * 40))["code"] == "no_device"
    assert r.approve_device(PID) == {"ok": True, "text": "Phone driver: test-phone approved"}
    reply, _ = asyncio.run(started(r, device="phone", did=PID, key="k" * 40))
    assert reply["ok"] is True and paired["role"] == "pending"


def test_approval_refuses_an_approved_or_unknown_device_and_emulator_needs_none(tmp_path):
    r = make(tmp_path)
    emulator = register(r)
    assert emulator["role"] == "emulator" and r.devices()["devices"][DID]["approved"] is True
    as_phone = r.register({"device_id": DID, "kind": "phone", "model": "test-emulator", "android": "15",
                           "fcm": "tok", "driver_on": True, "app_version": "1.1700-emu"},
                          secret=emulator["device_secret"])
    assert as_phone["role"] == "pending" and r.devices()["phone"] is None
    phone = register(r, did=PID, kind="phone")
    assert r.approve_device(PID)["ok"] is True
    assert r.approve_device(PID)["code"] == "not_pending"
    assert r.approve_device("phone-fedcba9876543210")["code"] == "no_device"
    assert phone["role"] == "pending"


def test_legacy_devices_are_grandfathered_and_listing_shows_pending(tmp_path):
    r = make(tmp_path)
    pending_id = "phone-fedcba9876543210"
    P._write_json(r.data / "devices.json", {
        "devices": {
            PID: {"kind": "phone", "model": "test-phone", "android": "15", "app_version": "1.1700",
                  "registered": 10, "driver_on": True},
            pending_id: {"kind": "phone", "model": "pending-phone", "android": "16", "app_version": "1.1800",
                         "registered": 20, "driver_on": True},
            DID: {"kind": "emulator", "model": "test-emulator", "android": "15", "app_version": "1.1700-emu",
                  "registered": 30, "driver_on": True},
        },
        "phone": PID,
        "emulator": DID,
    })
    got = r.listing()
    assert got["devices"]["phone"]["model"] == "test-phone"
    assert got["devices"]["emulator"]["model"] == "test-emulator"
    assert got["pending"] == [{"device_id": pending_id, "model": "pending-phone", "android": "16",
                               "app_version": "1.1800", "registered": 20}]


def test_an_unknown_device_is_told_to_register(tmp_path):
    r = make(tmp_path)
    assert asyncio.run(r.next("nobody-0123456789abc")) == {"type": "unknown"}
    got = r.result({"device_id": "nobody-0123456789abc", "job": "x", "cmd_id": "c-000001", "result": {}})
    assert got["code"] == "not_yours"


def test_the_sweep_takes_screens_after_7_days_and_keeps_the_log(tmp_path):
    r = make(tmp_path)
    register(r)

    async def go():
        _, jid = await started(r)
        t = asyncio.create_task(r.command(jid, "c-000001", "shot", {}))
        await r.next(DID)
        r.result({"device_id": DID, "job": jid, "cmd_id": "c-000001",
                  "result": {"ok": True, "text": "Home", "png": base64.b64encode(PNG).decode()}})
        await t
        r.end(jid)
        return jid
    jid = asyncio.run(go())
    ended = r.jobs[jid]["ended"]
    d = r.data / "jobs" / jid
    assert P.sweep(r.data, now=ended + 6 * 86400) == 0
    assert P.sweep(r.data, now=ended + 8 * 86400) == 2
    assert not (d / "looks").exists() and not (d / "shots").exists()
    assert (d / "job.json").exists() and (d / "actions.jsonl").exists()


def test_the_listing_shows_live_history_and_devices_never_push_tokens(tmp_path):
    r = make(tmp_path)
    register(r)
    _, jid = asyncio.run(started(r))
    got = r.listing()
    assert [j["id"] for j in got["live"]] == [jid]
    assert got["devices"]["emulator"]["driver_on"] is True
    assert "tok-" not in json.dumps(got)
    r.end(jid)
    got = r.listing()
    assert got["live"] == [] and got["history"][0]["outcome"] == "done"
    assert r.detail(jid)["actions"][0]["kind"] == "start"
    assert r.detail("../../etc") is None


@pytest.mark.parametrize("verb,args", [
    ("tap", {}), ("tap", {"n": True}), ("tap", {"x": 5}), ("type", {"n": 1}), ("scroll", {"dir": "sideways"}),
    ("scroll", {"n": -1, "dir": "up"}), ("swipe", {"x1": 1, "y1": 2, "x2": 3}), ("swipe", {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "ms": 9000}),
    ("drag", {"n": 1}), ("move", {}), ("move", {"n": True}), ("drag", {"x1": 1, "y1": 2, "x2": 3}), ("drag", {"to": 4}), ("drag", {"n": 1, "to": 2, "ms": 100}),
    ("press", {"key": "power"}), ("open", {"app": ""}), ("wait", {"s": 99}), ("wait", {"text": ""}), ("fly", {})])
def test_bad_arguments_are_refused_in_words(verb, args):
    assert P.args_error(verb, args)


@pytest.mark.parametrize("verb,args", [
    ("look", {}), ("tap", {"n": 3}), ("long", {"x": 10, "y": 20}), ("type", {"n": 2, "text": "hello"}),
    ("scroll", {"dir": "down"}), ("scroll", {"n": 5, "dir": "up"}), ("swipe", {"x1": 1, "y1": 2, "x2": 3, "y2": 4}),
    ("drag", {"n": 0, "to": 3}), ("move", {"n": 5}), ("drag", {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "ms": 1200}), ("drag", {"n": 5, "x2": 3, "y2": 4}),
    ("press", {"key": "back"}), ("open", {"app": "Settings"}), ("wait", {"s": 5, "text": "Wi-Fi"}), ("shot", {})])
def test_good_arguments_pass(verb, args):
    assert P.args_error(verb, args) is None


class _Session:
    def __init__(self, status=200):
        self.sent, self.status = [], status

    def post(self, url, json=None, timeout=None):
        self.sent.append(json)
        return type("R", (), {"status_code": self.status})()


def test_the_drive_push_carries_its_kind_and_job_and_nothing_else(monkeypatch):
    from agent_companion import fcm
    s = _Session()
    monkeypatch.setattr(fcm, "_authed", lambda: (s, "project"))
    assert fcm.send_drive("tok", "20261116-063000-abcdef") == "ok"
    msg = s.sent[0]["message"]
    assert msg["token"] == "tok"
    assert msg["data"] == {"kind": "drive", "job": "20261116-063000-abcdef"}
    assert msg["android"] == {"priority": "HIGH", "ttl": "60s"}
    assert fcm.send_drive("", "x") == "no token" and len(s.sent) == 1


def test_a_missed_drive_push_touches_none_of_comms_state(monkeypatch):
    from agent_companion import fcm
    monkeypatch.setattr(fcm, "_authed", lambda: (_Session(status=404), "project"))
    assert fcm.send_drive("tok", "j") == "gone"



def test_approving_a_phone_revokes_every_other_phone(tmp_path):
    r = make(tmp_path)
    a, b = "phone-aaaaaaaaaaaaaaaa", "phone-bbbbbbbbbbbbbbbb"
    sa = r.register({"device_id": a, "kind": "phone", "model": "Old"})["device_secret"]
    assert r.approve_device(a)["ok"]
    sb = r.register({"device_id": b, "kind": "phone", "model": "New"})["device_secret"]
    assert r.approve_device(b)["ok"]
    back = r.register({"device_id": a, "kind": "phone", "model": "Old"}, secret=sa)
    assert back["role"] == "pending" and r.devices()["phone"] == b
