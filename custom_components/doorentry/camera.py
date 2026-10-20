"""Door station camera: live MJPEG while viewed, last image otherwise."""
from __future__ import annotations

import logging
import ssl

from aiohttp import web
from homeassistant.components.camera import Camera, async_get_still_stream
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DoorEntryConfigEntry
from .entity import DoorEntryEntity
from .sip import SipBusy, SipError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: DoorEntryConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([DoorCamera(entry)])


class DoorCamera(DoorEntryEntity, Camera):
    """Every live view is a call to the door station (its screen and camera light turn on).

    Still images never start a call, so the 10 s polling of the dashboard cards does not
    wake the door station up: they show the last image of the last call. Use the "refresh
    image" button (or an automation on the doorbell event) to update it.
    """

    _attr_name = None
    _attr_frame_interval = 0.1  # the door station sends 10 images/s
    content_type = "image/jpeg"

    def __init__(self, entry: DoorEntryConfigEntry) -> None:
        DoorEntryEntity.__init__(self, entry, "camera")
        Camera.__init__(self)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.gateway.add_call_listener(self.async_write_ha_state))

    @property
    def is_streaming(self) -> bool:
        return self.gateway.in_call

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        return self.gateway.current_image()

    async def handle_async_mjpeg_stream(self, request: web.Request) -> web.StreamResponse | None:
        try:
            async with self.gateway.session():
                return await async_get_still_stream(request, self.gateway.next_image, self.content_type, 0)
        except SipBusy:
            _LOGGER.warning("Door station busy (ringing or in use), no live view")
        except (SipError, ssl.SSLError, OSError, TimeoutError) as err:
            _LOGGER.warning("Could not call the door station: %s", err)
        return None
