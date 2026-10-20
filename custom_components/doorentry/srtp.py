"""SRTP AES_CM_128_HMAC_SHA1_80 (RFC 3711) with SDES keys, RTP only (no SRTCP)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

SUITE = "AES_CM_128_HMAC_SHA1_80"
TAG_LEN = 10


def _aes_ctr(key: bytes, iv: bytes, data: bytes) -> bytes:
    enc = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    return enc.update(data) + enc.finalize()


def _kdf(master_key: bytes, master_salt: bytes, label: int, length: int) -> bytes:
    # key_derivation_rate = 0: x = (label || 0^48) XOR master_salt
    x = bytearray(master_salt)
    x[7] ^= label
    return _aes_ctr(master_key, bytes(x) + b"\x00\x00", b"\x00" * length)


def _header_len(packet: bytes) -> int:
    cc = packet[0] & 0x0F
    length = 12 + 4 * cc
    if packet[0] & 0x10:
        ext_words = int.from_bytes(packet[length + 2:length + 4], "big")
        length += 4 + 4 * ext_words
    return length


def new_inline_key() -> str:
    return base64.b64encode(os.urandom(30)).decode()


class SrtpContext:
    """One direction of one SSRC stream."""

    def __init__(self, inline_key: str):
        material = base64.b64decode(inline_key.split("|")[0])
        master_key, master_salt = material[:16], material[16:30]
        self._key = _kdf(master_key, master_salt, 0x00, 16)
        self._auth = _kdf(master_key, master_salt, 0x01, 20)
        self._salt = _kdf(master_key, master_salt, 0x02, 14)
        self._roc = 0
        self._s_l: int | None = None

    def _iv(self, ssrc: int, index: int) -> bytes:
        iv = int.from_bytes(self._salt + b"\x00\x00", "big") ^ (ssrc << 64) ^ (index << 16)
        return iv.to_bytes(16, "big")

    def _tag(self, authenticated: bytes, roc: int) -> bytes:
        return hmac.new(self._auth, authenticated + roc.to_bytes(4, "big"), hashlib.sha1).digest()[:TAG_LEN]

    def protect(self, packet: bytes) -> bytes:
        seq = int.from_bytes(packet[2:4], "big")
        ssrc = int.from_bytes(packet[8:12], "big")
        if self._s_l is not None and seq < self._s_l and self._s_l - seq > 0x8000:
            self._roc = (self._roc + 1) & 0xFFFFFFFF
        self._s_l = seq
        hlen = _header_len(packet)
        index = (self._roc << 16) | seq
        out = packet[:hlen] + _aes_ctr(self._key, self._iv(ssrc, index), packet[hlen:])
        return out + self._tag(out, self._roc)

    def unprotect(self, packet: bytes) -> bytes | None:
        """Return the plain RTP packet, or None if authentication fails."""
        if len(packet) < 12 + TAG_LEN:
            return None
        body, tag = packet[:-TAG_LEN], packet[-TAG_LEN:]
        seq = int.from_bytes(body[2:4], "big")
        ssrc = int.from_bytes(body[8:12], "big")

        # RFC 3711 appendix A: guess the rollover counter from the highest sequence seen
        if self._s_l is None:
            roc = self._roc
        elif self._s_l < 0x8000:
            roc = self._roc - 1 if seq - self._s_l > 0x8000 else self._roc
        else:
            roc = self._roc + 1 if self._s_l - 0x8000 > seq else self._roc
        roc &= 0xFFFFFFFF

        if not hmac.compare_digest(tag, self._tag(body, roc)):
            return None

        if self._s_l is None or roc == (self._roc + 1) & 0xFFFFFFFF:
            self._roc, self._s_l = roc, seq
        elif roc == self._roc and seq > self._s_l:
            self._s_l = seq

        hlen = _header_len(body)
        index = (roc << 16) | seq
        return body[:hlen] + _aes_ctr(self._key, self._iv(ssrc, index), body[hlen:])
