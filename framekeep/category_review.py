"""Preview exact-content groups and apply or undo category decisions locally."""

import hashlib
import json
import uuid
from collections import Counter
from datetime import datetime
from typing import Literal, get_args

from .category_model import MODEL_VERSION

Suggestion = Literal["Photos", "Screenshots", "Illustrations"]
SUGGESTIONS = get_args(Suggestion)
PAGE_SIZE = 24
HISTORY_SIZE = 12


class CategoryReview:
    def __init__(self, library):
        self.library = library

    def _rows(self, db, filters):
        where, args = self.library.where({**filters, "kind": "Needs review", "media_type": "image"})
        return db.execute(
            "SELECT p.id,p.filename,p.sha256,p.kind_source,p.kind_suggestion,p.kind_score,p.category_version "
            f"FROM photos p WHERE {where} AND p.kind_override IS NULL ORDER BY p.id", args,
        )

    def _queue(self, db, filters):
        pending, older = 0, 0
        groups = {kind: {} for kind in SUGGESTIONS}
        counts = Counter()
        for row in self._rows(db, filters):
            kind = row["kind_suggestion"]
            if row["kind_source"] != "uncertain" or kind not in groups:
                pending += 1
                continue
            older += row["category_version"] < MODEL_VERSION
            groups[kind].setdefault(row["sha256"], []).append(row)
            counts[kind] += 1
        return groups, {"pending": pending, "older": older, "suggestions": dict(counts)}

    @staticmethod
    def _revision(group):
        identities = [(row["id"], row["sha256"], row["kind_suggestion"], row["category_version"]) for row in group]
        return hashlib.sha256(json.dumps(identities).encode()).hexdigest()

    @staticmethod
    def _write_batch(db, label, rows):
        batch = uuid.uuid4().hex
        db.execute("INSERT INTO category_review_batches VALUES (?,?,?,?)",
                   (batch, label, len(rows), datetime.now().isoformat(timespec="seconds")))
        db.executemany("UPDATE photos SET kind_override=?,kind_batch=? WHERE id=?",
                       ((row["kind_suggestion"], batch, row["id"]) for row in rows))
        return {"batch_id": batch, "updated": len(rows)}

    def preview(self, filters, kind, offset):
        with self.library.connect() as db:
            groups, counts = self._queue(db, filters)
        kind = kind or max(SUGGESTIONS, key=lambda label: counts["suggestions"].get(label, 0))
        candidates = sorted(groups[kind].values(), key=lambda group: (
            -max(row["kind_score"] or 0 for row in group), group[0]["id"],
        ))
        total = len(candidates)
        offset = min(offset, max(0, (total - 1) // PAGE_SIZE) * PAGE_SIZE)
        return {**counts, "kind": kind, "offset": offset, "total": total, "page_size": PAGE_SIZE,
                "groups": [{"id": group[0]["id"], "filename": group[0]["filename"],
                            "count": len(group), "revision": self._revision(group),
                            "older": all(row["category_version"] < MODEL_VERSION for row in group)}
                           for group in candidates[offset:offset + PAGE_SIZE]]}

    def approve(self, filters, kind, selection):
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            grouped, _ = self._queue(db, filters)
            groups = {group[0]["id"]: group for group in grouped[kind].values()}
            rows = []
            for photo_id, revision in selection.items():
                group = groups.get(photo_id)
                if group is None or self._revision(group) != revision:
                    raise ValueError("The preview changed. Refresh the batch and review it again.")
                rows.extend(group)
            result = self._write_batch(db, kind, rows)
        self.library.invalidate_plans()
        return result

    def _all_suggestions(self, db, filters):
        return [row for row in self._rows(db, filters)
                if row["kind_source"] == "uncertain" and row["kind_suggestion"] in SUGGESTIONS]

    def preview_all(self, filters):
        with self.library.connect() as db:
            rows = self._all_suggestions(db, filters)
        return {"total": len(rows), "counts": dict(Counter(row["kind_suggestion"] for row in rows)),
                "older": sum(row["category_version"] < MODEL_VERSION for row in rows),
                "revision": self._revision(rows)}

    def approve_all(self, filters, revision):
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = self._all_suggestions(db, filters)
            if not rows or self._revision(rows) != revision:
                raise ValueError("The suggestions changed. Open a new preview before accepting them.")
            result = self._write_batch(db, "AI suggestions", rows)
        self.library.invalidate_plans()
        return result

    def history(self, offset):
        with self.library.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT b.*, (SELECT count(*) FROM photos p WHERE p.kind_batch=b.id) AS remaining "
                "FROM category_review_batches b ORDER BY b.rowid DESC LIMIT ? OFFSET ?", (HISTORY_SIZE + 1, offset),
            )]
        return {"batches": rows[:HISTORY_SIZE], "next": offset + HISTORY_SIZE if len(rows) > HISTORY_SIZE else None}

    def undo(self, batch):
        with self.library.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            record = db.execute("SELECT item_count FROM category_review_batches WHERE id=?", (batch,)).fetchone()
            if record is None:
                raise ValueError("This batch is no longer in the review history.")
            restored = db.execute("UPDATE photos SET kind_override=NULL,kind_batch=NULL WHERE kind_batch=?", (batch,)).rowcount
            db.execute("DELETE FROM category_review_batches WHERE id=?", (batch,))
        self.library.invalidate_plans()
        return {"restored": restored, "skipped": record["item_count"] - restored}
