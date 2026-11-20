"""Phone-driver settings read from config each time they are used."""
import pytest

from agent_companion.phone import settings
from agent_companion.server.auth import HEADER_NAME


SETTINGS = (
    ("phone_app_name", settings.app_name, "agent-companion", "HomeBase"),
    ("phone_holder", settings.holder, "the device holder", "Sam"),
    ("phone_avd", settings.avd, "agent-companion-test", "host-test"),
    ("phone_token_header", settings.token_header, HEADER_NAME, "x-homebase-token"),
)


def configure(tmp_path, monkeypatch, text=""):
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setenv("AGENT_COMPANION_CONFIG", str(path))
    for key, *_ in SETTINGS:
        monkeypatch.delenv("AGENT_COMPANION_" + key.upper(), raising=False)
    return path


@pytest.mark.parametrize(("_key", "read", "default", "_override"), SETTINGS)
def test_phone_settings_have_the_documented_defaults(tmp_path, monkeypatch, _key, read, default, _override):
    configure(tmp_path, monkeypatch)
    assert read() == default


@pytest.mark.parametrize(("key", "read", "_default", "override"), SETTINGS)
def test_phone_settings_strip_config_values_at_call_time(tmp_path, monkeypatch, key, read, _default, override):
    path = configure(tmp_path, monkeypatch, f'{key} = "  {override}  "\n')
    assert read() == override
    path.write_text(f'{key} = "next-{override}"\n')
    assert read() == f"next-{override}"


@pytest.mark.parametrize(("key", "read", "default", "_override"), SETTINGS)
def test_empty_phone_settings_fall_back_to_their_defaults(tmp_path, monkeypatch, key, read, default, _override):
    configure(tmp_path, monkeypatch, f'{key} = "   "\n')
    assert read() == default


def test_holder_possessive_follows_the_current_holder(tmp_path, monkeypatch):
    path = configure(tmp_path, monkeypatch, 'phone_holder = "  Sam  "\n')
    assert settings.holder_possessive() == "Sam's"
    path.write_text('phone_holder = ""\n')
    assert settings.holder_possessive() == "the device holder's"
