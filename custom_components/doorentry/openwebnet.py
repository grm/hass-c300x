"""OpenWebNet client for the C300X gateway (TCP 20000, no Home Assistant imports).

The gateway asks for HMAC-SHA256 authentication (``*98*2##``) and accepts the ``PswOpen``
password from the Eliot cloud. Commands on this port are refused (``*#*0##``): it is only used to
monitor events, e.g. the doorbell (docs/field_tests.md § 2 and § 6).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

PORT = 20000
ACK = "*#*1##"
NACK = "*#*0##"
CMD_SESSION = "*99*0##"
MON_SESSION = "*99*1##"
HMAC_SHA256 = "*98*2##"
RECONNECT_DELAY = 30

M32 = 0xFFFFFFFF

# *8*1#<mode>#4#<address>*10## : mode 1 = door station ringing, 5 = our own "auto-on" call
RING = re.compile(r"^\*8\*1#1#\d+#(\d+)\*\d+##$")
# *8*3#<mode>#4*<x>## : end of call (420 = unanswered ring)
CALL_END = re.compile(r"^\*8\*3#(\d+)#\d+\*\d+##$")
# *#8**35*<state>*0*0## : global call state, 0 = idle
CALL_STATE = re.compile(r"^\*#8\*\*35\*(\d+)\*")
DOOR = re.compile(r"^\*8\*19\*(\d+)##$")


class AuthError(Exception):
    """Wrong OpenWebNet password."""


def own_calc_pass(password: str, nonce: str) -> str:
    """OPEN algorithm (p227f0/AbstractC3676b.java), for gateways without HMAC."""
    pwd = int(password) & M32
    x, started = 0, False
    for c in nonce:
        if c != "0" and not started:
            x, started = pwd, True
        if c == "1":
            x = ((x & 0xFFFFFF80) >> 7) | (x << 25)
        elif c == "2":
            x = ((x & 0xFFFFFFF0) >> 4) | (x << 28)
        elif c == "3":
            x = ((x & 0xFFFFFFF8) >> 3) | (x << 29)
        elif c == "4":
            x = (x << 1) | (x >> 31)
        elif c == "5":
            x = (x << 5) | (x >> 27)
        elif c == "6":
            x = (x << 12) | (x >> 20)
        elif c == "7":
            x = (x & 0xFF00) | ((x & 0xFF) << 24) | ((x & 0xFF0000) >> 16) | ((x & 0xFF000000) >> 8)
        elif c == "8":
            x = ((x & 0xFFFF) << 16) | (x >> 24) | ((x & 0xFF0000) >> 8)
        elif c == "9":
            x = ~x
        x &= M32
    return str(x)


def _sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def dec2hex(dec_str: str) -> str:
    """Pairs of decimal digits → hex digits."""
    return "".join(format(int(dec_str[i:i + 2]), "x") for i in range(0, len(dec_str), 2))


def hex2dec(hex_str: str) -> str:
    """Each hex digit → two decimal digits."""
    return "".join(f"{int(c, 16):02d}" for c in hex_str)


class OwnHmac:
    """OpenWebNet HMAC-SHA256 (p227f0/C3675a)."""

    CONSTS = "736F70653E636F70653E"

    def __init__(self, ra_dec: str, password: str) -> None:
        self.ra = dec2hex(ra_dec)
        self.kab = _sha256_hex(password)
        self.rb = _sha256_hex(str(uuid.uuid4()))
        self.rb_dec = hex2dec(self.rb)
        self.digest_dec = hex2dec(_sha256_hex(self.ra + self.rb + self.CONSTS + self.kab))

    def check(self, confirm_dec: str) -> bool:
        return confirm_dec == hex2dec(_sha256_hex(self.ra + self.rb + self.kab))


class OpenWebNetConnection:
    """One authenticated OpenWebNet session."""

    def __init__(self, host: str, password: str, port: int = PORT) -> None:
        self.host = host
        self.port = port
        self.password = password
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._buffer = b""

    async def connect(self, session: str = CMD_SESSION, timeout: float = 10) -> None:
        self._reader, self._writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), timeout)
        sock = self._writer.get_extra_info("socket")
        if sock is not None:
            # Monitor sessions can stay silent for hours: detect a dead gateway
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        try:
            await self._authenticate(session)
        except BaseException:
            await self.close()
            raise

    async def _authenticate(self, session: str) -> None:
        if await self.recv() != ACK:
            raise ConnectionError("Unexpected welcome frame")
        await self.send(session)
        response = await self.recv()
        if response == ACK:
            return
        if response == NACK:
            raise AuthError("Session refused")
        if response == HMAC_SHA256:
            await self.send(ACK)
            match = re.match(r"\*#(\d+)##", await self.recv())
            if not match:
                raise ConnectionError("Invalid HMAC nonce")
            hmac = OwnHmac(match.group(1), self.password)
            await self.send(f"*#{hmac.rb_dec}*{hmac.digest_dec}##")
            confirm = await self.recv()
            match = re.match(r"\*#(\d+)##", confirm)
            if not match or not hmac.check(match.group(1)):
                raise AuthError("HMAC authentication failed")
            await self.send(ACK)
            return
        match = re.match(r"\*#(\d+)##", response)
        if not match:
            raise ConnectionError(f"Unexpected authentication frame {response}")
        await self.send(f"*#{own_calc_pass(self.password, match.group(1))}##")
        if await self.recv() != ACK:
            raise AuthError("OPEN authentication failed")

    async def send(self, frame: str) -> None:
        self._writer.write(frame.encode())
        await self._writer.drain()

    async def recv(self, timeout: float | None = 10) -> str:
        while b"##" not in self._buffer:
            chunk = await asyncio.wait_for(self._reader.read(4096), timeout)
            if not chunk:
                raise ConnectionError("Connection closed by the gateway")
            self._buffer += chunk
        idx = self._buffer.index(b"##") + 2
        frame, self._buffer = self._buffer[:idx].decode(errors="replace"), self._buffer[idx:]
        return frame

    async def close(self) -> None:
        writer, self._writer = self._writer, None
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def test_connection(host: str, password: str) -> None:
    """Raise if the gateway cannot be reached or the password is wrong."""
    conn = OpenWebNetConnection(host, password)
    await conn.connect(CMD_SESSION)
    await conn.close()


@dataclass
class DoorEvent:
    """Decoded monitor frame."""

    kind: str  # "ring", "ring_end", "door_open"
    address: str | None
    frame: str


def parse_frame(frame: str) -> DoorEvent | None:
    if m := RING.match(frame):
        return DoorEvent("ring", m.group(1), frame)
    if (m := CALL_END.match(frame)) and m.group(1) == "1":
        return DoorEvent("ring_end", None, frame)
    if (m := CALL_STATE.match(frame)) and m.group(1) == "0":
        return DoorEvent("ring_end", None, frame)
    if m := DOOR.match(frame):
        return DoorEvent("door_open", m.group(1), frame)
    return None


class OpenWebNetMonitor:
    """Monitor session with automatic reconnection."""

    def __init__(self, host: str, password: str) -> None:
        self.host = host
        self.password = password
        self.connected = False
        self._listeners: list[Callable[[DoorEvent], None]] = []
        self._state_listeners: list[Callable[[], None]] = []
        self._task: asyncio.Task | None = None

    def add_listener(self, listener: Callable[[DoorEvent], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def add_state_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._state_listeners.append(listener)
        return lambda: self._state_listeners.remove(listener)

    def _set_connected(self, connected: bool) -> None:
        if connected != self.connected:
            self.connected = connected
            for listener in list(self._state_listeners):
                listener()

    def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            conn = OpenWebNetConnection(self.host, self.password)
            try:
                await conn.connect(MON_SESSION)
                _LOGGER.debug("OpenWebNet monitor connected")
                self._set_connected(True)
                while True:
                    frame = await conn.recv(timeout=None)
                    _LOGGER.debug("OWN %s", frame)
                    event = parse_frame(frame)
                    if event:
                        for listener in list(self._listeners):
                            listener(event)
            except asyncio.CancelledError:
                raise
            except (TimeoutError, OSError, ConnectionError, AuthError) as err:
                _LOGGER.warning("OpenWebNet monitor disconnected (%s), retrying in %d s", err, RECONNECT_DELAY)
            finally:
                await conn.close()
                self._set_connected(False)
            await asyncio.sleep(RECONNECT_DELAY)
