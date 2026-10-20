"""Gateway credentials from the Eliot cloud: used by the config flow and the automatic renewal."""
from __future__ import annotations

import datetime as dt
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_CA,
    CONF_CERT,
    CONF_GATEWAY_ID,
    CONF_KEY,
    CONF_OPEN_PASSWORD,
    CONF_PLANT_ID,
    CONF_SIP_ACCOUNT,
)
from .eliot import EliotClient, EliotError, generate_key_and_csr

DEVICE_ID = "HOMEASSISTANT"


def certificate_expiry(cert_pem: str) -> dt.datetime:
    from cryptography import x509

    return x509.load_pem_x509_certificate(cert_pem.encode()).not_valid_after_utc


async def async_fetch_credentials(
    hass: HomeAssistant, client: EliotClient, plant_id: str, prefer_account: str | None = None
) -> dict[str, Any]:
    """PswOpen, cloud-known address, SIP account and a freshly signed client certificate.

    Raises EliotError (EliotAuthError if the login is no longer valid).
    """
    try:
        gateway_id = await client.gateway_id(plant_id)
        gateway = await client.gateway(plant_id, gateway_id)
        accounts = await client.sip_accounts(plant_id, gateway_id)
        # Reusing an existing account with a new key is what was validated on a real gateway
        account = next(
            (a for a in accounts if a == prefer_account),
            next((a for a in accounts if f"-{DEVICE_ID}@" in a), accounts[0] if accounts else None),
        )
        if account is None:
            account = await client.create_sip_account(gateway_id, client.email, DEVICE_ID)
        key_pem, csr_pem = await hass.async_add_executor_job(generate_key_and_csr, account)
        cert_pem, ca_pem = await client.sign_certificate(account, csr_pem)
        open_password = gateway["PswOpen"]
    except KeyError as err:
        raise EliotError(f"Unexpected cloud answer: missing {err}") from err
    return {
        CONF_PLANT_ID: plant_id,
        CONF_GATEWAY_ID: gateway_id,
        CONF_HOST: gateway.get("IpAddress") or "",
        CONF_OPEN_PASSWORD: open_password,
        CONF_SIP_ACCOUNT: account,
        CONF_CERT: cert_pem,
        CONF_KEY: key_pem,
        CONF_CA: ca_pem,
    }


async def async_renew(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """New certificate and PswOpen with the remembered account password; keeps the local address."""
    client = EliotClient(async_get_clientsession(hass))
    await client.login(entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    fresh = await async_fetch_credentials(
        hass, client, entry.data[CONF_PLANT_ID], prefer_account=entry.data[CONF_SIP_ACCOUNT]
    )
    if fresh[CONF_GATEWAY_ID] != entry.data[CONF_GATEWAY_ID]:
        raise EliotError("The installation now has another gateway")
    return {**entry.data, **fresh, CONF_HOST: entry.data[CONF_HOST]}
