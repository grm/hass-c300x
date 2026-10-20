"""ffmpeg helpers: H264 → JPEG frames, any audio source → 8 kHz PCM for Speex."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from .call import SPEEX_FRAME_BYTES

_LOGGER = logging.getLogger(__name__)

JPEG_EOI = b"\xff\xd9"
MAX_STDIN_BUFFER = 2 * 1024 * 1024


class JpegDecoder:
    """Decode an Annex-B H264 stream into JPEG images with ffmpeg."""

    def __init__(self, ffmpeg: str) -> None:
        self._ffmpeg = ffmpeg
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self.latest: bytes | None = None
        self.frame_ready = asyncio.Condition()

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            self._ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            # Measured: first image after ~1 s instead of ~5 s of probing. "-fflags nobuffer" and
            # "-flags low_delay" make ffmpeg output only 2 images; raw H264 has no timestamps, hence
            # the explicit frame rate (the door station sends 10 images/s)
            "-probesize", "32768", "-analyzeduration", "0", "-f", "h264", "-framerate", "10", "-i", "pipe:0",
            "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "3", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        self._reader = asyncio.create_task(self._read())

    def feed(self, nal: bytes) -> None:
        if not self._proc or not self._proc.stdin or self._proc.stdin.is_closing():
            return
        if self._proc.stdin.transport.get_write_buffer_size() > MAX_STDIN_BUFFER:
            return  # ffmpeg is stuck: drop rather than grow without bound
        self._proc.stdin.write(nal)

    async def _read(self) -> None:
        buf = b""
        while True:
            chunk = await self._proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            # 0xFFD9 cannot appear inside entropy-coded data (0xFF is stuffed), only as EOI
            *frames, buf = buf.split(JPEG_EOI)
            if frames and frames[-1].startswith(b"\xff\xd8"):
                async with self.frame_ready:
                    self.latest = frames[-1] + JPEG_EOI
                    self.frame_ready.notify_all()
        err = await self._proc.stderr.read()
        if err:
            _LOGGER.debug("ffmpeg decoder: %s", err.decode(errors="replace").strip())

    async def stop(self) -> None:
        if not self._proc:
            return
        if self._proc.stdin and not self._proc.stdin.is_closing():
            self._proc.stdin.close()
        try:
            await asyncio.wait_for(self._proc.wait(), 3)
        except TimeoutError:
            self._proc.kill()
            await self._proc.wait()
        if self._reader:
            await asyncio.gather(self._reader, return_exceptions=True)
        async with self.frame_ready:
            self.frame_ready.notify_all()
        self._proc = None


def talk_filter(gain_db: float) -> str:
    """Telephone band + compression: louder speech without clipping on the door station speaker."""
    makeup = min(64.0, max(1.0, 10 ** (gain_db / 20)))  # acompressor takes a linear factor
    return ("highpass=f=200,lowpass=f=3400,"
            f"acompressor=threshold=-24dB:ratio=6:attack=5:release=150:makeup={makeup:.2f},"
            "alimiter=limit=0.8:level=0")  # headroom: Speex overshoots peaks on decode


class PcmSource:
    """Read any ffmpeg input (file, URL) as 20 ms frames of 8 kHz mono s16le."""

    def __init__(self, ffmpeg: str, source: str, gain_db: float) -> None:
        self._cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", source,
            "-af", talk_filter(gain_db), "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1",
        ]
        self._proc: asyncio.subprocess.Process | None = None

    async def frames(self) -> AsyncIterator[bytes]:
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            while True:
                try:
                    yield await self._proc.stdout.readexactly(SPEEX_FRAME_BYTES)
                except asyncio.IncompleteReadError as err:
                    if err.partial:
                        yield err.partial.ljust(SPEEX_FRAME_BYTES, b"\x00")
                    break
        finally:
            await self.close()

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if not proc:
            return
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        err = await proc.stderr.read()
        if err:
            _LOGGER.warning("ffmpeg audio source: %s", err.decode(errors="replace").strip())
