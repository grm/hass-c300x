"""BTicino Door Entry (Classe 300X): local doorbell, door lock, camera and speaker."""
from __future__ import annotations

import datetime as dt
import logging
import ssl
from collections.abc import Mapping
from typing import Any

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_CA,
    CONF_CERT,
    CONF_DEVADDR,
    CONF_GATEWAY_ID,
    CONF_KEY,
    CONF_OPEN_PASSWORD,
    CONF_SIP_ACCOUNT,
    DEFAULT_DEVADDR,
    DOMAIN,
    RENEW_BEFORE,
    RENEW_CHECK_INTERVAL,
)
from .credentials import async_renew, certificate_expiry
from .eliot import EliotAuthError, EliotError
from .gateway import DoorGateway
from .openwebnet import AuthError, test_connection
from .sip import SipError, make_ssl_context

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.MEDIA_PLAYER,
]

type DoorEntryConfigEntry = ConfigEntry[DoorGateway]


def _issue_id(entry: ConfigEntry) -> str:
    return f"certificate_expiring_{entry.entry_id}"


@callback
def _update_expiry_issue(hass: HomeAssistant, entry: ConfigEntry, expiry: dt.datetime) -> None:
    """Repairs warning before the certificate expires (when it cannot be renewed automatically)."""
    if expiry - dt.datetime.now(dt.UTC) > RENEW_BEFORE:
        ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(entry),
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="certificate_expiring",
        translation_placeholders={"title": entry.title, "date": expiry.date().isoformat()},
        data={"entry_id": entry.entry_id},
    )


async def _async_renew(hass: HomeAssistant, entry: DoorEntryConfigEntry) -> str | None:
    """Renew certificate and PswOpen with the remembered password.

    Returns None on success, "auth" if the cloud rejects the password, "cloud" if it is unreachable.
    """
    try:
        data = await async_renew(hass, entry)
    except EliotAuthError as err:
        _LOGGER.warning("The Door Entry account rejected the remembered password: %s", err)
        return "auth"
    except EliotError as err:
        _LOGGER.warning("Could not renew the gateway credentials: %s", err)
        return "cloud"
    hass.config_entries.async_update_entry(entry, data=data)
    _LOGGER.info("Gateway credentials renewed, certificate valid until %s",
                 await hass.async_add_executor_job(certificate_expiry, data[CONF_CERT]))
    return None


async def _async_check_gateway(data: Mapping[str, Any]) -> bool:
    """False if the gateway rejects PswOpen."""
    try:
        await test_connection(data[CONF_HOST], data[CONF_OPEN_PASSWORD])
    except AuthError:
        return False
    except (OSError, TimeoutError, ConnectionError) as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"host": data[CONF_HOST]},
        ) from err
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DoorEntryConfigEntry) -> bool:
    can_renew = CONF_PASSWORD in entry.data
    renewed = False
    expiry = await hass.async_add_executor_job(certificate_expiry, entry.data[CONF_CERT])
    if can_renew and expiry - dt.datetime.now(dt.UTC) < RENEW_BEFORE:
        error = await _async_renew(hass, entry)
        if error is None:
            renewed = True
            expiry = await hass.async_add_executor_job(certificate_expiry, entry.data[CONF_CERT])
        elif error == "cloud" and expiry < dt.datetime.now(dt.UTC):
            raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="renewal_failed")
    if expiry < dt.datetime.now(dt.UTC):
        raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="certificate_expired")
    _update_expiry_issue(hass, entry, expiry)

    # PswOpen rejected (gateway reset): fetch the new one if we can
    if not await _async_check_gateway(entry.data) and (
        not can_renew
        or renewed
        or await _async_renew(hass, entry) is not None
        or not await _async_check_gateway(entry.data)
    ):
        raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="invalid_open_password")

    data = entry.data
    ssl_context = await hass.async_add_executor_job(make_ssl_context, data[CONF_CERT], data[CONF_KEY], data[CONF_CA])
    gateway = DoorGateway(
        host=data[CONF_HOST],
        gateway_id=data[CONF_GATEWAY_ID],
        sip_account=data[CONF_SIP_ACCOUNT],
        ssl_context=ssl_context,
        password=data[CONF_OPEN_PASSWORD],
        devaddr=entry.options.get(CONF_DEVADDR, DEFAULT_DEVADDR),
        ffmpeg=get_ffmpeg_manager(hass).binary,
    )
    try:
        await gateway.test_sip()
    except (SipError, ssl.SSLError, OSError, TimeoutError) as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"host": data[CONF_HOST]},
        ) from err

    entry.runtime_data = gateway
    gateway.monitor.start()
    entry.async_on_unload(gateway.close)
    entry.async_on_unload(entry.add_update_listener(_async_reload))

    @callback
    def _daily_check(_now: dt.datetime) -> None:
        if expiry - dt.datetime.now(dt.UTC) > RENEW_BEFORE:
            return
        if CONF_PASSWORD in entry.data:
            hass.config_entries.async_schedule_reload(entry.entry_id)  # setup renews
        else:
            _update_expiry_issue(hass, entry, expiry)

    entry.async_on_unload(async_track_time_interval(hass, _daily_check, RENEW_CHECK_INTERVAL))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload(hass: HomeAssistant, entry: DoorEntryConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: DoorEntryConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))
