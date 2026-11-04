"""Bounded short-motion conversion. Only the trusted app invokes this worker."""

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from framekeep.image_worker import set_resource_limits
from framekeep.source_files import open_source
from framekeep.video_metadata import read_video_metadata

MAX_INPUT = 128 * 1024 * 1024
MAX_PREVIEW = 8 * 1024 * 1024
PREVIEW_SECONDS = 6


def convert(request):
    path, root = Path(request["path"]), Path(request["root"])
    metadata = read_video_metadata(path, root)["metadata"]
    if not 0 < metadata["duration"] <= 10:
        raise ValueError("Motion preview requires a short Live Photo video of at most 10 seconds.")
    with tempfile.TemporaryDirectory(prefix="framekeep-motion-") as work:
        temporary = Path(work)
        source, target = temporary / "input.mov", temporary / "preview.mp4"
        digest = hashlib.sha256()
        with open_source(path, root) as stream, source.open("xb") as output:
            if not 0 < os.fstat(stream.fileno()).st_size <= MAX_INPUT:
                raise ValueError("Live Photo motion exceeds the 128 MiB preview limit.")
            while block := stream.read(1024 * 1024):
                digest.update(block)
                output.write(block)
        if digest.hexdigest() != request["sha256"]:
            raise ValueError("Motion contents changed since indexing. Rescan this source.")
        command = [
            request["ffmpeg"], "-nostdin", "-hide_banner", "-loglevel", "error",
            "-protocol_whitelist", "file", "-f", "mov", "-enable_drefs", "0", "-use_absolute_path", "0",
            "-probesize", "1048576", "-analyzeduration", "2000000", "-threads", "1",
            "-max_pixels", "50000000", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
            "-t", str(PREVIEW_SECONDS), "-filter_threads", "1", "-vf",
            "scale=w='min(960,iw)':h='min(960,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,fps=24,format=yuv420p",
            "-c:v", "libx264", "-threads", "1", "-preset", "veryfast", "-crf", "25",
            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", "-fs", str(MAX_PREVIEW),
            "-f", "mp4", str(target),
        ]
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=18, check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        except subprocess.TimeoutExpired:
            raise ValueError("Live Photo motion preview exceeded its time limit.") from None
        if result.returncode or not target.is_file() or not 0 < target.stat().st_size <= MAX_PREVIEW:
            raise ValueError("This Live Photo motion could not be previewed within the supported limits.")
        return {"ok": True, "mp4": base64.b64encode(target.read_bytes()).decode("ascii")}


def main():
    try:
        # This wrapper and one FFmpeg child share a Windows job, including kill-on-close.
        set_resource_limits(process_limit=2)
        raw = sys.stdin.buffer.read(16_385)
        if len(raw) > 16_384:
            raise ValueError("Invalid motion-preview request.")
        result = convert(json.loads(raw))
    except Exception as error:  # noqa: BLE001 -- bounded subprocess boundary
        result = {"ok": False, "error": str(error)[:400]}
    sys.stdout.buffer.write(json.dumps(result).encode())


if __name__ == "__main__":
    main()
