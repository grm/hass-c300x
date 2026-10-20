"""Repairs: certificate about to expire → sign in again."""
from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant


class CertificateRepairFlow(ConfirmRepairFlow):
    """On confirmation, start the reauthentication, which requests a new certificate."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_confirm(self, user_input: dict[str, str] | None = None):
        if user_input is not None and (entry := self.hass.config_entries.async_get_entry(self._entry_id)):
            entry.async_start_reauth(self.hass)
        return await super().async_step_confirm(user_input)


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None) -> RepairsFlow:
    return CertificateRepairFlow(data["entry_id"])
