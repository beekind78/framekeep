"""Link stills and motion only when their embedded Apple identifiers agree uniquely."""

from pathlib import Path

from .image_processing import process_photo_metadata, process_video
from .live_metadata import VERSION


class LivePhotos:
    def __init__(self, library):
        self.library = library

    def analyze(self, filters, progress, cancelled):
        from .library import file_hash

        scoped = {"library": filters.get("library", "")}
        rows = self.library.photos(scoped, None)["photos"]
        checked, errors = 0, []
        for index, photo in enumerate(rows):
            if cancelled():
                break
            progress(index, len(rows), photo["filename"])
            with self.library.connect() as db:
                known = db.execute(
                    "SELECT 1 FROM live_photo_metadata WHERE photo_id=? AND sha256=? AND version=?",
                    (photo["id"], photo["sha256"], VERSION),
                ).fetchone()
            if known:
                continue
            try:
                reader = process_video if photo["media_type"] == "video" else process_photo_metadata
                path, root = Path(photo["path"]), Path(photo["source_root"])
                metadata = reader(path, root)["metadata"]
                if (metadata["size"], metadata["mtime_ns"]) != (photo["size"], photo["mtime_ns"]):
                    raise ValueError("Source changed since indexing. Rescan before linking Live Photos.")
                if file_hash(path, root) != photo["sha256"]:
                    raise ValueError("Source contents changed. Rescan before linking Live Photos.")
                with self.library.connect() as db:
                    self.library.save_live_metadata(db, photo["id"], photo["sha256"], metadata)
                checked += 1
            except (OSError, ValueError) as error:
                errors.append({"file": photo["filename"], "reason": str(error)})
        libraries = {photo["library_id"] for photo in rows}
        for library_id in libraries:
            self.library.rebuild_live_pairs(library_id)
        with self.library.connect() as db:
            pairs = [pair for pair in self.library.live_pairs(db) if pair["library_id"] in libraries]
        progress(len(rows), len(rows), "Live Photo linking complete")
        return {"live_photos": len(pairs), "metadata_checked": checked, "error_count": len(errors),
                "errors": errors[:30], "cancelled": cancelled()}
