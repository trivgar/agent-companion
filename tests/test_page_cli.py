"""The generic page command uses the core's URL and hook token."""
from __future__ import annotations

import json
import runpy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class _Reply:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"page": {"id": "page-id"}}).encode()


def test_publish_uses_the_configured_server_and_core_token(tmp_path, monkeypatch):
    command = runpy.run_path(str(ROOT / "bin" / "page"), run_name="page_cli")
    token = tmp_path / "hook-token"
    token.write_text("token-value\n")
    source = tmp_path / "page.html"
    source.write_text("<h1>Hi</h1>")
    monkeypatch.setenv("AGENT_COMPANION_URL", "https://dashboard.example")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(command["auth"], "token_path", lambda: token)
    sent = []

    def opener(request, timeout):
        sent.append((request, timeout))
        return _Reply()

    ok, address = command["publish"](source, "Hello", False, opener=opener)
    request, timeout = sent[0]
    assert ok and address == "https://dashboard.example/?view=pages&page=page-id"
    assert request.full_url == "https://dashboard.example/api/pages" and timeout == command["TIMEOUT"]
    assert request.get_header("X-agent-companion-token") == "token-value"
    assert json.loads(request.data) == {
        "html": "<h1>Hi</h1>", "title": "Hello", "folder": str(tmp_path), "speaker": "an agent",
        "project": tmp_path.name, "source": str(source), "household": False,
    }
