"""Doorbell and door events from the OpenWebNet monitor."""
from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DoorEntryConfigEntry
from .entity import MonitorEntity
from .openwebnet import DoorEvent


async def async_setup_entry(
    hass: HomeAssistant, entry: DoorEntryConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([DoorbellEvent(entry), DoorOpenedEvent(entry)])


class _MonitorEvent(MonitorEntity, EventEntity):
    _kind = ""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.gateway.monitor.add_listener(self._on_event))

    @callback
    def _on_event(self, event: DoorEvent) -> None:
        if event.kind == self._kind:
            self._trigger_event(self.event_types[0], {"address": event.address})
            self.async_write_ha_state()


class DoorbellEvent(_MonitorEvent):
    """*8*1#1#4#<address>*10## : someone rings at the door station."""

    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = ["ring"]
    _attr_translation_key = "doorbell"
    _kind = "ring"

    def __init__(self, entry: DoorEntryConfigEntry) -> None:
        super().__init__(entry, "doorbell")


class DoorOpenedEvent(_MonitorEvent):
    """*8*19*<where>## : the door was opened (app, indoor unit or Home Assistant)."""

    _attr_event_types = ["opened"]
    _attr_translation_key = "door"
    _kind = "door_open"

    def __init__(self, entry: DoorEntryConfigEntry) -> None:
        super().__init__(entry, "door_opened")
