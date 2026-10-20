"""Pure Python SIP client implementation.

This is a minimal SIP client that implements just what we need:
- REGISTER with TLS + client certificate
- MESSAGE to send OpenWebNet frames

No external dependencies beyond Python stdlib + cryptography for digest auth.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import socket
import ssl
import time
import uuid
from typing import Callable
from urllib.parse import quote

_LOGGER = logging.getLogger(__name__)


def _status_code(response: str) -> int:
    match = re.match(r"SIP/2\.0\s+(\d{3})", response)
    return int(match.group(1)) if match else 0


class SipClientPure:
    """Pure Python SIP client for BTicino C300X."""

    def __init__(
        self,
        gateway_ip: str,
        gateway_id: str,
        sip_account: str,
        sip_password: str,
        cert_file: str | None = None,
        key_file: str | None = None,
        ca_file: str | None = None,
    ):
        """Initialize SIP client."""
        self.gateway_ip = gateway_ip
        self.gateway_id = gateway_id
        self.sip_account = sip_account
        self.sip_password = sip_password
        self.cert_file = cert_file
        self.key_file = key_file
        self.ca_file = ca_file

        self.domain = f"{gateway_id}.bs.iotleg.com"
        self.local_port = random.randint(5062, 6000)

        # Extract username from sip_account
        self.username = sip_account.split("@")[0] if "@" in sip_account else sip_account

        # SIP tags and identifiers
        self.call_id = str(uuid.uuid4())
        self.from_tag = self._generate_tag()
        self.cseq = 1

        # Connection
        self._sock: ssl.SSLSocket | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._registered = False

        # Auth
        self._auth_nonce: str | None = None
        self._auth_realm: str | None = None

    def _generate_tag(self) -> str:
        """Generate a random SIP tag."""
        return hashlib.md5(str(uuid.uuid4()).encode()).hexdigest()[:8]

    def _generate_branch(self) -> str:
        """Generate a Via branch parameter."""
        return "z9hG4bK" + hashlib.md5(str(uuid.uuid4()).encode()).hexdigest()[:16]

    def _calculate_digest_response(
        self,
        method: str,
        uri: str,
        nonce: str,
        realm: str,
    ) -> str:
        """Calculate Digest authentication response."""
        # HA1 = MD5(username:realm:password)
        ha1 = hashlib.md5(
            f"{self.username}:{realm}:{self.sip_password}".encode()
        ).hexdigest()

        # HA2 = MD5(method:uri)
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()

        # Response = MD5(HA1:nonce:HA2)
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()

        return response

    async def _connect(self) -> None:
        """Establish TLS connection to gateway."""
        if self._writer:
            return  # Already connected

        _LOGGER.info("Connecting to %s:5061 via TLS", self.gateway_ip)

        # Create SSL context
        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)

        # Load client certificate if provided
        if self.cert_file and self.key_file:
            _LOGGER.info("Loading client certificate: %s", self.cert_file)
            ssl_context.load_cert_chain(
                certfile=self.cert_file,
                keyfile=self.key_file,
            )

        # Load CA bundle if provided
        if self.ca_file:
            ssl_context.load_verify_locations(cafile=self.ca_file)

        # Gateway cert CN is "sip:<domain>" with no SAN, so standard hostname matching always fails
        ssl_context.check_hostname = False

        # Connect
        try:
            self._reader, self._writer = await asyncio.open_connection(
                self.gateway_ip,
                5061,
                ssl=ssl_context,
                server_hostname=self.domain,
            )
            _LOGGER.info("TLS connection established")
        except Exception as err:
            _LOGGER.error("TLS connection failed: %s", err)
            raise

        peer_cert = self._writer.get_extra_info("peercert") or {}
        peer_cn = next(
            (v for rdn in peer_cert.get("subject", ()) for k, v in rdn if k == "commonName"),
            None,
        )
        if peer_cn != f"sip:{self.domain}":
            self._writer.close()
            self._writer = None
            self._reader = None
            raise ssl.SSLError(f"Unexpected gateway certificate CN: {peer_cn}")

    async def _send(self, message: str) -> None:
        """Send SIP message."""
        if not self._writer:
            raise ConnectionError("Not connected")

        _LOGGER.debug("TX:\n%s", message)
        self._writer.write(message.encode())
        await self._writer.drain()

    async def _receive(self, timeout: float = 5.0) -> str:
        """Receive one SIP message."""
        if not self._reader:
            raise ConnectionError("Not connected")

        try:
            while True:
                head = await asyncio.wait_for(
                    self._reader.readuntil(b"\r\n\r\n"), timeout=timeout
                )
                # Skip CRLF keep-alives (RFC 5626)
                if head.strip():
                    break

            match = re.search(rb"Content-Length:\s*(\d+)", head, re.IGNORECASE)
            body = b""
            if match and int(match.group(1)) > 0:
                body = await asyncio.wait_for(
                    self._reader.readexactly(int(match.group(1))), timeout=timeout
                )

            message = (head + body).decode("utf-8", errors="replace")
            _LOGGER.debug("RX:\n%s", message)
            return message

        except asyncio.TimeoutError:
            raise TimeoutError("Timeout receiving SIP message")
        except asyncio.IncompleteReadError:
            raise ConnectionError("Connection closed")

    def _build_register(self, with_auth: bool = False) -> str:
        """Build REGISTER request."""
        uri = f"sip:{self.domain}"
        from_uri = f"<sip:{self.sip_account}>"
        to_uri = from_uri
        contact_uri = f"<sip:{self.username}@{self.gateway_ip}:{self.local_port};transport=tls;maddr={self.gateway_ip}>"

        lines = [
            f"REGISTER {uri} SIP/2.0",
            f"Via: SIP/2.0/TLS {self.gateway_ip}:{self.local_port};branch={self._generate_branch()}",
            f"From: {from_uri};tag={self.from_tag}",
            f"To: {to_uri}",
            f"Call-ID: {self.call_id}",
            f"CSeq: {self.cseq} REGISTER",
            f"Contact: {contact_uri}",
            "Expires: 300",
            "Max-Forwards: 70",
            "User-Agent: HomeAssistant-DoorEntry/0.1.0",
        ]

        # Add authentication if we have nonce
        if with_auth and self._auth_nonce and self._auth_realm:
            response = self._calculate_digest_response(
                "REGISTER",
                uri,
                self._auth_nonce,
                self._auth_realm,
            )

            auth = (
                f'Digest username="{self.username}", '
                f'realm="{self._auth_realm}", '
                f'nonce="{self._auth_nonce}", '
                f'uri="{uri}", '
                f'response="{response}", '
                f'algorithm=MD5'
            )
            lines.append(f"Authorization: {auth}")

        lines.extend([
            "Content-Length: 0",
            "",
            "",
        ])

        return "\r\n".join(lines)

    def _build_message(self, target: str, body: str) -> str:
        """Build MESSAGE request."""
        # Ensure target has sip: prefix
        if not target.startswith("sip:"):
            target = f"sip:{target}"
        if "@" not in target:
            target = f"{target}@{self.domain}"

        uri = target
        from_uri = f"<sip:{self.sip_account}>"
        to_uri = f"<{target}>"

        self.cseq += 1

        lines = [
            f"MESSAGE {uri} SIP/2.0",
            f"Via: SIP/2.0/TLS {self.gateway_ip}:{self.local_port};branch={self._generate_branch()}",
            f"From: {from_uri};tag={self.from_tag}",
            f"To: {to_uri}",
            f"Call-ID: {self.call_id}",
            f"CSeq: {self.cseq} MESSAGE",
            "Max-Forwards: 70",
            "User-Agent: HomeAssistant-DoorEntry/0.1.0",
            "Content-Type: text/plain",
            f"Content-Length: {len(body)}",
            "",
            body,
        ]

        return "\r\n".join(lines)

    async def register(self) -> bool:
        """Register to SIP server."""
        try:
            # Connect
            await self._connect()

            # Send REGISTER without auth
            _LOGGER.info("Sending REGISTER (without auth)")
            await self._send(self._build_register(with_auth=False))

            # Receive response (expect 401 Unauthorized)
            response = await self._receive()

            # Parse 401 response for auth challenge
            if _status_code(response) in (401, 407):
                _LOGGER.info("Received auth challenge")

                # Extract nonce and realm
                www_auth = re.search(
                    r'WWW-Authenticate:\s*Digest\s+(.+)',
                    response,
                    re.IGNORECASE,
                )
                if not www_auth:
                    www_auth = re.search(
                        r'Proxy-Authenticate:\s*Digest\s+(.+)',
                        response,
                        re.IGNORECASE,
                    )

                if www_auth:
                    auth_params = www_auth.group(1)

                    # Parse nonce
                    nonce_match = re.search(r'nonce="([^"]+)"', auth_params)
                    if nonce_match:
                        self._auth_nonce = nonce_match.group(1)

                    # Parse realm
                    realm_match = re.search(r'realm="([^"]+)"', auth_params)
                    if realm_match:
                        self._auth_realm = realm_match.group(1)

                    _LOGGER.info("Auth realm: %s", self._auth_realm)

                # Send REGISTER with auth
                self.cseq += 1
                _LOGGER.info("Sending REGISTER (with auth)")
                await self._send(self._build_register(with_auth=True))

                # Receive response
                response = await self._receive()

            if 200 <= _status_code(response) < 300:
                _LOGGER.info("SIP registration successful")
                self._registered = True
                return True
            else:
                _LOGGER.error("SIP registration failed: %s", response.split("\n")[0])
                return False

        except Exception as err:
            _LOGGER.error("SIP registration error: %s", err)
            return False

    async def send_message(self, target: str, body: str) -> bool:
        """Send SIP MESSAGE."""
        if not self._registered:
            _LOGGER.error("Not registered")
            return False

        try:
            _LOGGER.debug("Sending MESSAGE to %s: %s", target, body)

            # Send MESSAGE
            await self._send(self._build_message(target, body))

            # Receive response
            response = await self._receive(timeout=2.0)

            if 200 <= _status_code(response) < 300:
                _LOGGER.debug("MESSAGE sent successfully")
                return True
            else:
                _LOGGER.warning("MESSAGE failed: %s", response.split("\n")[0])
                return False

        except Exception as err:
            _LOGGER.error("Failed to send MESSAGE: %s", err)
            return False

    async def send_door_command(self, where: str) -> bool:
        """Send door open command (two frames)."""
        target = f"c300x@{self.domain}"

        # Frame 1: press
        success1 = await self.send_message(target, f"*8*19*{where}##")
        if not success1:
            return False

        # Small delay
        await asyncio.sleep(0.1)

        # Frame 2: release
        success2 = await self.send_message(target, f"*8*20*{where}##")
        return success2

    async def send_light_command(self, where: str, turn_on: bool) -> bool:
        """Send staircase light command."""
        target = f"c300x@{self.domain}"

        # Same frames for on/off (toggle)
        await self.send_message(target, f"*8*21*{where}##")
        await asyncio.sleep(0.1)
        await self.send_message(target, f"*8*22*{where}##")

        return True

    async def close(self) -> None:
        """Close SIP client."""
        _LOGGER.info("Closing SIP client")
        self._registered = False

        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
            self._writer = None
            self._reader = None


# Export as SipClient
SipClient = SipClientPure
PURE_AVAILABLE = True

__all__ = ["SipClient", "PURE_AVAILABLE"]
