"""Certificate expiry: automatic renewal with a remembered password, Repairs warning otherwise."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.doorentry.const import CONF_CA, CONF_CERT, CONF_KEY, CONF_OPEN_PASSWORD, DOMAIN
from custom_components.doorentry.eliot import EliotAuthError, EliotError
from custom_components.doorentry.openwebnet import AuthError

from .conftest import GATEWAY_ID, entry_data, make_cert


def _entry(days: int, remember: bool) -> MockConfigEntry:
    data = entry_data(*make_cert(days))
    if remember:
        data[CONF_PASSWORD] = "secret"
    return MockConfigEntry(domain=DOMAIN, unique_id=GATEWAY_ID, title="Home", data=data)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"certificate_expiring_{entry.entry_id}")


def _renewed(entry_data_: dict, days: int = 720, **changes) -> dict:
    cert, key = make_cert(days)
    return {**entry_data_, CONF_CERT: cert, CONF_KEY: key, CONF_CA: cert, **changes}


async def test_expiring_without_password_warns(hass: HomeAssistant, mock_gateway) -> None:
    entry = _entry(10, remember=False)
    with patch("custom_components.doorentry.async_renew", AsyncMock()) as renew:
        await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    renew.assert_not_called()
    issue = _issue(hass, entry)
    assert issue and issue.is_fixable
    assert issue.translation_placeholders["title"] == "Home"


async def test_valid_certificate_no_warning(hass: HomeAssistant, mock_gateway) -> None:
    entry = _entry(365, remember=False)
    await _setup(hass, entry)
    assert _issue(hass, entry) is None


async def test_expiring_with_password_renews(hass: HomeAssistant, mock_gateway) -> None:
    entry = _entry(10, remember=True)
    renewed = _renewed(dict(entry.data))
    with patch("custom_components.doorentry.async_renew", AsyncMock(return_value=renewed)) as renew:
        await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    renew.assert_awaited_once()
    assert entry.data[CONF_CERT] == renewed[CONF_CERT]
    assert _issue(hass, entry) is None


async def test_expiring_cloud_down_keeps_working(hass: HomeAssistant, mock_gateway) -> None:
    entry = _entry(10, remember=True)
    with patch("custom_components.doorentry.async_renew", AsyncMock(side_effect=EliotError("down"))):
        await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert _issue(hass, entry) is not None


async def test_expired_cloud_down_retries(hass: HomeAssistant, mock_gateway) -> None:
    entry = _entry(-1, remember=True)
    with patch("custom_components.doorentry.async_renew", AsyncMock(side_effect=EliotError("down"))):
        await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_expired_password_rejected_asks_reauth(hass: HomeAssistant, mock_gateway) -> None:
    entry = _entry(-1, remember=True)
    with patch("custom_components.doorentry.async_renew", AsyncMock(side_effect=EliotAuthError("401"))):
        await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress())


async def test_open_password_changed_is_refreshed(hass: HomeAssistant, mock_gateway) -> None:
    entry = _entry(365, remember=True)
    mock_gateway.side_effect = [AuthError("NACK"), None]
    renewed = _renewed(dict(entry.data), **{CONF_OPEN_PASSWORD: "67890"})
    with patch("custom_components.doorentry.async_renew", AsyncMock(return_value=renewed)):
        await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_OPEN_PASSWORD] == "67890"
    assert mock_gateway.await_args.args[1] == "67890"


async def test_repair_flow_starts_reauth(hass: HomeAssistant, mock_gateway) -> None:
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "repairs", {})
    entry = _entry(10, remember=False)
    await _setup(hass, entry)
    from custom_components.doorentry.repairs import async_create_fix_flow

    flow = await async_create_fix_flow(hass, f"certificate_expiring_{entry.entry_id}", {"entry_id": entry.entry_id})
    flow.hass = hass
    flow.handler = DOMAIN
    flow.issue_id = f"certificate_expiring_{entry.entry_id}"
    result = await flow.async_step_init()
    assert result["step_id"] == "confirm"
    await flow.async_step_confirm({})
    await hass.async_block_till_done()
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())
