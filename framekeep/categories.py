"""Download a pinned public model, then categorize photos entirely on this PC."""

import hashlib
import urllib.request
from collections import Counter
from pathlib import Path

from .category_model import MODEL_BYTES, MODEL_NAME, MODEL_SHA256, MODEL_URL, MODEL_VERSION
from .image_processing import process_image


class Categories:
    def __init__(self, library):
        self.library = library
        self.directory = library.data / "models" / "categories"
        self.model = self.directory / MODEL_NAME
        self._verified = None

    def ready(self):
        try:
            info = self.model.stat()
            signature = (info.st_size, info.st_mtime_ns)
            if signature == self._verified:
                return True
            if info.st_size != MODEL_BYTES:
                return False
            with self.model.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != MODEL_SHA256:
                return False
            self._verified = signature
            return True
        except OSError:
            return False

    def install(self, progress, cancelled):
        if self.ready():
            return {"ready": True}
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.model.with_suffix(".download")
        try:
            received = 0
            with urllib.request.urlopen(MODEL_URL, timeout=45) as response, temporary.open("wb") as output:
                while block := response.read(256 * 1024):
                    if cancelled():
                        return {"cancelled": True}
                    received += len(block)
                    if received > MODEL_BYTES:
                        raise ValueError("Category-model download exceeded its expected size.")
                    output.write(block)
                    progress(
                        received,
                        MODEL_BYTES,
                        f"Downloading category model: {received / 1024 / 1024:.1f} / 85 MB",
                    )
            with temporary.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if received != MODEL_BYTES or digest != MODEL_SHA256:
                raise ValueError("Category model checksum mismatch. Please retry the download.")
            temporary.replace(self.model)
        finally:
            temporary.unlink(missing_ok=True)
        progress(MODEL_BYTES, MODEL_BYTES, "Category model ready")
        return {"ready": True}

    def analyze(self, filters, progress, cancelled):
        if not self.ready():
            raise ValueError("Download the category model first.")
        filters = {**filters, "media_type": "image"}
        rows = self.library.photos(filters, limit=None)["photos"]
        errors, updated, manual, unchanged = [], 0, 0, 0
        for index, photo in enumerate(rows):
            if cancelled():
                break
            progress(index, len(rows), photo["filename"])
            if photo["kind_override"] is not None:
                manual += 1
                continue
            if photo["kind_source"] in ("filename", "camera", "animation", "sample") or (
                photo["category_version"] == MODEL_VERSION and photo["kind"] != "Needs review"
            ):
                unchanged += 1
                continue
            try:
                processed = process_image(
                    Path(photo["path"]), Path(photo["source_root"]), 480, category_models=self.directory
                )
                metadata = processed["metadata"]
                if metadata.get("category_error"):
                    raise ValueError(metadata["category_error"])
                if (metadata["size"], metadata["mtime_ns"]) != (photo["size"], photo["mtime_ns"]):
                    raise ValueError("Source changed since indexing. Rescan before detecting categories.")
                with self.library.connect() as db:
                    if processed.get("fingerprint"):
                        self.library.save_fingerprint(db, photo["id"], photo["sha256"], processed["fingerprint"])
                    changed = db.execute(
                        "UPDATE photos SET kind=?,kind_source=?,kind_score=?,kind_suggestion=?,category_version=? "
                        "WHERE id=? AND kind_override IS NULL AND sha256=?",
                        (
                            metadata["kind"],
                            metadata["kind_source"],
                            metadata["kind_score"],
                            metadata["kind_suggestion"],
                            metadata["category_version"],
                            photo["id"],
                            photo["sha256"],
                        ),
                    ).rowcount
                updated += changed
            except (OSError, ValueError) as error:
                errors.append({"file": photo["filename"], "reason": str(error)})
        counts = Counter(photo["kind"] for photo in self.library.photos(filters, limit=None)["photos"])
        progress(len(rows), len(rows), "Category detection complete")
        return {
            "categorized": updated,
            "kept_manual": manual,
            "already_classified": unchanged,
            "category_counts": dict(counts),
            "error_count": len(errors),
            "errors": errors[:30],
            "cancelled": cancelled(),
        }
