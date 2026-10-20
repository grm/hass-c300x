"""Fixtures: a config entry with a throw-away certificate, no network."""
from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from homeassistant.const import CONF_EMAIL, CONF_HOST
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.doorentry.const import (
    CONF_CA,
    CONF_CERT,
    CONF_GATEWAY_ID,
    CONF_KEY,
    CONF_OPEN_PASSWORD,
    CONF_PLANT_ID,
    CONF_SIP_ACCOUNT,
    DOMAIN,
)

GATEWAY_ID = "1234567"
ACCOUNT = f"user-example.com-HOMEASSISTANT@{GATEWAY_ID}.bs.iotleg.com"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def make_cert(days: int) -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ACCOUNT)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(1).not_valid_before(now - dt.timedelta(days=400))
        .not_valid_after(now + dt.timedelta(days=days)).sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return cert.public_bytes(serialization.Encoding.PEM).decode(), key_pem


@pytest.fixture(scope="session")
def certificate() -> tuple[str, str]:
    return make_cert(365)


def entry_data(cert: str, key: str) -> dict:
    return {
        CONF_EMAIL: "user@example.com",
        CONF_PLANT_ID: "42",
        CONF_GATEWAY_ID: GATEWAY_ID,
        CONF_HOST: "192.0.2.10",
        CONF_OPEN_PASSWORD: "12345",
        CONF_SIP_ACCOUNT: ACCOUNT,
        CONF_CERT: cert,
        CONF_KEY: key,
        CONF_CA: cert,
    }


@pytest.fixture
def config_entry(certificate) -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, unique_id=GATEWAY_ID, title="Maison", data=entry_data(*certificate))


@pytest.fixture
def mock_gateway():
    """Gateway reachable: OpenWebNet auth and SIP REGISTER succeed, monitor not started."""
    with (
        patch("custom_components.doorentry.test_connection", AsyncMock()) as own,
        patch("custom_components.doorentry.gateway.DoorGateway.test_sip", AsyncMock()),
        patch("custom_components.doorentry.openwebnet.OpenWebNetMonitor.start"),
    ):
        yield own
