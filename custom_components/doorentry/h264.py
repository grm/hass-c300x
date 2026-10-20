"""RTP H264 depacketizer (RFC 6184, packetization-mode 1) producing an Annex-B byte stream."""
from __future__ import annotations

START_CODE = b"\x00\x00\x00\x01"
NAL_IDR = 5
NAL_SPS = 7
NAL_PPS = 8


def rtp_payload(packet: bytes) -> tuple[int, bool, bytes] | None:
    """Return (sequence number, marker, payload) of a plain RTP packet."""
    if len(packet) < 12 or packet[0] >> 6 != 2:
        return None
    cc = packet[0] & 0x0F
    offset = 12 + 4 * cc
    if packet[0] & 0x10:
        if len(packet) < offset + 4:
            return None
        offset += 4 + 4 * int.from_bytes(packet[offset + 2:offset + 4], "big")
    end = len(packet)
    if packet[0] & 0x20:
        end -= packet[-1]
    if offset >= end:
        return None
    return int.from_bytes(packet[2:4], "big"), bool(packet[1] & 0x80), packet[offset:end]


class H264Depacketizer:
    """Feed RTP packets, get complete NAL units (with start code)."""

    def __init__(self) -> None:
        self._fu: bytearray | None = None
        self._last_seq: int | None = None

    def push(self, packet: bytes) -> list[bytes]:
        parsed = rtp_payload(packet)
        if parsed is None:
            return []
        seq, _marker, payload = parsed
        lost = self._last_seq is not None and seq != (self._last_seq + 1) & 0xFFFF
        self._last_seq = seq
        if lost:
            self._fu = None  # a fragment is missing: drop the NAL being rebuilt

        nal_type = payload[0] & 0x1F
        if 1 <= nal_type <= 23:
            return [START_CODE + payload]
        if nal_type == 24:  # STAP-A
            nals, i = [], 1
            while i + 2 <= len(payload):
                size = int.from_bytes(payload[i:i + 2], "big")
                i += 2
                if size == 0 or i + size > len(payload):
                    break
                nals.append(START_CODE + payload[i:i + size])
                i += size
            return nals
        if nal_type == 28 and len(payload) > 2:  # FU-A
            start, end = payload[1] & 0x80, payload[1] & 0x40
            if start:
                header = (payload[0] & 0xE0) | (payload[1] & 0x1F)
                self._fu = bytearray([header])
            if self._fu is None:
                return []
            self._fu += payload[2:]
            if end:
                nal, self._fu = bytes(self._fu), None
                return [START_CODE + nal]
        return []


def nal_type(nal: bytes) -> int:
    return nal[len(START_CODE)] & 0x1F
