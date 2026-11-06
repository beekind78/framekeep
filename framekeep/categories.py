"""Download a pinned public model, then categorize photos entirely on this PC."""

import hashlib
import os
import urllib.request
from collections import Counter
from pathlib import Path

from .category_model import MODEL_BYTES, MODEL_NAME, MODEL_SHA256, MODEL_URL, MODEL_VERSION
from .checkpoints import photo_identity, run_items
from .image_processing import process_image
from .library import scan_hash
from .source_files import open_source


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
                        f"Downloading category model: {received / 1024**2:.1f} / {MODEL_BYTES / 1024**2:.1f} MiB",
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

    def analyze(self, filters, progress, cancelled, checkpoint=None):
        if not self.ready():
            raise ValueError("Download the category model first.")
        filters = {**filters, "media_type": "image"}
        rows = self.library.analysis_rows(filters, checkpoint)

        def analyze_photo(photo):
            if photo["kind_override"] is not None:
                return {"manual": 1}
            if photo["kind_source"] in ("filename", "camera", "animation", "sample") or (
                photo["category_version"] >= MODEL_VERSION
            ):
                return {"unchanged": 1}
            try:
                path, root = Path(photo["path"]), Path(photo["source_root"])
                metadata = self._matching_prediction(photo["sha256"])
                fingerprint = None
                if metadata is None:
                    processed = process_image(path, root, 480, category_models=self.directory)
                    metadata = processed["metadata"]
                    if metadata.get("category_error"):
                        raise ValueError(metadata["category_error"])
                    if (metadata["size"], metadata["mtime_ns"]) != (photo["size"], photo["mtime_ns"]):
                        raise ValueError("Source changed since indexing. Rescan before detecting categories.")
                    fingerprint = processed.get("fingerprint")
                with open_source(path, root) as source:
                    info = os.fstat(source.fileno())
                    if (info.st_size, info.st_mtime_ns) != (photo["size"], photo["mtime_ns"]):
                        raise ValueError("Source changed since indexing. Rescan before detecting categories.")
                    digest = scan_hash(source, info, cancelled)
                    if digest is None:
                        return None
                    if digest != photo["sha256"]:
                        raise ValueError("Source contents changed. Rescan before detecting categories.")
                with self.library.connect() as db:
                    if fingerprint:
                        self.library.save_fingerprint(db, photo["id"], photo["sha256"], fingerprint)
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
                return {"updated": changed, "kind": metadata["kind"]}
            except (OSError, ValueError) as error:
                return {"error": {"file": photo["filename"], "reason": str(error)}}

        outcomes = run_items(rows, photo_identity, analyze_photo, progress, cancelled, checkpoint,
                             check_sources=self.library.check_analysis_sources)
        errors = [item["error"] for item in outcomes if "error" in item]
        counts = Counter()
        if not cancelled():
            if filters.get("pending_categories"):
                counts.update(item["kind"] for item in outcomes if item.get("updated"))
            else:
                counts.update(photo["kind"] for photo in self.library.photos(filters, limit=None)["photos"])
            progress(len(rows), len(rows), "Category detection complete")
        return {
            "categorized": sum(item.get("updated", 0) for item in outcomes),
            "kept_manual": sum(item.get("manual", 0) for item in outcomes),
            "already_classified": sum(item.get("unchanged", 0) for item in outcomes),
            "category_counts": dict(counts),
            "error_count": len(errors),
            "errors": errors[:30],
            "cancelled": cancelled(),
        }

    def _matching_prediction(self, digest):
        with self.library.connect() as db:
            matched = db.execute(
                "SELECT kind,kind_source,kind_score,kind_suggestion,category_version FROM photos "
                "WHERE sha256=? AND missing=0 AND category_version=? AND kind_source IN ('model','uncertain') "
                "ORDER BY id LIMIT 1", (digest, MODEL_VERSION),
            ).fetchone()
        return matched
