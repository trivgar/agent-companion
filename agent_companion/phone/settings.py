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


def agent_name() -> str:
    value = str(config.get("phone_driver_agent") or "phone-agent").strip()
    return value[:40] or "phone-agent"
