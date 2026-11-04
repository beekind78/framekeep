"""Small generated containers for metadata/copy checks, without encoded video frames."""

import struct
from datetime import UTC, datetime


def atom(name, payload, *, extended=False):
    return (
        struct.pack(">I4sQ", 1, name, len(payload) + 16)
        if extended
        else struct.pack(">I4s", len(payload) + 8, name)
    ) + payload


def video_bytes(*, date=None, version=0, brand=b"qt  ", legacy=False, has_video=True, extended=False):
    date = date or datetime(2026, 4, 3, 4, tzinfo=UTC)
    seconds = int((date - datetime(1904, 1, 1, tzinfo=UTC)).total_seconds())
    if version == 1:
        mvhd = bytes([1, 0, 0, 0]) + struct.pack(">QQIQ", seconds, seconds, 1000, 5500)
    else:
        mvhd = b"\0" * 4 + struct.pack(">IIII", seconds, seconds, 1000, 5500)
    handler = b"\0" * 8 + (b"vide" if has_video else b"soun") + b"\0" * 12
    movie = atom(
        b"moov", atom(b"mvhd", mvhd + b"\0" * 80) + atom(b"trak", atom(b"mdia", atom(b"hdlr", handler)))
    )
    prefix = atom(b"wide", b"") if legacy else atom(b"ftyp", brand + b"\0" * 4 + brand)
    return prefix + atom(b"mdat", b"\0" * 16, extended=extended) + movie
