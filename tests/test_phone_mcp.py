"""The generic phone-agent MCP server keeps its protocol and job capability."""
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path


def load_mcp():
    path = Path(__file__).parent.parent / "bin" / "phone_mcp.py"
    loader = importlib.machinery.SourceFileLoader("phone_mcp_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Lease:
    def take(self, *_args, **_kwargs):
        return {"ok": True, "new": True}

    def release(self, *_args, **_kwargs):
        return {"ok": True}


class Client:
    def __init__(self):
        self.calls = []

    def start(self, device, task, agent):
        self.calls.append(("start", device, task, agent))
        return {"ok": True, "text": "driving", "job": {"id": "job-123"}, "job_cap": "cap"}

    def command(self, job_id, verb, args, cap=None):
        self.calls.append(("command", job_id, verb, args, cap))
        return {"ok": True, "text": "done"}

    def end(self, job_id, cap=None):
        self.calls.append(("end", job_id, cap))
        return {"ok": True, "text": "ended"}

    def jobs(self):
        return {"ok": True, "live": [], "history": [], "devices": {}}


def tool(server, name, arguments=None):
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments or {}}})
    return reply["result"]


def test_mcp_lists_tools_and_sends_the_started_jobs_capability(tmp_path):
    mcp = load_mcp()
    client = Client()
    server = mcp.PhoneServer(client=client, lease_api=Lease(), key_file=tmp_path / "missing-key",
                             emulator_touch=lambda: None, emu_booted=lambda: True)
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert {item["name"] for item in listed} >= {"phone_start", "phone_look", "phone_end"}
    assert tool(server, "phone_start", {"task": "open settings"})["isError"] is False
    assert tool(server, "phone_look")["isError"] is False
    assert client.calls[-1] == ("command", "job-123", "look", {}, "cap")


def test_mcp_refuses_real_phone_start_without_the_second_opt_in(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_COMPANION_PHONE_DRIVER_REAL_PHONE", "false")
    mcp = load_mcp()
    server = mcp.PhoneServer(client=Client(), lease_api=Lease(), key_file=tmp_path / "key",
                             emulator_touch=lambda: None, emu_booted=lambda: True)
    result = tool(server, "phone_start", {"task": "read status", "device": "phone"})
    assert result["isError"] is False
    assert "real phone driver is off" in result["content"][0]["text"]
