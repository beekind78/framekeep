"""Save edited and recovered JPEG copies to a separate folder."""

import base64
import hashlib
import uuid
from pathlib import Path

from .image_processing import _run_worker
from .library import copy_verified_original, safe_segment
from .media_types import PHOTO_EXTENSIONS
from .output_files import (
    ApprovedOutput,
    new_output_file,
    new_output_folder,
    output_directory,
    remove_empty_output_folder,
)
from .recovery_ai import MAX_ENHANCE_SIDE, recovery_model


def _save_verified_jpeg(root, stem, encoded, label):
    data = base64.b64decode(encoded, validate=True)
    if not 0 < len(data) <= 10 * 1024**2:
        raise ValueError(f"{label} exceeded the save limit.")
    with new_output_file(root, Path(stem + ".jpg"), first_suffix=2) as (target, output):
        output.write(data)
        output.flush()
        output.seek(0)
        if hashlib.file_digest(output, "sha256").hexdigest() != hashlib.sha256(data).hexdigest():
            raise ValueError(f"{label} verification failed.")
    return target


def save_edit(library, photo_id, destination, turns, crop, progress, cancelled):
    photo = library.photo(photo_id)
    if photo["media_type"] != "image":
        raise ValueError("Image editing is available for still photos only.")
    root = destination if isinstance(destination, ApprovedOutput) else ApprovedOutput.inspect(
        library.output_folder(destination)
    )
    progress(0, 1, "Creating an edited copy locally…")
    request = {key: photo[key] for key in ("path", "sha256", "size", "mtime_ns", "captured_at")}
    request.update(operation="image_edit", root=photo["source_root"], turns=turns, crop=crop)
    result = _run_worker(request)
    if cancelled():
        return {"cancelled": True}
    library.photo(photo_id)
    stem = safe_segment(Path(photo["filename"]).stem) + "-edited"
    target = _save_verified_jpeg(root, stem, result["jpeg"], "Edited copy")
    progress(1, 1, "Edited copy saved and verified")
    return {"edited_copy": str(target), "width": result["width"], "height": result["height"]}


def _match_photo(library, match):
    candidate = library.photo(match["id"])
    suffix = "." + candidate["extension"].lower()
    if candidate["sha256"] != match["sha256"] or candidate["media_type"] != "image" or suffix not in PHOTO_EXTENSIONS:
        raise ValueError("The suggested library photo changed. Rescan its source.")
    return candidate, suffix


def _save_library_match(library, match, root, damaged):
    candidate, suffix = _match_photo(library, match)
    relative = Path(safe_segment(damaged.stem) + "-library-match" + suffix)
    target = copy_verified_original(Path(candidate["path"]), Path(candidate["source_root"]),
                                    candidate["sha256"], root, relative)
    return {"file": damaged.name, "source": candidate["filename"], "copy": target.name,
            "width": candidate["width"], "height": candidate["height"]}


def repairable_photo(library, photo_id):
    photo = library.photo(photo_id)
    if photo["media_type"] != "image" or photo["extension"] not in ("JPG", "JPEG"):
        raise ValueError("Gray edge repair is available for JPEG photos.")
    return photo


def review_photo_repair(library, photo_id, progress, cancelled):
    photo = repairable_photo(library, photo_id)
    progress(0, 1, "Checking the photo and looking for a matching copy…")
    if cancelled():
        return {}
    request = {key: photo[key] for key in ("path", "sha256", "size", "mtime_ns")}
    result = _run_worker({**request, "root": photo["source_root"], "operation": "jpeg_repair",
                          "match_data": str(library.data)})
    match = result.get("match")
    suggestion = None
    if match:
        candidate, _ = _match_photo(library, match)
        suggestion = {key: candidate[key] for key in ("id", "filename", "width", "height")}
    return {"repair_review": {"photo_id": photo_id, "filename": photo["filename"],
                              "has_crop": result["crop"] is not None, "match": suggestion},
            "jpeg_repair": {"photo_id": photo_id, "sha256": photo["sha256"], "crop": result["crop"],
                            "preview": result.get("preview"), "match": match}}


def save_photo_repair(library, repair, choice, destination, progress, cancelled):
    photo = repairable_photo(library, repair["photo_id"])
    if photo["sha256"] != repair["sha256"]:
        raise ValueError("The JPEG changed since this review. Open a new repair review.")
    progress(0, 1, "Verifying the photo and preparing a separate copy…")
    request = {key: photo[key] for key in ("path", "sha256", "size", "mtime_ns", "captured_at")}
    request["root"] = photo["source_root"]
    if choice == "crop":
        result = _run_worker({**request, "operation": "image_edit", "turns": 0, "crop": repair["crop"]})
    else:
        _run_worker({**request, "operation": "jpeg_repair", "verify_only": True})
    if cancelled():
        return {"cancelled": True, "repair": True, "recovered": 0}

    root = new_output_folder(destination, "Framekeep-Repaired-" + uuid.uuid4().hex[:12])
    matches, errors = [], []
    try:
        library.output_folder(str(root.path))
        if choice == "match":
            matches.append(_save_library_match(library, repair["match"], root, Path(photo["filename"])))
        else:
            stem = safe_segment(Path(photo["filename"]).stem) + "-repaired"
            _save_verified_jpeg(root, stem, result["jpeg"], "Repaired copy")
    except (OSError, ValueError):
        try:
            remove_empty_output_folder(destination, root)
        except (OSError, ValueError):
            pass
        raise
    library_id, indexed = _index_recovered_folder(library, root, progress, cancelled, errors)
    return {"repair": True, "recovered": 1, "trimmed": int(choice == "crop"), "matched": len(matches),
            "matches": matches, "destination": str(root.path), "recovered_library": library_id,
            "indexed_recovered": indexed, "cancelled": cancelled(), "errors": errors, "error_count": len(errors)}


def _index_recovered_folder(library, root, progress, cancelled, errors):
    library_id = None
    if not cancelled():
        try:
            with output_directory(root) as path:
                library_id = library.add(str(path))
                scan = library.scan(library_id, lambda done, total, name: progress(done, total, f"Adding to Framekeep: {name}"), cancelled)
            errors.extend(scan.get("errors", []))
            return library_id, scan.get("found", 0)
        except (OSError, ValueError) as error:
            errors.append({"file": root.path.name, "reason": str(error)})
    return library_id, 0


def _recovery_source(library, recovery):
    with library.connect() as db:
        source = db.execute(
            "SELECT path FROM libraries WHERE id=? AND removed=0", (recovery["library_id"],)
        ).fetchone()
    if source is None:
        raise ValueError("The source folder was removed. Rescan it to create a new report.")
    return source["path"]


def review_recovery_matches(library, recovery, progress, cancelled):
    source = _recovery_source(library, recovery)
    files = recovery["files"]
    candidates, errors = [], []
    for index, item in enumerate(files):
        if cancelled():
            break
        name = Path(item["path"]).name
        progress(index, len(files), name)
        try:
            result = _run_worker({**item, "root": source, "operation": "jpeg_recovery",
                                  "match_data": str(library.data), "match_only": True})
            match = result["match"]
            if match:
                photo, _ = _match_photo(library, match)
                candidates.append({**match, "index": index, "file": name, "source": photo["filename"],
                                   "width": photo["width"], "height": photo["height"]})
        except (OSError, ValueError) as error:
            errors.append({"file": name, "reason": str(error)})
    return {"jpeg_recovery": recovery, "review_candidates": candidates,
            "errors": errors[:30], "error_count": len(errors)}


def recovery_preview(library, recovery, candidate):
    item = recovery["files"][candidate["index"]]
    result = _run_worker({**item, "root": _recovery_source(library, recovery), "operation": "jpeg_recovery",
                          "preview_only": True, "rotation": candidate["rotation"]})
    return base64.b64decode(result["jpeg"], validate=True)


def recover_jpegs(library, recovery, destination, progress, cancelled, *, enhance=False, approved=None, trim_edge=False):
    source = _recovery_source(library, recovery)
    approved = approved or {}
    root = new_output_folder(destination, "Framekeep-Recovered-" + uuid.uuid4().hex[:12])
    library.output_folder(str(root.path))

    model, model_unavailable = None, False
    recovered, enhanced, trimmed = 0, 0, 0
    matches, errors = [], []
    files = recovery["files"]
    for index, item in enumerate(files):
        if cancelled():
            break
        path = Path(item["path"])
        progress(index, len(files), path.name)
        try:
            request = {**item, "root": source, "operation": "jpeg_recovery", "trim_edge": trim_edge}
            match = approved.get(index)
            if match:
                _run_worker({**request, "match_only": True})  # Recheck the damaged file's scan hash.
                if cancelled():
                    break
                matches.append(_save_library_match(library, match, root, path))
                recovered += 1
                continue
            result = _run_worker(request)
            if cancelled():
                break
            if enhance and max(result["width"], result["height"]) <= MAX_ENHANCE_SIDE:
                if model is None and not model_unavailable:
                    try:
                        model = recovery_model(library.data, progress, cancelled)
                    except (OSError, ValueError):
                        model_unavailable = True
                if model and not cancelled():
                    result = _run_worker({**request, "ai_model": str(model)})
            if cancelled():
                break
            stem = safe_segment(path.stem) + "-recovered"
            if result["trimmed"]:
                stem += "-trimmed"
            if result["enhanced"]:
                stem += "-ai3x"
            _save_verified_jpeg(root, stem, result["jpeg"], "Recovered copy")
            recovered += 1
            enhanced += result["enhanced"]
            trimmed += result["trimmed"]
        except (OSError, ValueError) as error:
            errors.append({"file": path.name, "reason": str(error)})
    library_id, indexed = _index_recovered_folder(library, root, progress, cancelled, errors) if recovered else (None, 0)
    if not recovered:
        try:
            remove_empty_output_folder(destination, root)
        except (OSError, ValueError):
            pass
    return {"recovered": recovered, "enhanced": enhanced, "enhance_requested": enhance, "trimmed": trimmed,
            "matched": len(matches), "matches": matches[:30], "model_unavailable": model_unavailable,
            "destination": str(root.path if recovered else destination.path), "cancelled": cancelled(),
            "recovered_library": library_id, "indexed_recovered": indexed,
            "errors": errors[:30], "error_count": len(errors)}
