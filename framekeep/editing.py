"""Save edited JPEGs exclusively to a separate folder, never over originals."""

import base64
import hashlib
from pathlib import Path

from .image_processing import _run_worker
from .library import file_hash, safe_segment
from .output_files import check_output_stream, output_directory


def save_edit(library, photo_id, destination, turns, crop, progress, cancelled):
    photo = library.photo(photo_id)
    if photo["media_type"] != "image":
        raise ValueError("Image editing is available for still photos only.")
    root = library.output_folder(destination)
    progress(0, 1, "Creating an edited copy locally…")
    request = {key: photo[key] for key in ("path", "sha256", "size", "mtime_ns", "captured_at")}
    request.update(operation="image_edit", root=photo["source_root"], turns=turns, crop=crop)
    result = _run_worker(request)
    data = base64.b64decode(result["jpeg"], validate=True)
    if not 0 < len(data) <= 10 * 1024**2:
        raise ValueError("Edited copy exceeded the save limit.")
    if cancelled():
        return {"cancelled": True}
    library.photo(photo_id)
    stem = safe_segment(Path(photo["filename"]).stem) + "-edited"
    with output_directory(root):
        number = 1
        while True:
            target = root / (stem + (f" ({number})" if number > 1 else "") + ".jpg")
            try:
                output = target.open("xb")
                break
            except FileExistsError:
                number += 1
        try:
            with output:
                check_output_stream(output, target)
                output.write(data)
            if file_hash(target, root) != hashlib.sha256(data).hexdigest():
                raise ValueError("Edited copy verification failed.")
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    progress(1, 1, "Edited copy saved and verified")
    return {"edited_copy": str(target), "width": result["width"], "height": result["height"]}
