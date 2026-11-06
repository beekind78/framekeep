"""Authenticated on-demand Live Photo previews. Originals are never transcoded in place."""

import os
import threading
from pathlib import Path

from .image_processing import process_motion
from .paths import ffmpeg_executable
from .source_files import open_source

MAX_PREVIEW = 8 * 1024 * 1024


class Motion:
    def __init__(self, library):
        self.library = library
        self._lock = threading.Lock()

    @staticmethod
    def ready():
        return ffmpeg_executable() is not None

    def preview(self, still_id):
        still = self.library.photo(still_id)
        if not still.get("live_motion_id"):
            raise ValueError("This image has no verified Live Photo motion pair.")
        motion = self.library.photo(still["live_motion_id"])
        with open_source(Path(motion["path"]), Path(motion["source_root"])) as source:
            info = os.fstat(source.fileno())
            if (info.st_size, info.st_mtime_ns) != (motion["size"], motion["mtime_ns"]):
                raise ValueError("Motion changed since indexing. Rescan this source.")
        ffmpeg = ffmpeg_executable()
        if not ffmpeg:
            raise ValueError("Install FFmpeg and reopen Framekeep to play Live Photo motion. The still photo remains available.")
        with self._lock:
            folder = self.library.data / "motion_previews"
            folder.mkdir(exist_ok=True)
            target = folder / (motion["sha256"] + "-v1.mp4")
            if target.is_file() and 0 < target.stat().st_size <= MAX_PREVIEW:
                return target.read_bytes()
            data = process_motion(Path(motion["path"]), Path(motion["source_root"]), Path(ffmpeg), motion["sha256"])
            if not 0 < len(data) <= MAX_PREVIEW:
                raise ValueError("Motion preview exceeded the output limit.")
            temporary = target.with_suffix(".tmp")
            try:
                temporary.write_bytes(data)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            return data
