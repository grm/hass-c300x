"""Binary sensors: door station ringing, call in progress."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from . import DoorEntryConfigEntry
from .const import RING_TIMEOUT
from .entity import DoorEntryEntity, MonitorEntity
from .openwebnet import DoorEvent


async def async_setup_entry(
    hass: HomeAssistant, entry: DoorEntryConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([RingingSensor(entry), CallSensor(entry)])


class RingingSensor(MonitorEntity, BinarySensorEntity):
    """On from the ring frame until the end-of-call frame (≈ 29 s when nobody answers).

    While it is on, the door station refuses our calls (486 Busy): no video, no talking.
    """

    _attr_translation_key = "ringing"
    _attr_is_on = False

    def __init__(self, entry: DoorEntryConfigEntry) -> None:
        super().__init__(entry, "ringing")
        self._timeout: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.gateway.monitor.add_listener(self._on_event))
        self.async_on_remove(self._cancel_timeout)

    @callback
    def _cancel_timeout(self) -> None:
        if self._timeout:
            self._timeout()
            self._timeout = None

    @callback
    def _on_event(self, event: DoorEvent) -> None:
        if event.kind == "ring":
            self._cancel_timeout()
            self._timeout = async_call_later(self.hass, RING_TIMEOUT, self._expire)
            self._attr_is_on = True
            self.async_write_ha_state()
        elif event.kind == "ring_end" and self._attr_is_on:
            self._expire()

    @callback
    def _expire(self, _now=None) -> None:
        self._cancel_timeout()
        self._attr_is_on = False
        self.async_write_ha_state()


class CallSensor(DoorEntryEntity, BinarySensorEntity):
    """Our own call to the door station (live view, snapshot, speaker)."""

    _attr_translation_key = "call"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: DoorEntryConfigEntry) -> None:
        super().__init__(entry, "call")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.gateway.add_call_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        return self.gateway.in_call
