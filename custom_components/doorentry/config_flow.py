"""Config flow: one-time Eliot cloud login, then everything runs locally."""
from __future__ import annotations

import logging
import ssl
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import NumberSelector, NumberSelectorConfig

from .const import (
    CONF_CA,
    CONF_CERT,
    CONF_DEVADDR,
    CONF_GATEWAY_ID,
    CONF_KEY,
    CONF_LOCKS,
    CONF_OPEN_PASSWORD,
    CONF_PLANT_ID,
    CONF_REMEMBER,
    CONF_SIP_ACCOUNT,
    CONF_STAIR_LIGHT,
    CONF_TALK_GAIN,
    DEFAULT_DEVADDR,
    DEFAULT_LOCKS,
    DEFAULT_TALK_GAIN,
    DOMAIN,
)
from .credentials import async_fetch_credentials
from .eliot import EliotAuthError, EliotClient, EliotError
from .openwebnet import AuthError, test_connection
from .sip import SipError, make_ssl_context, open_connection

_LOGGER = logging.getLogger(__name__)

LOGIN_SCHEMA = vol.Schema({
    vol.Required(CONF_EMAIL): str,
    vol.Required(CONF_PASSWORD): str,
    vol.Optional(CONF_REMEMBER, default=False): bool,
})


async def _test_local(hass, data: dict[str, Any]) -> str | None:
    """Error key, or None if both OpenWebNet and SIP answer."""
    try:
        await test_connection(data[CONF_HOST], data[CONF_OPEN_PASSWORD])
    except AuthError:
        return "invalid_open_password"
    except (OSError, TimeoutError, ConnectionError):
        return "cannot_connect_local"
    try:
        ctx = await hass.async_add_executor_job(make_ssl_context, data[CONF_CERT], data[CONF_KEY], data[CONF_CA])
        conn = await open_connection(data[CONF_HOST], data[CONF_GATEWAY_ID], data[CONF_SIP_ACCOUNT], ctx)
        await conn.close()
    except (SipError, ssl.SSLError, OSError, TimeoutError) as err:
        _LOGGER.warning("SIP test failed: %s", err)
        return "cannot_connect_sip"
    return None


class DoorEntryConfigFlow(ConfigFlow, domain=DOMAIN):
    """Eliot login → plant → local address."""

    VERSION = 1

    def __init__(self) -> None:
        self._client: EliotClient | None = None
        self._email = ""
        self._password: str | None = None  # only kept if the user wants automatic renewal
        self._plants: dict[str, str] = {}
        self._data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DoorEntryOptionsFlow()

    async def _login(self, user_input: dict[str, Any]) -> str | None:
        self._client = EliotClient(async_get_clientsession(self.hass))
        self._email = user_input[CONF_EMAIL]
        self._password = user_input[CONF_PASSWORD] if user_input.get(CONF_REMEMBER) else None
        try:
            await self._client.login(self._email, user_input[CONF_PASSWORD])
            self._plants = await self._client.plants()
        except EliotAuthError:
            return "invalid_auth"
        except EliotError as err:
            _LOGGER.warning("Eliot login failed: %s", err)
            return "cannot_connect"
        return None if self._plants else "no_plant"

    async def _fetch(self, plant_id: str, prefer_account: str | None = None) -> None:
        self._data = {
            CONF_EMAIL: self._email,
            **await async_fetch_credentials(self.hass, self._client, plant_id, prefer_account),
        }
        if self._password:
            self._data[CONF_PASSWORD] = self._password

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not (error := await self._login(user_input)):
                if len(self._plants) == 1:
                    return await self.async_step_plant({CONF_PLANT_ID: next(iter(self._plants))})
                return await self.async_step_plant()
            errors["base"] = error
        return self.async_show_form(
            step_id="user", data_schema=self.add_suggested_values_to_schema(LOGIN_SCHEMA, user_input), errors=errors
        )

    async def async_step_plant(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="plant", data_schema=vol.Schema({
                vol.Required(CONF_PLANT_ID): vol.In(self._plants),
            }))
        try:
            await self._fetch(user_input[CONF_PLANT_ID])
        except EliotError as err:
            _LOGGER.warning("Could not get the gateway credentials: %s", err)
            return self.async_abort(reason="cloud_error")
        await self.async_set_unique_id(self._data[CONF_GATEWAY_ID])
        self._abort_if_unique_id_configured()
        return await self.async_step_host()

    async def async_step_host(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data[CONF_HOST] = user_input[CONF_HOST].strip()
            if not (error := await _test_local(self.hass, self._data)):
                return self.async_create_entry(
                    title=self._plants.get(self._data[CONF_PLANT_ID]) or f"Classe 300X {self._data[CONF_GATEWAY_ID]}",
                    data=self._data,
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="host",
            data_schema=vol.Schema({vol.Required(CONF_HOST, default=self._data.get(CONF_HOST, "")): str}),
            description_placeholders={"gateway_id": self._data[CONF_GATEWAY_ID]},
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """New certificate and PswOpen from the cloud, keeping the same gateway."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            if not (error := await self._login({CONF_EMAIL: entry.data[CONF_EMAIL], **user_input})):
                try:
                    await self._fetch(entry.data[CONF_PLANT_ID], entry.data[CONF_SIP_ACCOUNT])
                except EliotError as err:
                    _LOGGER.warning("Could not get the gateway credentials: %s", err)
                    return self.async_abort(reason="cloud_error")
                self._data[CONF_HOST] = entry.data[CONF_HOST]
                if not (error := await _test_local(self.hass, self._data)):
                    return self.async_update_reload_and_abort(entry, data=self._data)
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_REMEMBER, default=CONF_PASSWORD in entry.data): bool,
            }),
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the gateway address (e.g. new DHCP lease)."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_HOST: user_input[CONF_HOST].strip()}
            if not (error := await _test_local(self.hass, data)):
                return self.async_update_reload_and_abort(entry, data=data)
            errors["base"] = error
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({vol.Required(CONF_HOST, default=entry.data[CONF_HOST]): str}),
            errors=errors,
        )


def _addresses(value: str) -> list[str]:
    return [a.strip() for a in value.replace(";", ",").split(",") if a.strip()]


class DoorEntryOptionsFlow(OptionsFlow):
    """Door station address, locks, stair light, talk volume."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            values = [user_input[CONF_DEVADDR], *_addresses(user_input.get(CONF_LOCKS, ""))]
            if user_input.get(CONF_STAIR_LIGHT):
                values.append(user_input[CONF_STAIR_LIGHT])
            if not all(v.strip().isdigit() for v in values):
                errors["base"] = "invalid_address"
            else:
                user_input[CONF_LOCKS] = ", ".join(_addresses(user_input.get(CONF_LOCKS, "")))
                user_input[CONF_TALK_GAIN] = int(user_input[CONF_TALK_GAIN])
                return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        schema = vol.Schema({
            vol.Required(CONF_DEVADDR, default=options.get(CONF_DEVADDR, DEFAULT_DEVADDR)): str,
            vol.Optional(CONF_LOCKS, default=options.get(CONF_LOCKS, DEFAULT_LOCKS)): str,
            vol.Optional(CONF_STAIR_LIGHT, description={"suggested_value": options.get(CONF_STAIR_LIGHT)}): str,
            vol.Required(CONF_TALK_GAIN, default=options.get(CONF_TALK_GAIN, DEFAULT_TALK_GAIN)): NumberSelector(
                NumberSelectorConfig(min=0, max=36, step=1, unit_of_measurement="dB")
            ),
        })
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
