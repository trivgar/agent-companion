"""The relay's client (app/phoneclient.py): its headers, its words when
agent-companion is away, and one retry with the same cmd_id."""
import io
import json
import urllib.error

from agent_companion.phone import client as PC


class Opener:
    def __init__(self, replies):
        self.replies = list(replies)
        self.reqs = []

    def __call__(self, req, timeout=None):
        self.reqs.append(req)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return io.BytesIO(json.dumps(r).encode())


def test_it_sends_the_token_and_json_and_a_key_only_when_given():
    o = Opener([{"ok": True}])
    PC.Client(base="http://x", token="tok", opener=o).start("emulator", "open Settings", "agent-one")
    req = o.reqs[0]
    assert req.full_url == "http://x/api/phone/jobs" and req.get_method() == "POST"
    assert req.get_header("X-agent-companion-token") == "tok"
    assert req.get_header("Content-type") == "application/json"
    assert req.get_header("X-phone-agent-key") is None
    assert json.loads(req.data) == {"device": "emulator", "task": "open Settings", "agent": "agent-one"}
    o2 = Opener([{"ok": True}])
    PC.Client(base="http://x", token="tok", key="k" * 40, opener=o2).end("j")
    assert o2.reqs[0].get_header("X-phone-agent-key") == "k" * 40


def test_it_sends_the_token_under_the_configured_header(monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_PHONE_TOKEN_HEADER", "  x-homebase-token  ")
    o = Opener([{"ok": True}])
    PC.Client(base="http://x", token="tok", opener=o).jobs()
    assert o.reqs[0].get_header("X-homebase-token") == "tok"


def test_a_command_that_loses_the_server_is_asked_once_more_with_the_same_id():
    o = Opener([urllib.error.URLError("refused"), {"ok": True, "text": "Home"}])
    slept = []
    got = PC.Client(base="http://x", token="t", opener=o, wait=slept.append).command("j1", "look", {})
    assert got["text"] == "Home" and slept == [5]
    ids = [json.loads(r.data)["cmd_id"] for r in o.reqs]
    assert ids[0] == ids[1] and ids[0].startswith("c-")


def test_server_away_twice_says_look_again():
    o = Opener([urllib.error.URLError("refused"), urllib.error.URLError("refused")])
    got = PC.Client(base="http://x", token="t", opener=o, wait=lambda s: None).command("j1", "tap", {"n": 1})
    assert got["code"] == "unreachable" and "look again" in got["text"]


def test_an_http_error_comes_back_in_words():
    err = urllib.error.HTTPError("http://x", 401, "Unauthorized", {},
                                 io.BytesIO(b'{"detail": "missing or invalid token"}'))
    got = PC.Client(base="http://x", token="t", opener=Opener([err])).jobs()
    assert got == {"ok": False, "code": "http_401", "text": "missing or invalid token"}


def test_live_job_picks_the_devices_job():
    o = Opener([{"live": [{"id": "a", "device": "phone"}, {"id": "b", "device": "emulator"}]}])
    assert PC.Client(base="http://x", token="t", opener=o).live_job("emulator")["id"] == "b"


def test_card_stop_names_the_clients_base_as_its_origin():
    o = Opener([{"ok": True, "text": "stopped"}])
    got = PC.Client(base="http://x:8770/", token="t", opener=o).card_stop("j1")
    req = o.reqs[0]
    assert got["ok"] is True
    assert req.full_url == "http://x:8770/api/phone/jobs/j1/stop"
    assert req.get_method() == "POST"
    assert req.get_header("Origin") == "http://x:8770"


def test_a_started_jobs_capability_is_sent_for_its_command_and_end(tmp_path):
    o = Opener([
        {"ok": True, "job": {"id": "job-1"}, "job_cap": "this-jobs-cap"},
        {"ok": True, "text": "Home"},
        {"ok": True, "text": "ended"},
    ])
    c = PC.Client(base="http://x", token="t", opener=o, cap_path=tmp_path / "caps.json")
    c.start("emulator", "open Settings", "agent-one")
    c.command("job-1", "look", {})
    c.end("job-1")
    assert o.reqs[1].get_header("X-phone-job-cap") == "this-jobs-cap"
    assert o.reqs[2].get_header("X-phone-job-cap") == "this-jobs-cap"
    assert not (tmp_path / "caps.json").read_text().count("this-jobs-cap")


def test_a_cached_capability_survives_the_next_cli_process(tmp_path):
    path = tmp_path / "caps.json"
    first = PC.Client(base="http://x", token="t", opener=Opener([
        {"ok": True, "job": {"id": "job-1"}, "job_cap": "this-jobs-cap"},
    ]), cap_path=path)
    first.start("emulator", "open Settings", "agent-one")
    o = Opener([{"ok": True}])
    PC.Client(base="http://x", token="t", opener=o, cap_path=path).command("job-1", "look", {})
    assert o.reqs[0].get_header("X-phone-job-cap") == "this-jobs-cap"
