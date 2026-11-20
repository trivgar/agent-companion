"""Shared CLI/MCP phone-job helpers."""
from agent_companion.phone import emu, job


def test_targets_accept_only_a_look_number_or_a_point():
    assert job.point("120,640") == (120, 640)
    assert job.point("120,") is None
    assert job.target("12") == {"n": 12}
    assert job.target("120,640") == {"x": 120, "y": 640}


def test_result_words_include_a_safe_refusal_code():
    assert job.result_text({"ok": False, "code": "held", "text": "waiting"}) == "waiting [held]"
    assert job.result_text({"ok": True, "text": "look", "shot": "/safe/path"}) == "look\nscreenshot: /safe/path"


def test_lease_proxy_uses_a_host_backend_and_can_restore_the_file_lease(tmp_path, monkeypatch):
    class Lease:
        def __init__(self):
            self.calls = []

        def take(self, device, holder, seconds, *, why="", session=""):
            self.calls.append(("take", device, holder, seconds, why, session))
            return {"ok": True, "new": True}

        def live(self, device):
            self.calls.append(("live", device))
            return {"device": device, "holder": "Sam"}

        def release(self, device, holder, *, session=""):
            self.calls.append(("release", device, holder, session))
            return {"ok": True}

    fake = Lease()
    job.set_lease_backend(fake)
    try:
        assert job.leases.take("emulator", "Sam", 60, why="test", session="session") == {"ok": True, "new": True}
        assert emu.leases.live("emulator") == {"device": "emulator", "holder": "Sam"}
        assert emu.leases.release("emulator", "Sam", session="session") == {"ok": True}
        assert fake.calls == [
            ("take", "emulator", "Sam", 60, "test", "session"),
            ("live", "emulator"),
            ("release", "emulator", "Sam", "session"),
        ]
    finally:
        job.set_lease_backend(None)

    monkeypatch.setattr(job.drive, "DATA", tmp_path)
    restored = job.leases.take("emulator", "Sam", 60, why="test", session="session")
    assert restored["ok"] is True
    assert job.leases.live("emulator")["holder"] == "Sam"
