"""Stuck asks become one loud alert."""
import time
from datetime import datetime

from agent_companion import ring

NOW = datetime(2026, 10, 28, 15, 0).timestamp()


def ask(i, age, **kw):
    return {"id": f"a{i:015x}", "at": NOW - age, "speaker": "Juno", "project": "site",
            "session_id": "s1", "tool": "Bash", "what": "git push origin feat/x", "status": "pending", **kw}


def empty():
    return {"rung": {}, "last_at": 0.0, "days": {}}


def test_29_minutes_is_not_stuck_31_is():
    assert ring.decide([ask(1, 29 * 60)], empty(), NOW, asleep=False) == []
    assert [a["id"] for a in ring.decide([ask(1, 31 * 60)], empty(), NOW, asleep=False)] == [ask(1, 0)["id"]]


def test_asleep_holds_everything_and_it_alerts_on_waking():
    a = ask(1, 3 * 3600)
    assert ring.decide([a], empty(), NOW, asleep=True) == []
    assert ring.decide([a], empty(), NOW, asleep=False) == [a]


def test_each_ask_alerts_once_ever():
    a = ask(1, 3600)
    s = ring.mark(empty(), [a], NOW - 3600)
    assert ring.decide([a], s, NOW, asleep=False) == []


def test_all_ripe_asks_go_in_one_pass_oldest_first():
    got = ring.decide([ask(1, 3600), ask(2, 2000), ask(3, 60)], empty(), NOW, asleep=False)
    assert [a["id"] for a in got] == [ask(1, 0)["id"], ask(2, 0)["id"]]


def test_ten_minutes_between_passes():
    s = ring.mark(empty(), [ask(1, 3600)], NOW - 300)
    assert ring.decide([ask(2, 3600)], s, NOW, asleep=False) == []
    assert ring.decide([ask(2, 3600)], s, NOW + 301, asleep=False) != []


def test_eight_passes_a_day():
    s = empty()
    for i in range(8):
        s = ring.mark(s, [ask(i, 3600)], NOW - 7200 + i * 700)
    assert ring.decide([ask(99, 3600)], s, NOW + 700, asleep=False) == []


def test_private_asks_never_alert():
    assert ring.decide([ask(1, 3600, project="ledger")], empty(), NOW, asleep=False,
                       private=lambda a: a["project"] == "ledger") == []


def test_mark_prunes_old_entries():
    s = ring.mark(empty(), [ask(1, 0)], NOW - 3 * 86400)
    s = ring.mark(s, [ask(2, 0)], NOW)
    assert list(s["rung"]) == [ask(2, 0)["id"]]
    assert len(s["days"]) == 2 and all(isinstance(v, int) for v in s["days"].values())


def test_state_round_trips_and_a_bad_file_is_empty(tmp_path):
    p = tmp_path / "ring.json"
    ring.save(ring.mark(empty(), [ask(1, 0)], NOW), p)
    assert ring.load(p)["rung"] == {ask(1, 0)["id"]: NOW}
    assert (p.stat().st_mode & 0o777) == 0o600
    p.write_text("not json")
    assert ring.load(p) == empty()


def test_message_for_a_permission_and_a_question():
    t, b = ring.message(ask(1, 47 * 60), "Juno", NOW)
    assert t == "Juno stuck 47 min" and b == "Needs your OK. Bash: git push origin feat/x"
    q = ask(2, 31 * 60, tool="AskUserQuestion", what="Which site?",
            questions=[{"question": "Which site?", "header": "", "multiSelect": False,
                        "options": [{"label": "site"}, {"label": "ledger"}]}])
    t, b = ring.message(q, "Juno", NOW)
    assert t == "Juno stuck 31 min" and b == "Which site?\nsite · ledger"
    assert "—" not in t + b


def test_buttons_for_a_permission_and_a_question():
    assert [x["action"] for x in ring.actions(ask(1, 0))] == ["allow", "deny", "call"]
    q = ask(2, 0, tool="AskUserQuestion", questions=[{"question": "Q?", "header": "", "multiSelect": False, "options": []}])
    assert [x["action"] for x in ring.actions(q)] == ["answer", "call"]


def test_tick_sends_each_ripe_ask_once_with_buttons_and_records_it(tmp_path):
    sent = []
    send = lambda title, body, url, tag, actions: sent.append((title, body, url, tag, actions)) or True
    p = tmp_path / "ring.json"
    a = ask(1, 40 * 60)
    assert ring.tick([a], [], False, send, state_path=p, now=NOW) == 1
    title, body, url, tag, acts = sent[0]
    assert title == "Juno stuck 40 min" and tag == "stuck-Juno"
    assert url.startswith(f"/?agent=Juno&ask={a['id']}&n=") and len(url.split("&n=")[1]) >= 16
    assert [x["action"] for x in acts] == ["allow", "deny", "call"]
    assert ring.tick([a], [], False, send, state_path=p, now=NOW + 3600) == 0
    assert len(sent) == 1


def test_tick_skips_while_asleep_and_on_a_failed_send_tries_again(tmp_path):
    p = tmp_path / "ring.json"
    a = ask(1, 40 * 60)
    assert ring.tick([a], [], True, lambda *x: True, state_path=p, now=NOW) == 0
    assert ring.tick([a], [], False, lambda *x: False, state_path=p, now=NOW) == 0
    assert ring.load(p)["rung"] == {}, "a send that went nowhere is not an alert"


def test_a_helpers_ask_names_its_agent():
    a = ask(1, 0, speaker="Vance")
    rows = [{"speaker": "Juno", "pid": 5, "session_id": "root"},
            {"speaker": "Vance", "pid": 6, "root_pid": 5, "session_id": "s1", "kind": "helper"}]
    who, url, tag = ring.who_and_tap(a, rows)
    assert url.startswith("/?agent=") and tag.startswith("stuck-")


def test_ledger_never_alerts(tmp_path, monkeypatch):
    sent = []
    a = ask(1, 3600, project="ledger")
    checked = []
    monkeypatch.setattr(ring.addons, "private", lambda cwd, project: checked.append((cwd, project)) or project == "ledger")
    assert ring.tick([a], [{"session_id": "s1", "cwd": "/site"}], False,
                     lambda *x: sent.append(x) or True, state_path=tmp_path / "r.json", now=NOW) == 0
    assert sent == []
    assert checked == [("/site", "ledger")]


def test_the_watcher_never_starts_under_pytest():
    w = ring.Watcher(lambda: [], lambda: [], lambda: False, lambda *x: True)
    w.start()
    assert w.thread is None


def test_tick_uses_the_addon_sleep_state_when_none_is_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(ring.addons, "asleep", lambda: True)
    assert ring.tick([ask(1, 40 * 60)], [], None, lambda *x: True,
                     state_path=tmp_path / "ring.json", now=NOW) == 0


def test_a_buttons_key_works_once_and_a_guess_never(tmp_path):
    p = tmp_path / "ring.json"
    k = ring.issue("a1", NOW, p)
    assert not ring.consume("a1", "guess", NOW, p)
    assert not ring.consume("a2", k, NOW, p), "bound to its own ask"
    assert ring.consume("a1", k, NOW, p)
    assert not ring.consume("a1", k, NOW, p), "once"
    assert not ring.consume("a1", "", NOW, p)


def test_an_old_key_is_refused(tmp_path):
    p = tmp_path / "ring.json"
    k = ring.issue("a1", NOW - 3 * 86400, p)
    assert not ring.consume("a1", k, NOW, p)


def _alert(n=1):
    return {"title": f"Juno stuck {30 + n} min", "body": "Needs your OK. Bash: ls",
            "url": f"/?agent=Juno&ask=a{n:015x}&n=key{n}", "tag": "stuck-Juno",
            "actions": [{"action": "allow", "title": "Allow"}]}


def test_a_ring_record_holds_its_alerts_and_an_answer_url(tmp_path):
    p = tmp_path / "ring.json"
    rid, key = ring.new_ring("Juno", [_alert()], NOW, p)
    b = ring.brief(rid, NOW + 5, p)
    assert b["who"] == "Juno" and b["title"] == "Juno stuck 31 min"
    assert b["answer_url"] == f"/?agent=Juno&act=answer&r={rid}&rk={key}"
    assert b["alerts"] == [_alert()]
    assert ring.brief(rid, NOW + ring.RING_KEEP_S + 1, p) is None, "an hour old is gone"
    assert ring.brief("nope", NOW, p) is None


def test_a_ring_is_answered_once_with_its_own_key(tmp_path):
    p = tmp_path / "ring.json"
    rid, key = ring.new_ring("Juno", [_alert()], NOW, p)
    assert ring.answer(rid, "guess", NOW, p) is None
    assert ring.answer(rid, key, NOW, p) == "Juno"
    assert ring.answer(rid, key, NOW, p) is None


def test_tick_rings_when_the_phone_can_and_sends_no_alert(tmp_path):
    sent, rang = [], []
    p = tmp_path / "ring.json"
    a = ask(1, 40 * 60)
    n = ring.tick([a], [], False, lambda *x: sent.append(x) or True, state_path=p, now=NOW,
                  ring_send=lambda rid: rang.append(rid) or True)
    assert n == 1 and sent == [] and len(rang) == 1
    b = ring.brief(rang[0], NOW, p)
    assert b["alerts"][0]["tag"] == "stuck-Juno" and "&n=" in b["alerts"][0]["url"]
    assert ring.tick([a], [], False, lambda *x: True, state_path=p, now=NOW + 3600,
                     ring_send=lambda rid: True) == 0, "each ask once, rung or alerted"


def test_a_ring_that_cannot_go_falls_back_to_the_alerts(tmp_path):
    sent = []
    a = ask(1, 40 * 60)
    n = ring.tick([a], [], False, lambda *x: sent.append(x) or True, state_path=tmp_path / "r.json",
                  now=NOW, ring_send=lambda rid: False)
    assert n == 1 and len(sent) == 1 and sent[0][3] == "stuck-Juno"


def test_a_ring_keeps_its_asks_and_gives_them_once_after_it_was_answered(tmp_path):
    p = tmp_path / "ring.json"
    rid, key = ring.new_ring("Juno", [_alert()], NOW, p, ask_ids=["a1", "a2"])
    assert ring.claim(rid, NOW, p) is None, "not answered yet: no brief"
    assert ring.answer(rid, key, NOW, p) == "Juno"
    assert ring.claim(rid, NOW + 5, p) == ["a1", "a2"]
    assert ring.claim(rid, NOW + 6, p) is None, "one call gets the brief"


def test_an_answered_ring_can_be_read_before_its_call_claims_it(tmp_path):
    p = tmp_path / "ring.json"
    rid, key = ring.new_ring("Juno", [_alert()], NOW, p, ask_ids=["a1"])
    assert ring.answer(rid, key, NOW, p) == "Juno"
    assert ring.ready(rid, NOW, p) == ["a1"]
    assert ring.ready(rid, NOW + 1, p) == ["a1"], "a refused call leaves the brief"
    assert ring.claim(rid, NOW + 2, p) == ["a1"]


def test_an_old_ring_gives_nothing(tmp_path):
    p = tmp_path / "ring.json"
    rid, key = ring.new_ring("Juno", [_alert()], NOW, p, ask_ids=["a1"])
    ring.answer(rid, key, NOW, p)
    assert ring.claim(rid, NOW + ring.RING_KEEP_S + 1, p) is None
    assert ring.claim("nothex", NOW, p) is None


def test_tick_keeps_the_rung_asks_in_the_record(tmp_path):
    p, rang = tmp_path / "ring.json", []
    ring.tick([ask(1, 40 * 60), ask(2, 35 * 60)], [], False, lambda *x: True, state_path=p, now=NOW,
              ring_send=lambda rid: rang.append(rid) or True)
    e = ring._read_keys(ring._rings_path(p))[rang[0]]
    assert e["asks"] == [ask(1, 0)["id"], ask(2, 0)["id"]], "oldest first, as decide sorts"


def test_the_settings_set_the_timing(monkeypatch, tmp_path):
    f = tmp_path / "c.toml"
    f.write_text("ring_after_min = 5\nring_gap_min = 1\nring_day_max = 2\n")
    monkeypatch.setenv("AGENT_COMPANION_CONFIG", str(f))
    from agent_companion import ring
    assert ring.ring_after_s() == 300 and ring.gap_s() == 60 and ring.day_max() == 2


def test_a_claim_that_did_not_open_a_call_is_released_once(tmp_path):
    p = tmp_path / "ring.json"
    rid, key = ring.new_ring("Juno", [_alert()], NOW, p, ask_ids=["a1"])
    ring.answer(rid, key, NOW, p)
    assert ring.claim(rid, NOW + 1, p) == ["a1"]
    assert ring.claim(rid, NOW + 2, p) is None, "a second start at once gets nothing"
    ring.release(rid, p)
    assert ring.claim(rid, NOW + 3, p) == ["a1"]


def test_a_private_projects_worktree_never_rings_with_its_words(monkeypatch):
    from agent_companion import livecall
    monkeypatch.setattr(livecall, "_repo_name", lambda cwd: "ledger")
    monkeypatch.setattr(ring.addons, "private", lambda cwd, project: project == "ledger")
    rows = [{"session_id": "s1", "cwd": "/work/ledger-fix-branch"}]
    assert ring._private({"session_id": "s1", "project": "ledger-fix-branch"}, rows) is True


def _sender(sent, ok=True):
    return lambda *x: sent.append(x) or ok


def test_a_ring_nobody_fetched_sends_its_alerts_once_it_can_no_longer_ring(tmp_path):
    p, sent = tmp_path / "ring.json", []
    ring.new_ring("Juno", [_alert()], NOW, p, ask_ids=["a1"])
    pending = [{"id": "a1"}]
    assert ring.fall_back(pending, _sender(sent), NOW + ring.LAND_S - 1, p) == 0 and sent == []
    assert ring.fall_back(pending, _sender(sent), NOW + ring.LAND_S, p) == 1
    a = _alert()
    assert sent[0] == (a["title"], a["body"], a["url"], a["tag"], a["actions"])
    assert ring.fall_back(pending, _sender(sent), NOW + ring.LAND_S + 30, p) == 0
    assert len(sent) == 1


def test_a_fetched_ring_never_falls_back(tmp_path):
    p, sent = tmp_path / "ring.json", []
    rid, _ = ring.new_ring("Juno", [_alert()], NOW, p, ask_ids=["a1"])
    ring.fetched(rid, NOW + 5, p)
    assert ring.fall_back([{"id": "a1"}], _sender(sent), NOW + ring.LAND_S, p) == 0
    ring.fetched("nope", NOW, p)
    ring.fetched("", NOW, p)
    assert sent == []


def test_an_answered_ring_never_falls_back(tmp_path):
    p, sent = tmp_path / "ring.json", []
    rid, key = ring.new_ring("Juno", [_alert()], NOW, p, ask_ids=["a1"])
    assert ring.answer(rid, key, NOW, p) == "Juno"
    assert ring.fall_back([{"id": "a1"}], _sender(sent), NOW + ring.LAND_S, p) == 0
    assert sent == []


def test_an_ask_answered_meanwhile_gets_no_alert(tmp_path):
    p, sent = tmp_path / "ring.json", []
    ring.new_ring("Juno", [_alert(1), _alert(2)], NOW, p, ask_ids=["a1", "a2"])
    pending = [{"id": "a2"}]
    assert ring.fall_back(pending, _sender(sent), NOW + ring.LAND_S, p) == 1
    assert sent[0][2] == _alert(2)["url"]
    assert ring.fall_back(pending, _sender(sent), NOW + ring.LAND_S + 30, p) == 0
    assert len(sent) == 1


def test_an_ask_out_of_sight_for_a_pass_gets_its_alert_when_it_is_back(tmp_path):
    p, sent = tmp_path / "ring.json", []
    rid, _ = ring.new_ring("Juno", [_alert()], NOW, p, ask_ids=["a1"])
    assert ring.fall_back([], _sender(sent), NOW + ring.LAND_S, p) == 0 and sent == []
    record = ring._read_keys(ring._rings_path(p))[rid]
    assert "fell" not in record and "sent" not in record, "a pass with nothing to send writes nothing"
    assert ring.fall_back([{"id": "a1"}], _sender(sent), NOW + ring.LAND_S + 30, p) == 1
    assert ring.fall_back([{"id": "a1"}], _sender(sent), NOW + ring.LAND_S + 60, p) == 0 and len(sent) == 1


def test_an_alert_that_could_not_go_is_tried_again(tmp_path):
    p, sent = tmp_path / "ring.json", []
    ring.new_ring("Juno", [_alert()], NOW, p, ask_ids=["a1"])
    pending = [{"id": "a1"}]
    assert ring.fall_back(pending, _sender(sent, False), NOW + ring.LAND_S, p) == 0
    assert ring.fall_back(pending, _sender(sent, True), NOW + ring.LAND_S + 30, p) == 1


def test_an_hour_old_ring_is_left_alone(tmp_path):
    p, sent = tmp_path / "ring.json", []
    ring.new_ring("Juno", [_alert()], NOW, p, ask_ids=["a1"])
    assert ring.fall_back([{"id": "a1"}], _sender(sent), NOW + ring.RING_KEEP_S + 1, p) == 0
    assert sent == []


def test_a_rung_ask_nobody_fetched_ends_as_the_alert_tick_kept(tmp_path):
    p, sent = tmp_path / "ring.json", []
    a = ask(1, 40 * 60)
    assert ring.tick([a], [], False, _sender(sent), state_path=p, now=NOW, ring_send=lambda rid: True) == 1
    assert sent == []
    assert ring.fall_back([a], _sender(sent), NOW + ring.LAND_S, p) == 1
    assert sent[0][3] == "stuck-Juno" and "&n=" in sent[0][2]


def test_one_pass_falls_back_and_then_rings(tmp_path, monkeypatch):
    monkeypatch.setattr(ring, "STATE", tmp_path / "ring.json")
    sent, rang = [], []
    ring.new_ring("Juno", [_alert()], NOW - ring.LAND_S - 1, ask_ids=[ask(1, 60)["id"]])
    pending = [ask(1, 60), ask(2, 40 * 60)]
    w = ring.Watcher(lambda: pending, lambda: [], lambda: False, _sender(sent),
                     lambda rid: rang.append(rid) or True)
    assert w.once(NOW) == (1, 1)
    assert len(sent) == 1 and sent[0][2] == _alert()["url"]
    assert len(rang) == 1


def test_a_broken_fall_back_does_not_stop_the_ring(tmp_path, monkeypatch):
    monkeypatch.setattr(ring, "STATE", tmp_path / "ring.json")

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(ring, "fall_back", boom)
    rang = []
    w = ring.Watcher(lambda: [ask(1, 40 * 60)], lambda: [], lambda: False, lambda *x: True,
                     lambda rid: rang.append(rid) or True)
    assert w.once(NOW) == (0, 1)


def test_a_ring_whose_push_never_left_alerts_once(tmp_path):
    p, sent = tmp_path / "ring.json", []
    a = ask(1, 40 * 60)
    assert ring.tick([a], [], False, _sender(sent), state_path=p, now=NOW, ring_send=lambda rid: False) == 1
    assert len(sent) == 1
    assert ring.fall_back([a], _sender(sent), NOW + ring.LAND_S, p) == 0
    assert len(sent) == 1
