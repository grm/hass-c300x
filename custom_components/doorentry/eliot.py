"""Eliot cloud API (myhomeweb.com), only used once at setup: PswOpen, SIP account, client certificate.

See docs/api_endpoints.md and docs/field_tests.md § 1.
"""
from __future__ import annotations

import io
import json
import logging
import zipfile
from collections.abc import Mapping
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://www.myhomeweb.com"
APP_VERSION = "legacy-doorentryforc300x-android-1.17.5"
TIMEOUT = aiohttp.ClientTimeout(total=30)


class EliotError(Exception):
    """Cloud unreachable or unexpected answer."""


class EliotAuthError(EliotError):
    """Wrong email or password."""


def generate_key_and_csr(sip_account: str) -> tuple[str, str]:
    """RSA key + CSR with the subject the app uses. CPU bound: run it in an executor."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "IT"),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Italy"),
        x509.NameAttribute(NameOID.LOCALITY_NAME, "Varese"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Bticino spa"),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "DSI-PQS"),
        x509.NameAttribute(NameOID.COMMON_NAME, sip_account),
    ])).sign(key, hashes.SHA256())
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return key_pem, csr.public_bytes(serialization.Encoding.PEM).decode()


def unzip_certificate(data: bytes) -> tuple[str, str]:
    """signcert answers a raw zip (not Base64): <SipAccount>.cert.pem and ca-chain.cert.pem."""
    cert = ca = None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                if "ca-chain" in name:
                    ca = zf.read(name).decode()
                elif name.endswith(".pem"):
                    cert = zf.read(name).decode()
    except zipfile.BadZipFile as err:
        raise EliotError("signcert did not return a zip") from err
    if not cert or not ca:
        raise EliotError("Incomplete certificate zip")
    return cert, ca


class EliotClient:
    """Minimal async client."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._token: str | None = None
        self.email = ""

    async def _request(self, method: str, path: str, *, body: Any = None,
                       headers: dict[str, str] | None = None) -> tuple[Mapping[str, str], bytes]:
        hdrs = {"prj_name": "C3X", **(headers or {})}
        if self._token:
            hdrs["auth_token"] = self._token
        try:
            async with self._session.request(
                method, BASE_URL + path, json=body, headers=hdrs, timeout=TIMEOUT
            ) as resp:
                data = await resp.read()
                if resp.status in (401, 403, 432):
                    raise EliotAuthError(f"HTTP {resp.status}")
                if resp.status == 404:
                    return resp.headers, b"[]"
                if resp.status >= 400:
                    raise EliotError(f"{method} {path}: HTTP {resp.status}")
                return resp.headers, data
        except aiohttp.ClientError as err:
            raise EliotError(str(err)) from err

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        _, data = await self._request(method, path, **kwargs)
        try:
            return json.loads(data or b"null")
        except ValueError as err:
            raise EliotError(f"{path}: invalid JSON") from err

    async def login(self, email: str, password: str) -> None:
        self.email = email
        headers, _ = await self._request(
            "POST", "/eliot/users/sign_in", body={"username": email, "pwd": password, "appVersion": APP_VERSION}
        )
        self._token = headers.get("auth_token")
        if not self._token:
            raise EliotAuthError("No auth_token in sign_in answer")

    async def plants(self) -> dict[str, str]:
        """PlantId → PlantName."""
        plants = await self._json("GET", "/eliot/plants")
        return {str(p["PlantId"]): p.get("PlantName") or str(p["PlantId"]) for p in plants or []}

    async def gateway_id(self, plant_id: str) -> str:
        gateways = await self._json("GET", f"/eliot/plants/{plant_id}/gateway/")
        if not gateways:
            raise EliotError(f"No gateway in plant {plant_id}")
        return str(gateways[0]["GatewayId"])

    async def gateway(self, plant_id: str, gateway_id: str) -> dict[str, Any]:
        # need_token 0 = regular login (1 is only for the association)
        return await self._json("GET", f"/eliot/plants/{plant_id}/gateway/{gateway_id}", headers={"need_token": "0"})

    async def sip_accounts(self, plant_id: str, gateway_id: str) -> list[str]:
        users = await self._json("GET", f"/eliot/sip/users/plants/{plant_id}/gateway/{gateway_id}")
        return [u["SipAccount"] for u in users or [] if u.get("SipAccount")]

    async def create_sip_account(self, gateway_id: str, email: str, device_id: str) -> str:
        account = f"{email.replace('@', '-')}-{device_id}@{gateway_id}.bs.iotleg.com"
        await self._request("POST", "/eliot/sip/user", body={
            "SipAccount": account, "DeviceName": "Home Assistant", "GatewayId": gateway_id, "IdDevice": device_id,
        })
        return account

    async def sign_certificate(self, sip_account: str, csr_pem: str) -> tuple[str, str]:
        _, data = await self._request("POST", "/eliot/users/cert/signcert",
                                      body={"CommonName": sip_account, "CSR": csr_pem})
        return unzip_certificate(data)
