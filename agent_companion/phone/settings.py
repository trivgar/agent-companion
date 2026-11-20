"""Configuration checks shared by the optional phone-driver surfaces."""
from __future__ import annotations

from .. import config


def _on(name: str) -> bool:
    value = config.get(name, False)
    return value is True or (isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"})


def enabled() -> bool:
    return _on("phone_driver")


def real_phone_enabled() -> bool:
    return _on("phone_driver_real_phone")


def _text(name: str, default: str) -> str:
    value = config.get(name, "")
    text = str(value).strip() if value is not None else ""
    return text or default


def app_name() -> str:
    return _text("phone_app_name", "agent-companion")


def holder() -> str:
    return _text("phone_holder", "the device holder")


def holder_possessive() -> str:
    return holder() + "'s"


def avd() -> str:
    return _text("phone_avd", "agent-companion-test")


def token_header() -> str:
    # The server imports phone routes, so defer this import until the setting is read.
    from agent_companion.server.auth import HEADER_NAME
    return _text("phone_token_header", HEADER_NAME)


def agent_name() -> str:
    value = str(config.get("phone_driver_agent") or "phone-agent").strip()
    return value[:40] or "phone-agent"
