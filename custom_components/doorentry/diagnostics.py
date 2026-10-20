"""Diagnostics, without the secrets."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import DoorEntryConfigEntry
from .const import CONF_CA, CONF_CERT, CONF_KEY, CONF_OPEN_PASSWORD, CONF_SIP_ACCOUNT

TO_REDACT = {CONF_EMAIL, CONF_PASSWORD, CONF_OPEN_PASSWORD, CONF_SIP_ACCOUNT, CONF_CERT, CONF_KEY, CONF_CA}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: DoorEntryConfigEntry) -> dict[str, Any]:
    gateway = entry.runtime_data
    return {
        "data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "monitor_connected": gateway.monitor.connected,
        "in_call": gateway.in_call,
        "has_image": gateway.current_image() is not None,
    }
