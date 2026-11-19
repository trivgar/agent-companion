"""The public phone CLI is generic and remains disabled without opt-in."""
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path


def load_cli():
    path = Path(__file__).parent.parent / "bin" / "phone"
    loader = importlib.machinery.SourceFileLoader("phone_cli_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_cli_refuses_when_the_phone_driver_is_off(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_COMPANION_PHONE_DRIVER", "false")
    assert load_cli().main(["status"]) == 1
    assert "phone driver is off" in capsys.readouterr().err


def test_cli_uses_the_emulator_helper_when_enabled(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_COMPANION_PHONE_DRIVER", "true")
    cli = load_cli()
    monkeypatch.setattr(cli.phoneemu, "status", lambda: {"running": False, "serial": "emulator-test"})
    assert cli.main(["--as", "agent", "emu", "status"]) == 0
    assert '"serial": "emulator-test"' in capsys.readouterr().out


def test_cli_command_arguments_match_the_relay_protocol():
    cli = load_cli()
    assert cli.command_of(type("Args", (), {"cmd": "tap", "target": "12"})()) == ("tap", {"n": 12})
    args = type("Args", (), {"cmd": "wait", "seconds": "5", "text": "ready"})()
    assert cli.command_of(args) == ("wait", {"s": 5.0, "text": "ready"})
