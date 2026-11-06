"""Shared limits for full-length playback preparation."""

import hashlib
from typing import Literal

PlaybackQuality = Literal["720p", "4k"]
PLAYBACK_BOUNDS = {"720p": (1280, 720), "4k": (3840, 2160)}
MAX_INPUT = 2 * 1024**3
MAX_OUTPUT = 256 * 1024**2
MAX_DURATION = 30 * 60
WALL_SECONDS = 180
MAX_PSSH_MARKERS = 128


def stage_video(source, target, size: int, sha256: str) -> None:
    """Verify staged bytes and bound protection-system headers before FFmpeg opens them."""
    digest = hashlib.sha256()
    tail = b""
    copied = markers = 0
    while block := source.read(1024 * 1024):
        copied += len(block)
        if copied > size:
            raise ValueError("Video changed while preparing playback. Rescan this source.")
        # CVE-2026-107678 needs thousands of MOV 'pssh' atoms. Counting their
        # literal type markers overcounts safely, including in nested atoms.
        combined = tail + block
        markers += combined.count(b"pssh", max(0, len(tail) - 3))
        if markers > MAX_PSSH_MARKERS:
            raise ValueError("Video has too many protection headers for local playback.")
        # A legacy compressed movie header can hide those markers until decoding.
        if b"dcomzlib" in combined:
            raise ValueError("Compressed QuickTime headers are not supported for local playback.")
        tail = combined[-7:]
        digest.update(block)
        target.write(block)
    if copied != size or digest.hexdigest() != sha256:
        raise ValueError("Video contents changed since indexing. Rescan this source.")
