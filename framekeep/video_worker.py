"""Full-length, bounded local playback conversion; no original is modified."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from framekeep.image_worker import set_resource_limits
from framekeep.source_files import open_source
from framekeep.video_metadata import read_video_metadata
from framekeep.video_policy import MAX_DURATION, MAX_INPUT, MAX_OUTPUT, WALL_SECONDS


def convert(request):
    path, root, target = Path(request["path"]), Path(request["root"]), Path(request["target"])
    metadata = read_video_metadata(path, root)["metadata"]
    if not 0 < metadata["duration"] <= MAX_DURATION:
        raise ValueError("In-app playback supports videos up to 30 minutes. Open the original for longer videos.")
    source = target.with_name("input.mov")
    digest = hashlib.sha256()
    with open_source(path, root) as stream, source.open("xb") as output:
        info = os.fstat(stream.fileno())
        if not 0 < info.st_size <= MAX_INPUT:
            raise ValueError("In-app playback supports source files up to 2 GiB.")
        if (info.st_size, info.st_mtime_ns) != (request["size"], request["mtime_ns"]):
            raise ValueError("Video changed since indexing. Rescan this source.")
        while block := stream.read(1024 * 1024):
            digest.update(block)
            output.write(block)
    if digest.hexdigest() != request["sha256"]:
        raise ValueError("Video contents changed since indexing. Rescan this source.")
    command = [
        request["ffmpeg"], "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
        "-protocol_whitelist", "file", "-f", "mov", "-enable_drefs", "0", "-use_absolute_path", "0",
        "-probesize", "1048576", "-analyzeduration", "2000000", "-threads", "1",
        "-max_pixels", "50000000", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
        "-filter_threads", "1", "-vf",
        "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,fps=30,format=yuv420p",
        "-c:v", "libx264", "-threads", "1", "-preset", "veryfast", "-crf", "25",
        "-c:a", "aac", "-ac", "2", "-b:a", "128k", "-movflags", "+faststart",
        "-fs", str(MAX_OUTPUT), "-f", "mp4", str(target),
    ]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=WALL_SECONDS - 10, check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    if result.returncode or not target.is_file() or not 0 < target.stat().st_size < MAX_OUTPUT:
        raise ValueError("Video could not be prepared within the playback limits. The original is still available.")
    duration = read_video_metadata(target, target.parent)["metadata"]["duration"]
    # Never present a time/file-size limited prefix as the complete movie.
    if duration < metadata["duration"] - 0.5 or duration > metadata["duration"] + 1:
        raise ValueError("The complete video could not be prepared. Open the original instead.")
    return {"ok": True}


def main():
    try:
        set_resource_limits(process_limit=2, cpu_seconds=150)
        raw = sys.stdin.buffer.read(16_385)
        if len(raw) > 16_384:
            raise ValueError("Invalid playback request.")
        result = convert(json.loads(raw))
    except Exception as error:  # noqa: BLE001 -- bounded worker boundary
        result = {"ok": False, "error": str(error)[:400]}
    sys.stdout.buffer.write(json.dumps(result).encode())


if __name__ == "__main__":
    main()
