"""Extract a small representative JPEG in a bounded worker and FFmpeg child."""

import base64
import io
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from framekeep.image_worker import set_resource_limits
from framekeep.source_files import open_source
from framekeep.video_metadata import read_video_metadata
from framekeep.video_policy import MAX_INPUT, stage_video

MAX_JPEG = 512 * 1024


def thumbnail(request):
    from PIL import Image

    path, root = Path(request["path"]), Path(request["root"])
    metadata = read_video_metadata(path, root)["metadata"]
    work = request["work"]
    source, target = Path(work) / "input.mov", Path(work) / "frame.jpg"
    with open_source(path, root) as stream, source.open("xb") as output:
        info = os.fstat(stream.fileno())
        if not 0 < info.st_size <= MAX_INPUT:
            raise ValueError("Video thumbnails support source files up to 2 GiB.")
        if (info.st_size, info.st_mtime_ns) != (request["size"], request["mtime_ns"]):
            raise ValueError("Video changed since indexing. Rescan this source.")
        stage_video(stream, output, info.st_size, request["sha256"])
    # Skip a little of the opening, then choose among 12 downscaled frames.
    position = min(1.0, metadata["duration"] / 10) if metadata["duration"] >= 1 else 0
    command = [
        request["ffmpeg"], "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
        "-protocol_whitelist", "file", "-f", "mov", "-enable_drefs", "0", "-use_absolute_path", "0",
        "-probesize", "1048576", "-analyzeduration", "2000000", "-threads", "1",
        "-max_pixels", "50000000", "-ss", str(position), "-t", "3", "-i", str(source),
        "-map", "0:v:0", "-an", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
        "-frames:v", "1", "-filter_threads", "1", "-vf",
        "scale=w='min(480,iw)':h='min(480,ih)':force_original_aspect_ratio=decrease,thumbnail=12",
        "-c:v", "mjpeg", "-threads", "1", "-q:v", "3", "-fs", str(MAX_JPEG),
        "-f", "image2", "-update", "1", str(target),
    ]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=18, check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    if result.returncode or not target.is_file() or not 0 < target.stat().st_size < MAX_JPEG:
        raise ValueError("A thumbnail could not be generated for this video. You can still open the video.")
    with Image.open(target, formats=("JPEG",)) as frame:
        if not 0 < frame.width <= 480 or not 0 < frame.height <= 480:
            raise ValueError("Video thumbnail exceeded the dimension limit.")
        output = io.BytesIO()
        frame.convert("RGB").save(output, "JPEG", quality=85, exif=b"", icc_profile=None)
    data = output.getvalue()
    if not 0 < len(data) < MAX_JPEG:
        raise ValueError("Video thumbnail exceeded the output limit.")
    return {"ok": True, "jpeg": base64.b64encode(data).decode("ascii")}


def main():
    try:
        set_resource_limits(process_limit=2)
        raw = sys.stdin.buffer.read(16_385)
        if len(raw) > 16_384:
            raise ValueError("Invalid video thumbnail request.")
        result = thumbnail(json.loads(raw))
    except Exception as error:  # noqa: BLE001 -- bounded worker boundary
        result = {"ok": False, "error": str(error)[:400]}
    sys.stdout.buffer.write(json.dumps(result).encode())


if __name__ == "__main__":
    main()
