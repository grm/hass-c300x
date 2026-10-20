"""Video/audio call to the door station (no Home Assistant imports).

Port of the field-tested ``probe_call.py``: INVITE with ``a=DEVADDR``, mandatory SRTP (SDES),
H264 video from the gateway, Speex 8 kHz both ways. See docs/field_tests.md § 4.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import struct
import time
import uuid
from collections.abc import AsyncIterator, Callable

from .h264 import NAL_IDR, NAL_PPS, NAL_SPS, H264Depacketizer, nal_type
from .sip import SipBusy, SipConnection, SipError, SipMessage
from .srtp import SrtpContext, new_inline_key

_LOGGER = logging.getLogger(__name__)

SPEEX_PT = 98
SPEEX_FRAME_BYTES = 320  # 160 samples s16le = 20 ms at 8 kHz
MAX_GOP_NALS = 600
VFU_BODY = (
    '<?xml version="1.0" encoding="utf-8" ?><media_control><vc_primitive><to_encoder>'
    "<picture_fast_update></picture_fast_update></to_encoder></vc_primitive></media_control>"
)


def build_offer(local_ip: str, devaddr: str, audio_port: int, video_port: int, audio_key: str, video_key: str) -> str:
    sess = int(time.time())
    lines = [
        "v=0",
        f"o=- {sess} {sess} IN IP4 {local_ip}",
        "s=Talk",
        f"c=IN IP4 {local_ip}",
        "t=0 0",
        f"a=DEVADDR:{devaddr}",  # session attribute: which door station camera
        f"m=audio {audio_port} RTP/SAVP {SPEEX_PT} 101",
        f"a=rtpmap:{SPEEX_PT} speex/8000",
        "a=rtpmap:101 telephone-event/8000",
        "a=sendrecv",
        f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{audio_key}",
        f"m=video {video_port} RTP/SAVP 99",
        "a=rtpmap:99 H264/90000",
        "a=fmtp:99 profile-level-id=42801F;packetization-mode=1",
        "a=recvonly",
        f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{video_key}",
    ]
    return "\r\n".join(lines) + "\r\n"


def parse_answer(sdp: str) -> tuple[str | None, dict[str, dict]]:
    """Connection address and, per media: port, first payload type, SRTP key."""
    conn = re.search(r"^c=IN IP4 (\S+)", sdp, re.MULTILINE)
    media = {}
    for part in re.split(r"(?m)^(?=m=)", sdp)[1:]:
        m = re.match(r"m=(\w+) (\d+) \S+ ([\d ]+)", part)
        if not m:
            continue
        crypto = re.search(r"^a=crypto:\d+ \S+ inline:(\S+)", part, re.MULTILINE)
        media[m.group(1)] = {
            "port": int(m.group(2)),
            "pt": int(m.group(3).split()[0]),
            "key": crypto.group(1) if crypto else None,
        }
    return (conn.group(1) if conn else None), media


class _Rtp(asyncio.DatagramProtocol):
    def __init__(self, on_packet: Callable[[bytes], None] | None) -> None:
        self.on_packet = on_packet
        self.srtp: SrtpContext | None = None
        self.transport: asyncio.DatagramTransport | None = None
        self.packets = 0
        self.received = 0
        self.auth_failures = 0

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.received += 1
        if len(data) < 12 or data[0] >> 6 != 2:
            return  # STUN or garbage
        if self.srtp is None or self.on_packet is None:
            return
        plain = self.srtp.unprotect(data)
        if plain is None:
            self.auth_failures += 1
            return
        self.packets += 1
        self.on_packet(plain)


async def _bind_pair(loop: asyncio.AbstractEventLoop, proto: _Rtp) -> tuple[list[asyncio.BaseTransport], int]:
    """Bind an even RTP port and RTCP on port+1 (the gateway does not use rtcp-mux)."""
    for _ in range(50):
        port = random.randrange(40000, 60000, 2)
        try:
            rtp, _ = await loop.create_datagram_endpoint(lambda: proto, local_addr=("0.0.0.0", port))
        except OSError:
            continue
        try:
            rtcp, _ = await loop.create_datagram_endpoint(lambda: _Rtp(None), local_addr=("0.0.0.0", port + 1))
        except OSError:
            rtp.close()
            continue
        return [rtp, rtcp], port
    raise SipError("No free UDP port pair for RTP")


class DoorCall:
    """One established call. Use :meth:`start`, then :meth:`hangup`."""

    def __init__(self, conn: SipConnection, devaddr: str) -> None:
        self.conn = conn
        self.devaddr = devaddr
        self.ended = asyncio.Event()
        self._depacketizer = H264Depacketizer()
        self._video = _Rtp(self._on_video)
        self._audio = _Rtp(None)
        self._transports: list[asyncio.BaseTransport] = []
        self._audio_key = new_inline_key()
        self._video_key = new_inline_key()
        self._audio_dst: tuple[str, int] | None = None
        self._audio_pt = SPEEX_PT
        self._call_id = str(uuid.uuid4())
        self._from_tag = uuid.uuid4().hex[:10]
        self._cseq = 1
        self._to = f"<{conn.target}>"
        self._remote_uri = conn.target
        self._routes: list[str] = []
        self._sip_task: asyncio.Task | None = None
        # SPS/PPS + frames since the last IDR, replayed to late listeners so they decode right away
        self._params: dict[int, bytes] = {}
        self._gop: list[bytes] | None = None
        # listener → has it received a keyframe yet (P-frames without reference decode as garbage)
        self._nal_listeners: dict[Callable[[bytes], None], bool] = {}

    # -- media -----------------------------------------------------------------------------------

    def _on_video(self, packet: bytes) -> None:
        for nal in self._depacketizer.push(packet):
            kind = nal_type(nal)
            if kind in (NAL_SPS, NAL_PPS):
                self._params[kind] = nal
            elif kind == NAL_IDR:
                self._gop = [nal]
            elif self._gop is not None and len(self._gop) < MAX_GOP_NALS:
                self._gop.append(nal)
            else:
                self._gop = None
            keyframe = kind == NAL_IDR and len(self._params) == 2
            for listener, synced in list(self._nal_listeners.items()):
                if synced:
                    listener(nal)
                elif keyframe:
                    for param in self._params.values():
                        listener(param)
                    listener(nal)
                    self._nal_listeners[listener] = True

    def add_video_listener(self, listener: Callable[[bytes], None]) -> Callable[[], None]:
        """Receive Annex-B NAL units, starting with a keyframe."""
        synced = len(self._params) == 2 and self._gop is not None
        if synced:
            for nal in [*self._params.values(), *self._gop]:
                listener(nal)
        self._nal_listeners[listener] = synced
        return lambda: self._nal_listeners.pop(listener, None)

    @property
    def can_talk(self) -> bool:
        return self._audio_dst is not None

    async def send_audio(self, pcm_frames: AsyncIterator[bytes], encode: Callable[[bytes], bytes]) -> int:
        """Send 20 ms frames of 8 kHz s16le PCM, encoded with ``encode`` (Speex), paced on our clock.

        SRTP uses *our* key (the one of our offer). The door station is half-duplex: its
        microphone is muted while it receives speech.
        """
        if not self._audio_dst:
            raise SipError("No audio negotiated")
        transport = self._transports[0]
        ctx = SrtpContext(self._audio_key)
        ssrc = int.from_bytes(os.urandom(4), "big")
        seq = int.from_bytes(os.urandom(2), "big")
        ts = int.from_bytes(os.urandom(4), "big")
        sent = 0
        started = time.monotonic()
        async for pcm in pcm_frames:
            if self.ended.is_set():
                break
            marker = 0x80 if sent == 0 else 0
            header = struct.pack("!BBHII", 0x80, marker | self._audio_pt, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
            # Bursts overflow the door station jitter buffer (clicks)
            delay = started + sent * 0.02 - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            transport.sendto(ctx.protect(header + encode(pcm)), self._audio_dst)
            seq += 1
            ts += 160
            sent += 1
        return sent

    # -- signalling ------------------------------------------------------------------------------

    def _request(
        self, method: str, uri: str, cseq: int, extra: tuple[str, ...] | list[str] = (), body: str = "",
        branch: str | None = None,
    ) -> str:
        return self.conn.build_request(
            method, uri, to=self._to, call_id=self._call_id, from_tag=self._from_tag,
            cseq=cseq, branch=branch, extra=[*(f"Route: {r}" for r in self._routes), *extra], body=body,
        )

    async def start(self, timeout: float = 20) -> None:
        loop = asyncio.get_running_loop()
        audio_t, audio_port = await _bind_pair(loop, self._audio)
        video_t, video_port = await _bind_pair(loop, self._video)
        self._transports = [*audio_t, *video_t]
        try:
            await self._invite(audio_port, video_port, timeout)
        except BaseException:
            self._close_media()
            raise
        self._sip_task = asyncio.create_task(self._sip_loop())
        # SPS/PPS are only sent in-band: ask for a keyframe right away
        await self.request_keyframe()

    async def _invite(self, audio_port: int, video_port: int, timeout: float) -> None:
        offer = build_offer(self.conn.local_ip, self.devaddr, audio_port, video_port, self._audio_key, self._video_key)
        branch = "z9hG4bK" + uuid.uuid4().hex[:16]
        await self.conn.send(self._request(
            "INVITE", self.conn.target, self._cseq, branch=branch,
            extra=[
                f"Contact: {self.conn.contact()}",
                "Allow: INVITE, ACK, CANCEL, OPTIONS, BYE, MESSAGE, INFO, UPDATE",
                "Content-Type: application/sdp",
            ],
            body=offer,
        ))
        resp = await self.conn.wait_response("INVITE", timeout)
        if resp.status >= 300:
            # ACK for a non-2xx final response reuses the INVITE branch
            self._to = resp.header("To") or self._to
            await self.conn.send(self._request("ACK", self.conn.target, self._cseq, branch=branch))
            if resp.status in (486, 600):
                raise SipBusy(resp.first_line)
            raise SipError(f"INVITE refused: {resp.first_line}")

        self._to = resp.header("To") or self._to
        contact = resp.header("Contact")
        if contact:
            m = re.search(r"<([^>]+)>", contact)
            self._remote_uri = m.group(1) if m else contact.split(";")[0]
        routes = [r.strip() for h in resp.all("Record-Route") for r in h.split(",")]
        self._routes = list(reversed(routes))
        await self.conn.send(self._request("ACK", self._remote_uri, self._cseq))

        _LOGGER.debug("SDP answer:\n%s", resp.body)
        host, media = parse_answer(resp.body)
        video, audio = media.get("video"), media.get("audio")
        if video and video["port"] and video["key"]:
            self._video.srtp = SrtpContext(video["key"])
        else:
            _LOGGER.warning("Gateway did not accept video: %s", resp.body)
        if audio and audio["port"] and audio["key"] and host:
            self._audio.srtp = SrtpContext(audio["key"])
            self._audio_dst = (host, audio["port"])
            self._audio_pt = audio["pt"]

    async def request_keyframe(self) -> None:
        if self.ended.is_set():
            return
        self._cseq += 1
        await self.conn.send(self._request(
            "INFO", self._remote_uri, self._cseq,
            extra=["Content-Type: application/media_control+xml"], body=VFU_BODY,
        ))

    async def _handle_request(self, msg: SipMessage) -> None:
        await self.conn.send(self.conn.build_response(msg))
        if msg.method == "BYE":
            _LOGGER.debug("Gateway hung up")
            self.ended.set()

    async def _sip_loop(self) -> None:
        try:
            while not self.ended.is_set():
                while not self.conn.incoming.empty():
                    await self._handle_request(self.conn.incoming.get_nowait())
                try:
                    msg = await self.conn.receive(timeout=60)
                except TimeoutError:
                    continue
                if not msg.is_response:
                    await self._handle_request(msg)
        except SipError as err:
            _LOGGER.debug("Call signalling ended: %s", err)
        finally:
            self.ended.set()

    async def hangup(self) -> None:
        already_ended = self.ended.is_set()
        self.ended.set()
        if self._sip_task:
            self._sip_task.cancel()
            try:
                await self._sip_task
            except asyncio.CancelledError:
                pass
        if not already_ended and self.conn.connected:
            self._cseq += 1
            try:
                await self.conn.send(self._request("BYE", self._remote_uri, self._cseq))
                await self.conn.wait_response("BYE", 2)
            except SipError:
                pass
        self._close_media()
        _LOGGER.debug(
            "Call ended: video %d/%d packets decrypted (%d SRTP failures), audio %d received",
            self._video.packets, self._video.received, self._video.auth_failures, self._audio.received,
        )

    def _close_media(self) -> None:
        for transport in self._transports:
            transport.close()
        self._transports = []
        self._nal_listeners.clear()
