"""Local category review and perceptual duplicate suggestions, using the bounded worker."""

import json
import threading
from pathlib import Path

from .category_model import MODEL_VERSION
from .checkpoints import photo_identity, run_items
from .image_processing import process_image
from .library import file_hash
from .visual_similarity import VERSION, compare, find_pairs


class Review:
    def __init__(self, library, categories):
        self.library, self.categories = library, categories
        self._cache_lock = threading.Lock()
        self._cached_rows, self._cached_result = None, None

    def analyze(self, filters, progress, cancelled, checkpoint=None):
        rows = self.library.analysis_rows({**filters, "media_type": "image", "duplicates": False}, checkpoint)
        ready = self.categories.ready()

        def analyze_photo(photo):
            manual = photo["kind_override"] is not None
            outcome = {"manual": manual}
            classify = ready and not manual and photo["kind_source"] not in (
                "filename", "camera", "animation", "sample"
            ) and photo["category_version"] < MODEL_VERSION
            with self.library.connect() as db:
                cached = db.execute(
                    "SELECT 1 FROM visual_fingerprints WHERE photo_id=? AND sha256=? AND version=?",
                    (photo["id"], photo["sha256"], VERSION),
                ).fetchone()
            if cached and not classify:
                return outcome
            try:
                processed = process_image(
                    Path(photo["path"]), Path(photo["source_root"]), 480,
                    category_models=self.categories.directory if classify else None,
                )
                metadata = processed["metadata"]
                if (metadata["size"], metadata["mtime_ns"]) != (photo["size"], photo["mtime_ns"]):
                    raise ValueError("Source changed since indexing. Rescan before analysis.")
                if file_hash(Path(photo["path"]), Path(photo["source_root"])) != photo["sha256"]:
                    raise ValueError("Source contents changed since indexing. Rescan before analysis.")
                with self.library.connect() as db:
                    current = db.execute(
                        "SELECT 1 FROM photos WHERE id=? AND sha256=? AND missing=0",
                        (photo["id"], photo["sha256"]),
                    ).fetchone()
                    if not current:
                        raise ValueError("The indexed image changed. Rescan before analysis.")
                    self.library.save_fingerprint(db, photo["id"], photo["sha256"], processed["fingerprint"])
                    updated = 0
                    if classify and not metadata.get("category_error"):
                        updated = db.execute(
                            "UPDATE photos SET kind=?,kind_source=?,kind_score=?,kind_suggestion=?,category_version=? "
                            "WHERE id=? AND sha256=? AND kind_override IS NULL",
                            (metadata["kind"], metadata["kind_source"], metadata["kind_score"],
                             metadata["kind_suggestion"], metadata["category_version"], photo["id"], photo["sha256"]),
                        ).rowcount
                outcome.update(reviewed=1, categorized=updated)
                if metadata.get("category_error"):
                    raise ValueError(metadata["category_error"])
            except (OSError, ValueError) as error:
                outcome["error"] = {"file": photo["filename"], "reason": str(error)}
            return outcome

        outcomes = run_items(rows, photo_identity, analyze_photo, progress, cancelled, checkpoint,
                             check_sources=self.library.check_analysis_sources)
        errors = [item["error"] for item in outcomes if "error" in item]
        if not cancelled():
            progress(len(rows), len(rows), "Local review complete")
        return {
            "reviewed": sum(item.get("reviewed", 0) for item in outcomes),
            "category_updates": sum(item.get("categorized", 0) for item in outcomes),
            "kept_manual": sum(item["manual"] for item in outcomes),
            "model_ready": ready, "error_count": len(errors), "errors": errors[:30], "cancelled": cancelled(),
        }

    def _rows(self, filters):
        scoped = {**filters, "media_type": "image", "duplicates": False}
        where, args = self.library.where(scoped)
        with self.library.connect() as db:
            total = db.execute(f"SELECT count(*) FROM photos p WHERE {where}", args).fetchone()[0]
            rows = [dict(row) for row in db.execute(
                "SELECT p.id,p.sha256,p.width,p.height,v.phash,v.dhash,v.aspect,v.color,v.texture "
                "FROM photos p JOIN visual_fingerprints v ON v.photo_id=p.id AND v.sha256=p.sha256 "
                f"WHERE {where} AND v.version=? ORDER BY p.id", [*args, VERSION],
            )]
        for row in rows:
            row["color"] = json.loads(row["color"])
        return total, rows

    def similar(self, filters, limit=20, offset=0):
        total, rows = self._rows(filters)
        with self._cache_lock:
            if self._cached_rows != rows:
                self._cached_result = find_pairs(rows)
                self._cached_rows = rows
            pairs, limited = self._cached_result
        page = pairs[offset:offset + limit]
        ids = sorted({pair[key] for pair in page for key in ("left", "right")})
        photos = self.library.photos({"ids": ids}, None)["photos"] if ids else []
        return {
            "pairs": page, "photos": photos, "total_pairs": len(pairs),
            "analyzed": len(rows), "image_total": total, "limited": limited,
        }

    def matches(self, photo_id):
        photo = self.library.photo(photo_id)
        with self.library.connect() as db:
            exact_ids = [row[0] for row in db.execute(
                "SELECT id FROM photos WHERE sha256=? AND missing=0 AND id!=? ORDER BY id LIMIT 12",
                (photo["sha256"], photo_id),
            )]
        exact = self.library.photos({"ids": exact_ids}, None)["photos"] if exact_ids else []
        _, rows = self._rows({}) if photo["media_type"] == "image" else (0, [])
        current = next((row for row in rows if row["id"] == photo_id), None)
        pairs = []
        if current:
            for row in rows:
                match = compare(current, row)
                if match:
                    pairs.append({"left": photo_id, "right": row["id"], **match})
        pairs.sort(key=lambda pair: (pair["phash_distance"] + pair["dhash_distance"], pair["right"]))
        ids = [pair["right"] for pair in pairs[:12]]
        return {
            "exact": exact, "exact_total": max(0, self.library.photos({"ids": [photo_id]}, 1)["photos"][0]["copies"] - 1),
            "similar": self.library.photos({"ids": ids}, None)["photos"] if ids else [],
            "similar_total": len(pairs), "analyzed": current is not None,
        }
