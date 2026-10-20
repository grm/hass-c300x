"""Gateway hub: door commands, one shared call at a time, ring events (no Home Assistant imports)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from collections.abc import AsyncIterator, Callable

from .call import DoorCall
from .media import JpegDecoder, PcmSource
from .openwebnet import OpenWebNetMonitor
from .sip import SipConnection, open_connection, send_frames

_LOGGER = logging.getLogger(__name__)

# A call is kept this long after its last user leaves, so a snapshot following a live view
# (or two quick TTS messages) does not ring up the door station twice
CALL_LINGER = 5.0
TALK_DELAY = 1.5
MAX_CALL_DURATION = 180.0


class DoorGateway:
    """Everything that talks to one C300X gateway."""

    def __init__(
        self,
        host: str,
        gateway_id: str,
        sip_account: str,
        ssl_context: ssl.SSLContext,
        password: str,
        devaddr: str,
        ffmpeg: str,
    ) -> None:
        self.host = host
        self.gateway_id = gateway_id
        self.sip_account = sip_account
        self.devaddr = devaddr
        self._ssl = ssl_context
        self._ffmpeg = ffmpeg
        self.monitor = OpenWebNetMonitor(host, password)

        self._lock = asyncio.Lock()
        self._conn: SipConnection | None = None
        self._call: DoorCall | None = None
        self._decoder: JpegDecoder | None = None
        self._users = 0
        self._linger: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None
        self.last_image: bytes | None = None
        self._call_listeners: list[Callable[[], None]] = []

    # -- commands --------------------------------------------------------------------------------

    async def open_door(self, where: str) -> None:
        await send_frames(self.host, self.gateway_id, self.sip_account, self._ssl,
                          [f"*8*19*{where}##", f"*8*20*{where}##"])

    async def stair_light(self, where: str) -> None:
        await send_frames(self.host, self.gateway_id, self.sip_account, self._ssl,
                          [f"*8*21*{where}##", f"*8*22*{where}##"])

    async def test_sip(self) -> None:
        conn = await open_connection(self.host, self.gateway_id, self.sip_account, self._ssl)
        await conn.close()

    # -- shared call -----------------------------------------------------------------------------

    @property
    def in_call(self) -> bool:
        return self._call is not None and not self._call.ended.is_set()

    def add_call_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._call_listeners.append(listener)
        return lambda: self._call_listeners.remove(listener)

    def _notify(self) -> None:
        for listener in list(self._call_listeners):
            listener()

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[DoorCall]:
        """Use the call to the door station, starting it if needed.

        Raises :class:`~.sip.SipBusy` while the door station is ringing.
        """
        async with self._lock:
            if self._linger:
                self._linger.cancel()
                self._linger = None
            if not self.in_call:
                await self._start_call()
            self._users += 1
            call = self._call
        try:
            yield call
        finally:
            self._users -= 1
            if self._users == 0 and not call.ended.is_set():
                self._linger = asyncio.create_task(self._hangup_after(CALL_LINGER))

    async def _start_call(self) -> None:
        conn = await open_connection(self.host, self.gateway_id, self.sip_account, self._ssl)
        call = DoorCall(conn, self.devaddr)
        try:
            await call.start()
        except BaseException:
            await conn.close()
            raise
        decoder = JpegDecoder(self._ffmpeg)
        await decoder.start()
        call.add_video_listener(decoder.feed)
        self._conn, self._call, self._decoder = conn, call, decoder
        self._watchdog = asyncio.create_task(self._watch(call, decoder, conn))
        _LOGGER.debug("Call to door station %s established", self.devaddr)
        self._notify()

    async def _watch(self, call: DoorCall, decoder: JpegDecoder, conn: SipConnection) -> None:
        try:
            # The first keyframe can be lost while the decoder starts: ask once more
            await asyncio.sleep(1)
            if decoder.latest is None:
                await call.request_keyframe()
            await asyncio.wait_for(call.ended.wait(), MAX_CALL_DURATION)
        except TimeoutError:
            _LOGGER.debug("Call reached %.0f s, hanging up", MAX_CALL_DURATION)
        except asyncio.CancelledError:
            pass
        finally:
            await call.hangup()
            await decoder.stop()
            await conn.close()
            if decoder.latest:
                self.last_image = decoder.latest
            if self._call is call:
                self._call = self._decoder = self._conn = None
            self._notify()

    async def _hangup_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self.hangup()

    async def hangup(self) -> None:
        async with self._lock:
            watchdog, self._watchdog = self._watchdog, None
            if self._linger and self._linger is not asyncio.current_task():
                self._linger.cancel()
            self._linger = None
            if watchdog:
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)

    # -- video -----------------------------------------------------------------------------------

    def current_image(self) -> bytes | None:
        if self._decoder and self._decoder.latest:
            return self._decoder.latest
        return self.last_image

    async def next_image(self, timeout: float = 10) -> bytes | None:
        """Wait for a new frame of the current call, None if the call is over."""
        decoder = self._decoder
        if not decoder or not self.in_call:
            return None
        previous = decoder.latest
        async with decoder.frame_ready:
            try:
                await asyncio.wait_for(
                    decoder.frame_ready.wait_for(lambda: decoder.latest is not previous or not self.in_call),
                    timeout,
                )
            except TimeoutError:
                return None
        return decoder.latest if self.in_call else None

    async def snapshot(self, timeout: float = 15) -> bytes | None:
        """Fresh image: calls the door station if no call is running."""
        async with self.session():
            if self._decoder and self._decoder.latest:
                return self._decoder.latest
            return await self.next_image(timeout)

    # -- audio -----------------------------------------------------------------------------------

    async def talk(self, source: str, encoder_factory: Callable[[], object], gain_db: float = 18) -> float:
        """Play an ffmpeg-readable source on the door station speaker. Returns seconds sent."""
        fresh = not self.in_call
        async with self.session() as call:
            if fresh:
                await asyncio.sleep(TALK_DELAY)
            encoder = encoder_factory()
            try:
                sent = await call.send_audio(PcmSource(self._ffmpeg, source, gain_db).frames(), encoder.encode)
            finally:
                encoder.close()
        return sent * 0.02

    async def close(self) -> None:
        await self.monitor.stop()
        await self.hangup()
