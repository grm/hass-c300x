"""Minimal SIP/TLS client for the C300X gateway (no Home Assistant imports).

Verified on a real gateway (docs/field_tests.md):
- TLS on 5061 with the client certificate signed by the Eliot cloud. The server certificate CN is
  ``sip:<GatewayId>.bs.iotleg.com`` without SAN, so hostname checking is replaced by a CN check.
- REGISTER gets ``200 Registration successful`` without Digest challenge.
- MESSAGE ``*8*19*<where>##`` then ``*8*20*<where>##`` to ``c300x@<domain>`` opens the door.
"""
from __future__ import annotations

import asyncio
import os
import re
import ssl
import tempfile
import uuid
from dataclasses import dataclass, field

SIP_PORT = 5061
USER_AGENT = "HomeAssistant-DoorEntry"
TARGET_USER = "c300x"


class SipError(Exception):
    """SIP transport or protocol failure."""


class SipBusy(SipError):
    """The door station is busy (it is ringing or already in a call)."""


def make_ssl_context(cert_pem: str, key_pem: str, ca_pem: str) -> ssl.SSLContext:
    """Build the client TLS context. Blocking (file I/O): run it in an executor."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    # The gateway only offers RSA key exchange (AES256-GCM-SHA384), which Python's default cipher
    # list excludes since 3.10: the handshake fails with OpenSSL 3 (Home Assistant)
    ctx.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20:AES256-GCM-SHA384:AES128-GCM-SHA256")
    ctx.load_verify_locations(cadata=ca_pem)
    # load_cert_chain only accepts paths: the files only live for the duration of the call
    with tempfile.TemporaryDirectory() as tmp:
        cert_file, key_file = os.path.join(tmp, "c.pem"), os.path.join(tmp, "k.pem")
        with open(cert_file, "w") as f:
            f.write(cert_pem)
        with open(os.open(key_file, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
            f.write(key_pem)
        ctx.load_cert_chain(cert_file, key_file)
    return ctx


def _tag() -> str:
    return uuid.uuid4().hex[:10]


def _branch() -> str:
    return "z9hG4bK" + uuid.uuid4().hex[:16]


@dataclass
class SipMessage:
    """Parsed SIP request or response."""

    first_line: str
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: str = ""

    @classmethod
    def parse(cls, raw: str) -> SipMessage:
        head, _, body = raw.partition("\r\n\r\n")
        lines = head.split("\r\n")
        headers = []
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers.append((name.strip(), value.strip()))
        return cls(lines[0], headers, body)

    @property
    def is_response(self) -> bool:
        return self.first_line.startswith("SIP/2.0")

    @property
    def status(self) -> int:
        match = re.match(r"SIP/2\.0\s+(\d{3})", self.first_line)
        return int(match.group(1)) if match else 0

    @property
    def method(self) -> str:
        if self.is_response:
            return (self.header("CSeq") or " ").split()[-1]
        return self.first_line.split(" ", 1)[0]

    _COMPACT = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact", "l": "content-length"}

    def all(self, name: str) -> list[str]:
        name = name.lower()
        return [v for k, v in self.headers if self._COMPACT.get(k.lower(), k.lower()) == name]

    def header(self, name: str) -> str | None:
        values = self.all(name)
        return values[0] if values else None


class SipConnection:
    """One TLS connection registered on the gateway."""

    def __init__(self, host: str, gateway_id: str, sip_account: str, ssl_context: ssl.SSLContext) -> None:
        self.host = host
        self.domain = f"{gateway_id}.bs.iotleg.com"
        self.sip_account = sip_account
        self.username = sip_account.split("@", 1)[0]
        self.target = f"sip:{TARGET_USER}@{self.domain}"
        self._ssl = ssl_context
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.local_ip = ""
        self.local_port = 0
        self._call_id = str(uuid.uuid4())
        self._from_tag = _tag()
        self._cseq = 0
        # Requests received while waiting for a response, consumed by the call dialog
        self.incoming: asyncio.Queue[SipMessage] = asyncio.Queue()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self, timeout: float = 10) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, SIP_PORT, ssl=self._ssl, server_hostname=self.domain),
            timeout,
        )
        peer = self._writer.get_extra_info("peercert") or {}
        cn = next((v for rdn in peer.get("subject", ()) for k, v in rdn if k == "commonName"), None)
        if cn != f"sip:{self.domain}":
            await self.close()
            raise SipError(f"Unexpected gateway certificate CN: {cn}")
        self.local_ip, self.local_port = self._writer.get_extra_info("sockname")[:2]

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ssl.SSLError):
                pass

    # -- framing ---------------------------------------------------------------------------------

    async def send(self, raw: str) -> None:
        if not self.connected:
            raise SipError("Not connected")
        self._writer.write(raw.encode())
        await self._writer.drain()

    async def receive(self, timeout: float) -> SipMessage:
        if not self._reader:
            raise SipError("Not connected")
        try:
            while True:
                head = await asyncio.wait_for(self._reader.readuntil(b"\r\n\r\n"), timeout)
                if head.strip():  # skip CRLF keep-alives (RFC 5626)
                    break
            length = re.search(rb"^(?:Content-Length|l)\s*:\s*(\d+)", head, re.IGNORECASE | re.MULTILINE)
            body = b""
            if length and int(length.group(1)):
                body = await asyncio.wait_for(self._reader.readexactly(int(length.group(1))), timeout)
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError) as err:
            await self.close()
            raise SipError("Connection closed by the gateway") from err
        return SipMessage.parse((head + body).decode("utf-8", errors="replace"))

    # -- requests --------------------------------------------------------------------------------

    def contact(self) -> str:
        return f"<sip:{self.username}@{self.local_ip}:{self.local_port};transport=tls>"

    def build_request(
        self,
        method: str,
        uri: str,
        *,
        to: str | None = None,
        call_id: str | None = None,
        from_tag: str | None = None,
        cseq: int | None = None,
        branch: str | None = None,
        extra: list[str] | tuple[str, ...] = (),
        body: str = "",
    ) -> str:
        if cseq is None:
            self._cseq += 1
            cseq = self._cseq
        lines = [
            f"{method} {uri} SIP/2.0",
            f"Via: SIP/2.0/TLS {self.local_ip}:{self.local_port};branch={branch or _branch()};rport",
            "Max-Forwards: 70",
            f"From: <sip:{self.sip_account}>;tag={from_tag or self._from_tag}",
            f"To: {to or f'<{uri}>'}",
            f"Call-ID: {call_id or self._call_id}",
            f"CSeq: {cseq} {method}",
            *extra,
            f"User-Agent: {USER_AGENT}",
            f"Content-Length: {len(body.encode())}",
            "",
            body,
        ]
        return "\r\n".join(lines)

    @staticmethod
    def build_response(request: SipMessage, code: int = 200, reason: str = "OK") -> str:
        lines = [f"SIP/2.0 {code} {reason}"]
        for name in ("Via", "From", "To", "Call-ID", "CSeq"):
            lines += [f"{name}: {v}" for v in request.all(name)]
        return "\r\n".join([*lines, "Content-Length: 0", "", ""])

    async def wait_response(self, method: str, timeout: float, provisional: bool = False) -> SipMessage:
        """Next response for ``method``; incoming requests are queued for the dialog owner."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise SipError(f"Timeout waiting for {method} response")
            try:
                msg = await self.receive(remaining)
            except TimeoutError as err:
                raise SipError(f"Timeout waiting for {method} response") from err
            if not msg.is_response:
                await self.incoming.put(msg)
                continue
            if msg.method != method or (msg.status < 200 and not provisional):
                continue
            return msg

    async def register(self, expires: int = 300) -> None:
        uri = f"sip:{self.domain}"
        await self.send(self.build_request(
            "REGISTER", uri, to=f"<sip:{self.sip_account}>",
            extra=[f"Contact: {self.contact()}", f"Expires: {expires}"],
        ))
        resp = await self.wait_response("REGISTER", 10)
        # The reason phrase is "Registration successful", not "OK": test the code only
        if not 200 <= resp.status < 300:
            raise SipError(f"REGISTER refused: {resp.first_line}")

    async def message(self, body: str) -> None:
        await self.send(self.build_request(
            "MESSAGE", self.target, call_id=str(uuid.uuid4()), from_tag=_tag(),
            extra=["Content-Type: text/plain"], body=body,
        ))
        resp = await self.wait_response("MESSAGE", 5)
        if not 200 <= resp.status < 300:
            raise SipError(f"MESSAGE refused: {resp.first_line}")


async def open_connection(host: str, gateway_id: str, sip_account: str, ssl_context: ssl.SSLContext) -> SipConnection:
    """Connect and register."""
    conn = SipConnection(host, gateway_id, sip_account, ssl_context)
    await conn.connect()
    try:
        await conn.register()
    except BaseException:
        await conn.close()
        raise
    return conn


async def send_frames(
    host: str, gateway_id: str, sip_account: str, ssl_context: ssl.SSLContext, frames: list[str]
) -> None:
    """Send OpenWebNet frames as SIP MESSAGEs on a short-lived connection."""
    conn = await open_connection(host, gateway_id, sip_account, ssl_context)
    try:
        for i, frame in enumerate(frames):
            if i:
                await asyncio.sleep(0.1)
            await conn.message(frame)
    finally:
        await conn.close()
