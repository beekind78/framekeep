"""Full-resolution rotate/crop of a verified original in the bounded image worker."""

import base64
import hashlib
import io
import json
import math
import os
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from framekeep.image_processing import MAX_DIMENSION, MAX_FILE_BYTES, MAX_PIXELS
from framekeep.image_worker import set_resource_limits
from framekeep.source_files import open_source


def edit(request):
    from PIL import Image, ImageOps
    from pillow_heif import register_heif_opener

    from framekeep.media_types import FORMAT_EXTENSIONS

    turns, crop = request["turns"], request["crop"]
    if turns not in (0, 1, 2, 3) or len(crop) != 4 or not all(math.isfinite(value) for value in crop):
        raise ValueError("Invalid edit settings.")
    left, top, right, bottom = crop
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError("Choose a crop inside the image.")
    register_heif_opener(decode_threads=1)
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    with open_source(Path(request["path"]), Path(request["root"])) as source:
        info = os.fstat(source.fileno())
        if not 0 < info.st_size <= MAX_FILE_BYTES or (info.st_size, info.st_mtime_ns) != (request["size"], request["mtime_ns"]):
            raise ValueError("Original changed or exceeded the image limit. Rescan this source.")
        digest = hashlib.sha256()
        while block := source.read(1024 * 1024):
            digest.update(block)
        if digest.hexdigest() != request["sha256"]:
            raise ValueError("Original contents changed since indexing. Rescan this source.")
        source.seek(0)
        with Image.open(source, formats=tuple(FORMAT_EXTENSIONS)) as original:
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
    output = io.BytesIO()
    image.save(output, "JPEG", quality=92, exif=exif, icc_profile=None)
    data = output.getvalue()
    if len(data) > 10 * 1024**2:
        raise ValueError("Edited JPEG exceeds the 10 MiB save limit. Choose a smaller crop.")
    return {"ok": True, "jpeg": base64.b64encode(data).decode("ascii"), "width": image.width, "height": image.height}


if __name__ == "__main__":
    try:
        set_resource_limits()
        raw = sys.stdin.buffer.read(16_385)
        if len(raw) > 16_384:
            raise ValueError("Invalid edit request.")
        result = edit(json.loads(raw))
    except Exception as error:  # noqa: BLE001 -- bounded subprocess boundary
        result = {"ok": False, "error": str(error)[:400]}
    sys.stdout.buffer.write(json.dumps(result).encode())
