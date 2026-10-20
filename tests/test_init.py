"""Setup, entities and monitor events."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.camera import async_get_image
from homeassistant.components.media_player import MediaPlayerState
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.doorentry.const import DOMAIN
from custom_components.doorentry.openwebnet import AuthError, DoorEvent
from custom_components.doorentry.sip import SipBusy

from .conftest import GATEWAY_ID, entry_data, make_cert


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{GATEWAY_ID}_{key}")
    assert entity_id, key
    return entity_id


async def test_setup_and_ring(hass: HomeAssistant, config_entry, mock_gateway) -> None:
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    for platform, key in [
        ("binary_sensor", "ringing"), ("binary_sensor", "call"), ("button", "lock_20"),
        ("button", "refresh_image"), ("camera", "camera"), ("event", "doorbell"),
        ("event", "door_opened"), ("media_player", "speaker"),
    ]:
        _entity(hass, platform, key)
    ringing = _entity(hass, "binary_sensor", "ringing")
    doorbell = _entity(hass, "event", "doorbell")

    monitor = config_entry.runtime_data.monitor
    assert hass.states.get(ringing).state == STATE_UNAVAILABLE
    monitor._set_connected(True)
    await hass.async_block_till_done()
    assert hass.states.get(ringing).state == STATE_OFF
    assert hass.states.get(doorbell).state == STATE_UNKNOWN

    for listener in list(monitor._listeners):
        listener(DoorEvent("ring", "21", "*8*1#1#4#21*10##"))
    await hass.async_block_till_done()
    assert hass.states.get(ringing).state == STATE_ON
    state = hass.states.get(doorbell)
    assert state.attributes["event_type"] == "ring"
    assert state.attributes["address"] == "21"

    for listener in list(monitor._listeners):
        listener(DoorEvent("ring_end", None, "*8*3#1#4*420##"))
    await hass.async_block_till_done()
    assert hass.states.get(ringing).state == STATE_OFF

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_gateway_unreachable(hass: HomeAssistant, config_entry, mock_gateway) -> None:
    mock_gateway.side_effect = OSError("unreachable")
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_wrong_open_password(hass: HomeAssistant, config_entry, mock_gateway) -> None:
    mock_gateway.side_effect = AuthError("NACK")
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert any(flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress())


async def test_certificate_expired(hass: HomeAssistant, mock_gateway) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=GATEWAY_ID, data=entry_data(*make_cert(-1)))
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    mock_gateway.assert_not_called()


async def test_buttons_and_camera(hass: HomeAssistant, certificate, mock_gateway) -> None:
    config_entry = MockConfigEntry(domain=DOMAIN, unique_id=GATEWAY_ID, data=entry_data(*certificate),
                                   options={"locks": "20, 21", "stair_light": "20"})
    await _setup(hass, config_entry)
    gateway = config_entry.runtime_data

    with patch.object(gateway, "open_door", AsyncMock()) as open_door:
        await hass.services.async_call(
            "button", "press", {"entity_id": _entity(hass, "button", "lock_21")}, blocking=True
        )
    open_door.assert_awaited_once_with("21")
    _entity(hass, "button", "stair_light_20")

    # the still image never calls the door station: last image of the last call
    gateway.last_image = b"\xff\xd8jpeg\xff\xd9"
    image = await async_get_image(hass, _entity(hass, "camera", "camera"))
    assert image.content == gateway.last_image

    with patch.object(gateway, "snapshot", AsyncMock(side_effect=SipBusy("486"))), \
            pytest.raises(HomeAssistantError, match="busy"):
        await hass.services.async_call(
            "button", "press", {"entity_id": _entity(hass, "button", "refresh_image")}, blocking=True
        )


async def test_speaker(hass: HomeAssistant, config_entry, mock_gateway) -> None:
    await _setup(hass, config_entry)
    gateway = config_entry.runtime_data
    speaker = _entity(hass, "media_player", "speaker")
    with (
        patch("custom_components.doorentry.media_player.load_speex"),
        patch.object(gateway, "talk", AsyncMock(return_value=2.0)) as talk,
    ):
        await hass.services.async_call("media_player", "play_media", {
            "entity_id": speaker, "media_content_type": "music", "media_content_id": "http://example.com/a.mp3",
        }, blocking=True)
    assert talk.await_args.args[0] == "http://example.com/a.mp3"
    assert talk.await_args.args[2] == 18
    assert hass.states.get(speaker).state == MediaPlayerState.IDLE
