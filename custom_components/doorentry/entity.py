"""Base entity: one device per gateway."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from . import DoorEntryConfigEntry
from .const import CONF_GATEWAY_ID, DOMAIN, MANUFACTURER, MODEL
from .gateway import DoorGateway


class DoorEntryEntity(Entity):
    """Entity attached to the gateway device."""

    _attr_has_entity_name = True

    def __init__(self, entry: DoorEntryConfigEntry, key: str) -> None:
        self.gateway: DoorGateway = entry.runtime_data
        gateway_id = entry.data[CONF_GATEWAY_ID]
        self._attr_unique_id = f"{gateway_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, gateway_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=entry.title,
            serial_number=gateway_id,
        )


class MonitorEntity(DoorEntryEntity):
    """Entity fed by the OpenWebNet monitor: unavailable while it is disconnected."""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.gateway.monitor.add_state_listener(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        return self.gateway.monitor.connected
