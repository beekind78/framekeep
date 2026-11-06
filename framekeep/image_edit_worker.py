"""Create an edited or recovered JPEG from a verified original in a bounded process."""

import base64
import hashlib
import io
import json
import math
import os
import re
import sys
import warnings
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from framekeep.image_processing import MAX_DIMENSION, MAX_FILE_BYTES, MAX_PIXELS, is_truncated_image
from framekeep.image_worker import set_resource_limits
from framekeep.source_files import open_source


@contextmanager
def verified_original(request):
    with open_source(Path(request["path"]), Path(request["root"])) as source:
        info = os.fstat(source.fileno())
        if not 0 < info.st_size <= MAX_FILE_BYTES or (info.st_size, info.st_mtime_ns) != (
            request["size"], request["mtime_ns"]
        ):
            raise ValueError("Original changed or exceeded the image limit. Rescan this source.")
        if hashlib.file_digest(source, "sha256").hexdigest() != request["sha256"]:
            raise ValueError("Original contents changed since indexing. Rescan this source.")
        source.seek(0)
        yield source


def encode_jpeg(image, exif, size_error):
    with io.BytesIO() as output:
        image.save(output, "JPEG", quality=92, exif=exif, icc_profile=None)
        data = output.getvalue()
    if len(data) > 10 * 1024**2:
        raise ValueError(size_error)
    return {"ok": True, "jpeg": base64.b64encode(data).decode("ascii"), "width": image.width, "height": image.height}


def edit(request):
    from PIL import Image, ImageOps
    from pillow_heif import register_heif_opener

    from framekeep.media_types import PHOTO_DECODERS

    turns, crop = request["turns"], request["crop"]
    if turns not in (0, 1, 2, 3) or len(crop) != 4 or not all(math.isfinite(value) for value in crop):
        raise ValueError("Invalid edit settings.")
    left, top, right, bottom = crop
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError("Choose a crop inside the image.")
    register_heif_opener(decode_threads=1)
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    with verified_original(request) as source, Image.open(source, formats=PHOTO_DECODERS) as original:
        if original.width * original.height > MAX_PIXELS or max(original.size) > MAX_DIMENSION:
            raise ValueError("Image exceeded the edit dimension limit.")
        image = ImageOps.exif_transpose(original).convert("RGB")
    if turns:
        image = image.transpose({1: Image.Transpose.ROTATE_270, 2: Image.Transpose.ROTATE_180, 3: Image.Transpose.ROTATE_90}[turns])
    image = image.crop((math.floor(left * image.width), math.floor(top * image.height),
                        math.ceil(right * image.width), math.ceil(bottom * image.height)))
    exif = Image.Exif()
    exif[274], exif[305] = 1, "Framekeep edited copy"
    exif[306] = request["captured_at"].replace("-", ":", 2).replace("T", " ")[:19]
    return encode_jpeg(image, exif, "Edited JPEG exceeds the 10 MiB save limit. Choose a smaller crop.")


def recover_jpeg(request):
    from PIL import Image, ImageFile, ImageOps

    from framekeep.library import date_metadata
    from framekeep.recovery_ai import MAX_ENHANCE_SIDE, enhance_recovered_image
    from framekeep.recovery_match import find_clearer_copy, trim_gray_edge

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    with verified_original(request) as source:
        ImageFile.LOAD_TRUNCATED_IMAGES = False
        with Image.open(source, formats=("JPEG",)) as original:
            if original.format != "JPEG":
                raise ValueError("Recovery supports incomplete JPEG photos only.")
            if original.width * original.height > MAX_PIXELS or max(original.size) > MAX_DIMENSION:
                raise ValueError("Image exceeded the recovery dimension limit.")
            try:
                original.load()
            except OSError as error:
                if not is_truncated_image(error):
                    raise
            else:
                raise ValueError("This JPEG opens normally at full size. Rescan it before trying recovery.")
        source.seek(0)
        # Lenient loading applies only to this explicit recovery attempt in this subprocess.
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            with Image.open(source, formats=("JPEG",)) as original:
                recovered = original.convert("RGB")
                captured_at, _, _ = date_metadata(original, Path(request["path"]), os.fstat(source.fileno()).st_mtime)
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = False

    match = find_clearer_copy(recovered, Path(request["match_data"])) if request.get("match_data") else None
    if request.get("match_only"):
        return {"ok": True, "match": match}
    if request.get("preview_only"):
        recovered = recovered.rotate(request["rotation"], expand=True)
        recovered.thumbnail((480, 480))
        return encode_jpeg(recovered, b"", "The damaged photo preview is too large.")
    original_size = recovered.size
    if request.get("trim_edge"):
        recovered = trim_gray_edge(recovered)
    trimmed = recovered.size != original_size
    recovered = ImageOps.exif_transpose(recovered)

    exif = Image.Exif()
    exif[274], exif[305] = 1, "Framekeep recovered copy"
    exif[306] = captured_at.replace("-", ":", 2).replace("T", " ")[:19]
    enhanced = bool(request.get("ai_model")) and max(recovered.size) <= MAX_ENHANCE_SIDE
    if enhanced:
        recovered = enhance_recovered_image(recovered, Path(request["ai_model"]))
        exif[305] = "Framekeep recovered copy with AI enlargement"
    result = encode_jpeg(recovered, exif, "Recovered JPEG exceeds the 10 MiB save limit.")
    return {**result, "enhanced": enhanced, "trimmed": trimmed, "match": match}


def inspect_recovered_jpeg(request):
    from PIL import Image, ImageOps

    from framekeep.recovery_match import find_clearer_copy, gray_edge_crop

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    with verified_original(request) as source:
        if request.get("verify_only"):
            return {"ok": True}
        with Image.open(source, formats=("JPEG",)) as original:
            if original.width * original.height > MAX_PIXELS or max(original.size) > MAX_DIMENSION:
                raise ValueError("Repair supports JPEGs within Framekeep's image size limits.")
            image = ImageOps.exif_transpose(original).convert("RGB")
    box = gray_edge_crop(image)
    if box is None:
        return {"ok": True, "crop": None}
    cropped = image.crop(box)
    cropped.thumbnail((480, 480))
    preview = encode_jpeg(cropped, b"", "The cropped preview is too large.")
    width, height = image.size
    crop = [edge / side for edge, side in zip(box, (width, height, width, height), strict=True)]
    image.thumbnail((480, 480))
    visible = tuple(round(fraction * side) for fraction, side in zip(
        crop, (image.width, image.height, image.width, image.height), strict=True
    ))
    preferred = re.sub(r"-recovered(?:-trimmed)?(?:-ai3x)?(?: \(\d+\))?$", "", Path(request["path"]).stem.lower())
    match = find_clearer_copy(image, Path(request["match_data"]), visible=visible,
                             exclude_sha=request["sha256"], preferred_stem=preferred)
    return {"ok": True, "crop": crop, "preview": preview["jpeg"], "match": match}


if __name__ == "__main__":
    try:
        set_resource_limits()
        raw = sys.stdin.buffer.read(16_385)
        if len(raw) > 16_384:
            raise ValueError("Invalid edit request.")
        request = json.loads(raw)
        worker = {"jpeg_recovery": recover_jpeg, "jpeg_repair": inspect_recovered_jpeg}.get(request.get("operation"), edit)
        result = worker(request)
    except Exception as error:  # noqa: BLE001 -- bounded subprocess boundary
        result = {"ok": False, "error": str(error)[:400]}
    sys.stdout.buffer.write(json.dumps(result).encode())
