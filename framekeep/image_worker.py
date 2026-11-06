"""Internal worker. Resource limits are installed before importing native image libraries."""

from __future__ import annotations

import base64
import ctypes
import io
import json
import os
import sys
import warnings
from pathlib import Path

# -I ignores the caller's Python path. Only this trusted checkout is added.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from framekeep.image_processing import (
    MAX_DIMENSION,
    MAX_FILE_BYTES,
    MAX_OUTPUT_BYTES,
    MAX_PIXELS,
    MEMORY_BYTES,
    TRUNCATED_IMAGE_MESSAGE,
    TruncatedJPEGError,
    is_truncated_image,
)

_job_handle = None


def set_resource_limits(process_limit=1, cpu_seconds=20):
    global _job_handle
    if sys.platform != "win32":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        return
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("ProcessTime", ctypes.c_int64),
            ("JobTime", ctypes.c_int64),
            ("Flags", wintypes.DWORD),
            ("MinWorkingSet", ctypes.c_size_t),
            ("MaxWorkingSet", ctypes.c_size_t),
            ("ActiveProcesses", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("Priority", wintypes.DWORD),
            ("Scheduling", wintypes.DWORD),
        ]

    class Counters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("Basic", BasicLimits),
            ("IO", Counters),
            ("ProcessMemory", ctypes.c_size_t),
            ("JobMemory", ctypes.c_size_t),
            ("PeakProcessMemory", ctypes.c_size_t),
            ("PeakJobMemory", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    _job_handle = kernel.CreateJobObjectW(None, None)
    limits = ExtendedLimits()
    # Process CPU time, one active process, committed-memory cap and kill-on-job-close.
    limits.Basic.Flags = 0x2 | 0x8 | 0x100 | 0x2000
    limits.Basic.ProcessTime = cpu_seconds * 10_000_000
    limits.Basic.ActiveProcesses = process_limit
    limits.ProcessMemory = MEMORY_BYTES
    if (
        not _job_handle
        or not kernel.SetInformationJobObject(_job_handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
        or not kernel.AssignProcessToJobObject(_job_handle, kernel.GetCurrentProcess())
    ):
        raise OSError("Windows image-worker resource limits could not be established.")


def decode(request: dict):
    from PIL import Image, ImageOps, UnidentifiedImageError
    from pillow_heif import register_heif_opener

    from framekeep.category_model import initial_category, predict_category
    from framekeep.library import date_metadata
    from framekeep.live_metadata import apple_identifier
    from framekeep.media_types import PHOTO_DECODERS, media_kind, photo_extension
    from framekeep.source_files import open_source, require_file_identity
    from framekeep.visual_similarity import fingerprint

    register_heif_opener(decode_threads=1)
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    path, root, size = Path(request["path"]), Path(request["root"]), request["size"]
    if not isinstance(size, int) or not 1 <= size <= 2200:
        raise ValueError("Invalid preview dimensions.")
    with open_source(path, root) as source:
        expected = request.get("expected")
        require_file_identity(source, expected)
        info = os.fstat(source.fileno())
        if not 0 < info.st_size <= MAX_FILE_BYTES:
            raise ValueError("Image exceeds the 256 MiB encoded-file limit.")
        try:
            opened_image = Image.open(source, formats=PHOTO_DECODERS)
        except UnidentifiedImageError:
            source.seek(0)
            kind = media_kind(source.read(128))
            if kind == "video":
                raise ValueError(
                    "This file contains a video, despite its filename. Only photos are supported."
                ) from None
            if kind == "metadata":
                raise ValueError("This is a metadata file, not a photo.") from None
            raise ValueError(
                "Photo format could not be identified. The file may be incomplete or unsupported."
            ) from None
        with opened_image as image:
            extension = photo_extension(image.format, path.suffix)
            width, height = image.size
            if width * height > MAX_PIXELS or max(width, height) > MAX_DIMENSION:
                raise ValueError("Image exceeds the 50 megapixel or 30,000 pixel-side limit.")
            faces_only = request.get("faces_only")
            if not faces_only:
                date, date_source, camera = date_metadata(image, path, info.st_mtime)
                live_identifier = apple_identifier(image.getexif())
                if request.get("metadata_only"):
                    return {"ok": True, "metadata": {"live_identifier": live_identifier,
                            "size": info.st_size, "mtime_ns": info.st_mtime_ns}}
                # An MPO holds multiple still images. The primary frame is already selected.
                animated = image.format != "MPO" and getattr(image, "is_animated", False)
                category = initial_category(path, camera, animated)
            image.draft("RGB", (size, size))
            try:
                normalized = ImageOps.exif_transpose(image)
            except OSError as error:
                if image.format == "JPEG" and is_truncated_image(error):
                    raise TruncatedJPEGError(TRUNCATED_IMAGE_MESSAGE) from None
                raise
            normalized.thumbnail((size, size))
            normalized = normalized.convert("RGB")
            if faces_only:
                faces = extract_faces(normalized, Path(request["models"]))
                require_file_identity(source, expected)
                return {"ok": True, "faces": faces}
            if request.get("category_models") and category["kind_source"] == "pending":
                try:
                    category = predict_category(normalized, Path(request["category_models"]))
                except Exception as error:  # noqa: BLE001 -- classification failure must preserve readable photos
                    category = {**initial_category(path, "", False), "category_error": str(error)[:200]}
            output = io.BytesIO()
            normalized.save(output, "JPEG", quality=88, exif=b"", icc_profile=None)
            faces = extract_faces(normalized, Path(request["models"])) if request.get("models") else []
        require_file_identity(source, expected)
    return {
        "ok": True,
        "jpeg": base64.b64encode(output.getvalue()).decode("ascii"),
        "faces": faces,
        "fingerprint": fingerprint(normalized),
        "metadata": {
            "captured_at": date,
            "date_source": date_source,
            "camera": camera[:500],
            "width": width,
            "height": height,
            **category,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "extension": extension,
            "live_identifier": live_identifier,
        },
    }


def extract_faces(image, models: Path):
    import hashlib

    import cv2
    import numpy as np

    from framekeep.people import MODELS

    cv2.setNumThreads(1)
    bgr = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
    def model_buffer(name):
        _, checksum = MODELS[name]
        with (models / name).open("rb") as stream:
            data = stream.read(50 * 1024 * 1024 + 1)
        if len(data) > 50 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != checksum:
            raise ValueError("People model integrity check failed.")
        return np.frombuffer(data, dtype=np.uint8)

    detector_name, recognizer_name = MODELS
    empty = np.empty(0, dtype=np.uint8)
    detector = cv2.FaceDetectorYN.create("onnx", model_buffer(detector_name), empty, (image.width, image.height), 0.85)
    _, boxes = detector.detect(bgr)
    faces = [box for box in boxes[:50] if min(box[2], box[3]) >= 30] if boxes is not None else []
    if not faces:
        return []
    recognizer = cv2.FaceRecognizerSF.create("onnx", model_buffer(recognizer_name), empty)
    result = []
    for box in faces:
        crop = recognizer.alignCrop(bgr, box)
        vector = recognizer.feature(crop).flatten()
        vector /= max(float(np.linalg.norm(vector)), 1e-9)
        ok, jpeg = cv2.imencode(".jpg", crop)
        if not ok:
            raise ValueError("A face preview could not be encoded.")
        result.append(
            {"embedding": vector.tolist(), "jpeg": base64.b64encode(jpeg.tobytes()).decode("ascii")}
        )
    return result


def main():
    try:
        set_resource_limits()
        raw = sys.stdin.buffer.read(16_385)
        if len(raw) > 16_384:
            raise ValueError("Invalid image-worker request.")
        request = json.loads(raw)
        if request.get("media_type") == "video":
            from framekeep.video_metadata import read_video_metadata

            result = read_video_metadata(Path(request["path"]), Path(request["root"]), expected=request.get("expected"))
        else:
            result = decode(request)
    except TruncatedJPEGError:
        result = {"ok": False, "error_code": "truncated_jpeg", "error": TRUNCATED_IMAGE_MESSAGE}
    except Exception as error:  # noqa: BLE001 -- isolated process boundary, no unsafe fallback
        message = str(error) or "Image worker resource limit exceeded."
        if isinstance(error, OSError) and is_truncated_image(error):
            message = TRUNCATED_IMAGE_MESSAGE
        result = {"ok": False, "error": message[:400]}
    encoded = json.dumps(result).encode()
    if len(encoded) > MAX_OUTPUT_BYTES:
        encoded = b'{"ok":false,"error":"Image-worker output limit exceeded."}'
    sys.stdout.buffer.write(encoded)


if __name__ == "__main__":
    main()
