"""Config and options flow."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.doorentry.const import (
    CONF_DEVADDR,
    CONF_GATEWAY_ID,
    CONF_LOCKS,
    CONF_OPEN_PASSWORD,
    CONF_SIP_ACCOUNT,
    CONF_STAIR_LIGHT,
    CONF_TALK_GAIN,
    DOMAIN,
)
from custom_components.doorentry.eliot import EliotAuthError

from .conftest import ACCOUNT, GATEWAY_ID

LOGIN = {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"}


@pytest.fixture
def eliot():
    with patch("custom_components.doorentry.config_flow.EliotClient", autospec=True) as cls:
        client = cls.return_value
        client.email = "user@example.com"
        client.plants.return_value = {"42": "Maison"}
        client.gateway_id.return_value = GATEWAY_ID
        client.gateway.return_value = {"PswOpen": "12345", "IpAddress": "192.0.2.10"}
        client.sip_accounts.return_value = ["other@x", ACCOUNT]
        client.sign_certificate.return_value = ("CERT", "CA")
        with patch("custom_components.doorentry.credentials.generate_key_and_csr", return_value=("KEY", "CSR")):
            yield client


@pytest.fixture
def local_ok():
    with patch("custom_components.doorentry.config_flow._test_local", AsyncMock(return_value=None)) as test:
        yield test


@pytest.fixture(autouse=True)
def no_setup():
    with patch("custom_components.doorentry.async_setup_entry", AsyncMock(return_value=True)):
        yield


async def test_full_flow(hass: HomeAssistant, eliot, local_ok) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    # single installation: the plant step is skipped
    assert result["step_id"] == "host"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: " 192.0.2.20 "})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Maison"
    data = result["data"]
    assert data[CONF_HOST] == "192.0.2.20"
    assert data[CONF_GATEWAY_ID] == GATEWAY_ID
    assert data[CONF_OPEN_PASSWORD] == "12345"
    assert data[CONF_SIP_ACCOUNT] == ACCOUNT  # the Home Assistant account is preferred
    assert CONF_PASSWORD not in data  # not remembered by default
    eliot.create_sip_account.assert_not_called()
    eliot.sign_certificate.assert_awaited_once_with(ACCOUNT, "CSR")


async def test_remember_password(hass: HomeAssistant, eliot, local_ok) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**LOGIN, "remember_password": True})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "192.0.2.10"})
    assert result["data"][CONF_PASSWORD] == "secret"


async def test_creates_account_when_none(hass: HomeAssistant, eliot, local_ok) -> None:
    eliot.sip_accounts.return_value = []
    eliot.create_sip_account.return_value = "new@x"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "192.0.2.10"})
    assert result["data"][CONF_SIP_ACCOUNT] == "new@x"


async def test_invalid_auth(hass: HomeAssistant, eliot) -> None:
    eliot.login.side_effect = EliotAuthError("HTTP 401")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_local_error_then_retry(hass: HomeAssistant, eliot, local_ok) -> None:
    local_ok.side_effect = ["invalid_open_password", None]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "192.0.2.10"})
    assert result["errors"] == {"base": "invalid_open_password"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "192.0.2.10"})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_already_configured(hass: HomeAssistant, eliot, config_entry) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(hass: HomeAssistant, eliot, local_ok, config_entry) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new", "remember_password": True}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data["certificate"] == "CERT"
    assert config_entry.data[CONF_HOST] == "192.0.2.10"
    assert config_entry.data[CONF_PASSWORD] == "new"
    eliot.sip_accounts.assert_awaited()


async def test_reconfigure(hass: HomeAssistant, local_ok, config_entry) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "192.0.2.99"})
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_HOST] == "192.0.2.99"


async def test_options(hass: HomeAssistant, config_entry) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    options = {CONF_DEVADDR: "20", CONF_LOCKS: "20;21", CONF_STAIR_LIGHT: "x", CONF_TALK_GAIN: 12}
    result = await hass.config_entries.options.async_configure(result["flow_id"], options)
    assert result["errors"] == {"base": "invalid_address"}
    options[CONF_STAIR_LIGHT] = "20"
    result = await hass.config_entries.options.async_configure(result["flow_id"], options)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_LOCKS] == "20, 21"
    assert config_entry.options[CONF_TALK_GAIN] == 12
