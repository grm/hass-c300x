"""Buttons: door locks, stair light, camera refresh."""
from __future__ import annotations

import ssl

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DoorEntryConfigEntry
from .const import CONF_LOCKS, CONF_STAIR_LIGHT, DEFAULT_LOCKS, DOMAIN
from .entity import DoorEntryEntity
from .sip import SipBusy, SipError

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: DoorEntryConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    locks = [w.strip() for w in entry.options.get(CONF_LOCKS, DEFAULT_LOCKS).split(",") if w.strip()]
    entities: list[ButtonEntity] = [
        OpenDoorButton(entry, where, first=i == 0) for i, where in enumerate(locks)
    ]
    if stair_light := entry.options.get(CONF_STAIR_LIGHT):
        entities.append(StairLightButton(entry, stair_light))
    entities.append(RefreshImageButton(entry))
    async_add_entities(entities)


def _command_error(err: Exception) -> HomeAssistantError:
    return HomeAssistantError(translation_domain=DOMAIN, translation_key="command_failed",
                              translation_placeholders={"error": str(err)})


class OpenDoorButton(DoorEntryEntity, ButtonEntity):
    """SIP MESSAGE *8*19*<where>## then *8*20*<where>## (verified)."""

    def __init__(self, entry: DoorEntryConfigEntry, where: str, first: bool) -> None:
        super().__init__(entry, f"lock_{where}")
        self._where = where
        self._attr_translation_key = "open_door" if first else "open_lock"
        self._attr_translation_placeholders = {"where": where}

    async def async_press(self) -> None:
        try:
            await self.gateway.open_door(self._where)
        except (SipError, ssl.SSLError, OSError, TimeoutError) as err:
            raise _command_error(err) from err


class StairLightButton(DoorEntryEntity, ButtonEntity):
    """SIP MESSAGE *8*21*<where>## then *8*22*<where>## (from the app code, not field-tested)."""

    _attr_translation_key = "stair_light"

    def __init__(self, entry: DoorEntryConfigEntry, where: str) -> None:
        super().__init__(entry, f"stair_light_{where}")
        self._where = where

    async def async_press(self) -> None:
        try:
            await self.gateway.stair_light(self._where)
        except (SipError, ssl.SSLError, OSError, TimeoutError) as err:
            raise _command_error(err) from err


class RefreshImageButton(DoorEntryEntity, ButtonEntity):
    """Call the door station for a few seconds to update the camera image."""

    _attr_translation_key = "refresh_image"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: DoorEntryConfigEntry) -> None:
        super().__init__(entry, "refresh_image")

    async def async_press(self) -> None:
        try:
            image = await self.gateway.snapshot()
        except SipBusy as err:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="busy") from err
        except (SipError, ssl.SSLError, OSError, TimeoutError) as err:
            raise _command_error(err) from err
        if image is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_video")
