"""Speex narrowband encoder through ctypes (libspeex)."""
from __future__ import annotations

import ctypes
import ctypes.util
import functools
import sys

SPEEX_GET_FRAME_SIZE = 3
SPEEX_SET_QUALITY = 4
SPEEX_MODEID_NB = 0

_CANDIDATES = [
    "/opt/homebrew/lib/libspeex.dylib",
    "/usr/local/lib/libspeex.dylib",
    "libspeex.so.1",
    "libspeex.so",
]


class _SpeexBits(ctypes.Structure):
    _fields_ = [
        ("chars", ctypes.c_char_p),
        ("nbBits", ctypes.c_int),
        ("charPtr", ctypes.c_int),
        ("bitPtr", ctypes.c_int),
        ("owner", ctypes.c_int),
        ("overflow", ctypes.c_int),
        ("buf_size", ctypes.c_int),
        ("reserved1", ctypes.c_int),
        ("reserved2", ctypes.c_void_p),
    ]


@functools.cache
def _load() -> ctypes.CDLL:
    names = [ctypes.util.find_library("speex"), *_CANDIDATES]
    for name in filter(None, names):
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    hint = "brew install speex" if sys.platform == "darwin" else "apt install libspeex1"
    raise OSError(f"libspeex not found ({hint})")


class SpeexEncoder:
    """8 kHz mono, 20 ms frames of 160 signed 16-bit samples."""

    def __init__(self, quality: int = 8):
        lib = self._lib = _load()
        lib.speex_lib_get_mode.restype = ctypes.c_void_p
        lib.speex_lib_get_mode.argtypes = [ctypes.c_int]
        lib.speex_encoder_init.restype = ctypes.c_void_p
        lib.speex_encoder_init.argtypes = [ctypes.c_void_p]
        lib.speex_encoder_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        lib.speex_encoder_destroy.argtypes = [ctypes.c_void_p]
        lib.speex_encode_int.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_void_p]
        lib.speex_bits_write.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]

        self._state = lib.speex_encoder_init(lib.speex_lib_get_mode(SPEEX_MODEID_NB))
        q = ctypes.c_int(quality)
        lib.speex_encoder_ctl(self._state, SPEEX_SET_QUALITY, ctypes.byref(q))
        size = ctypes.c_int()
        lib.speex_encoder_ctl(self._state, SPEEX_GET_FRAME_SIZE, ctypes.byref(size))
        self.frame_size = size.value
        self._bits = _SpeexBits()
        lib.speex_bits_init(ctypes.byref(self._bits))
        self._out = ctypes.create_string_buffer(200)

    def encode(self, pcm: bytes) -> bytes:
        """Encode one frame of little-endian s16 PCM (frame_size samples)."""
        samples = (ctypes.c_int16 * self.frame_size).from_buffer_copy(pcm)
        lib = self._lib
        lib.speex_bits_reset(ctypes.byref(self._bits))
        lib.speex_encode_int(self._state, samples, ctypes.byref(self._bits))
        lib.speex_bits_insert_terminator(ctypes.byref(self._bits))
        n = lib.speex_bits_write(ctypes.byref(self._bits), self._out, len(self._out))
        return self._out.raw[:n]

    def close(self) -> None:
        if self._state:
            self._lib.speex_encoder_destroy(self._state)
            self._lib.speex_bits_destroy(ctypes.byref(self._bits))
            self._state = None
