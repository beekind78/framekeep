"""One bounded subprocess for all original-image decoding and face extraction."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .source_files import open_source

POLICY_VERSION = 2
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_PIXELS = 50_000_000
MAX_DIMENSION = 30_000
MEMORY_BYTES = 1024 * 1024 * 1024
TIMEOUT_SECONDS = 25
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
_slot = threading.BoundedSemaphore(1)


def _worker_command(worker="image_worker.py"):
    executable = Path(sys.executable)
    if sys.platform == "win32" and executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")
    return [str(executable), "-I", str(Path(__file__).with_name(worker))]


def process_image(
    path: Path, root: Path, size: int, *, models: Path | None = None, category_models: Path | None = None
) -> dict:
    if not 1 <= size <= 2200:
        raise ValueError("Invalid preview dimensions.")
    with open_source(path, root) as source:
        if not 0 < os.fstat(source.fileno()).st_size <= MAX_FILE_BYTES:
            raise ValueError("Image exceeds the 256 MiB encoded-file limit.")
    request = {
        "path": str(path),
        "root": str(root),
        "size": size,
        "models": str(models) if models else None,
        "category_models": str(category_models) if category_models else None,
    }
    result = _run_worker(request)
    result["jpeg"] = base64.b64decode(result["jpeg"], validate=True)
    return result


def process_video(path: Path, root: Path) -> dict:
    return _run_worker({"path": str(path), "root": str(root), "media_type": "video"})


def process_photo_metadata(path: Path, root: Path) -> dict:
    return _run_worker({"path": str(path), "root": str(root), "size": 32, "metadata_only": True})


def process_motion(path: Path, root: Path, ffmpeg: Path, sha256: str) -> bytes:
    result = _run_worker({"path": str(path), "root": str(root), "ffmpeg": str(ffmpeg),
                          "sha256": sha256, "operation": "motion"})
    return base64.b64decode(result["mp4"], validate=True)


def process_video_thumbnail(photo: dict, ffmpeg: str) -> bytes:
    request = {key: photo[key] for key in ("path", "sha256", "size", "mtime_ns")}
    request.update(root=photo["source_root"], ffmpeg=ffmpeg, operation="video_thumbnail")
    # The parent owns cleanup even when the bounded worker is killed or times out.
    with tempfile.TemporaryDirectory(prefix="framekeep-video-thumbnail-") as work:
        request["work"] = work
        return base64.b64decode(_run_worker(request)["jpeg"], validate=True)


def _run_worker(request: dict) -> dict:
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    environment = {
        **os.environ,
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "OPENCV_LOG_LEVEL": "ERROR",
    }
    with _slot:
        try:
            worker = {"motion": "motion_worker.py", "video_thumbnail": "video_thumbnail_worker.py",
                      "image_edit": "image_edit_worker.py"}.get(request.get("operation"))
            completed = subprocess.run(
                _worker_command(worker) if worker else _worker_command(),
                input=json.dumps(request).encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=TIMEOUT_SECONDS,
                creationflags=flags,
                cwd=Path(__file__).resolve().parent.parent,
                env=environment,
            )
        except subprocess.TimeoutExpired:
            raise ValueError(
                f"Image processing exceeded {TIMEOUT_SECONDS} seconds; file was skipped."
            ) from None
    if completed.returncode or len(completed.stdout) > MAX_OUTPUT_BYTES:
        raise ValueError("Image worker stopped or exceeded its resource limit; file was skipped.")
    try:
        result = json.loads(completed.stdout)
    except (ValueError, UnicodeError):
        raise ValueError("Image worker returned an invalid result; file was skipped.") from None
    if not result.get("ok"):
        raise ValueError(result.get("error", "This image could not be processed."))
    return result
