"""Door station speaker: plays any media (TTS, files) through the intercom call."""
from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Any

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    BrowseMedia,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DoorEntryConfigEntry
from .const import CONF_TALK_GAIN, DEFAULT_TALK_GAIN, DOMAIN
from .entity import DoorEntryEntity
from .sip import SipBusy, SipError
from .speex import SpeexEncoder
from .speex import _load as load_speex

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: DoorEntryConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([DoorSpeaker(entry)])


class DoorSpeaker(DoorEntryEntity, MediaPlayerEntity):
    """Half-duplex: the door station speaker plays what we send, nothing else."""

    _attr_translation_key = "speaker"
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.BROWSE_MEDIA
        | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
        | MediaPlayerEntityFeature.STOP
    )
    _attr_state = MediaPlayerState.IDLE

    def __init__(self, entry: DoorEntryConfigEntry) -> None:
        super().__init__(entry, "speaker")
        self._gain = entry.options.get(CONF_TALK_GAIN, DEFAULT_TALK_GAIN)
        self._task: asyncio.Task | None = None

    async def async_browse_media(
        self, media_content_type: MediaType | str | None = None, media_content_id: str | None = None
    ) -> BrowseMedia:
        return await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: (item.media_content_type or "").startswith("audio/"),
        )

    async def async_play_media(self, media_type: MediaType | str, media_id: str, **kwargs: Any) -> None:
        if media_source.is_media_source_id(media_id):
            media = await media_source.async_resolve_media(self.hass, media_id, self.entity_id)
            media_id = media.url
        url = async_process_play_media_url(self.hass, media_id)
        try:
            await self.hass.async_add_executor_job(load_speex)
        except OSError as err:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_speex") from err

        await self._cancel()
        self._task = task = asyncio.create_task(self.gateway.talk(url, SpeexEncoder, self._gain))
        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()
        try:
            seconds = await task
            _LOGGER.debug("Played %.1f s on the door station", seconds)
        except asyncio.CancelledError:
            if not task.cancelled() or asyncio.current_task().cancelling():
                raise  # we are cancelled ourselves, not stopped by async_media_stop
        except SipBusy as err:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="busy") from err
        except (SipError, ssl.SSLError, OSError, TimeoutError) as err:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="command_failed",
                                     translation_placeholders={"error": str(err)}) from err
        finally:
            if self._task is task:
                self._task = None
                self._attr_state = MediaPlayerState.IDLE
                self.async_write_ha_state()

    async def _cancel(self) -> None:
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def async_media_stop(self) -> None:
        await self._cancel()
        self._attr_state = MediaPlayerState.IDLE
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        await self._cancel()
