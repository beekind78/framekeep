"""On-device YuNet detection and SFace similarity suggestions. No cloud inference."""

import base64
import json
import os
import urllib.request
from collections import Counter, OrderedDict
from pathlib import Path

import numpy as np

from .checkpoints import photo_identity, run_items
from .image_processing import process_faces
from .library import file_hash, scan_hash
from .source_files import file_identity, open_source, require_file_identity

ZOO_COMMIT = "48b10fa505aeb51fbd127fc60a5c660040e823b7"
MODELS = {
    "face_detection_yunet_2023mar.onnx": (
        "face_detection_yunet",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    ),
    "face_recognition_sface_2021dec.onnx": (
        "face_recognition_sface",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    ),
}


def embedding(value):
    vector = np.asarray(value, dtype=np.float32)
    if vector.shape != (128,) or not np.isfinite(vector).all():
        raise ValueError("Invalid face embedding.")
    return vector


class FaceMatcher:
    """Keep the first eight references per person and compare them in one vector operation."""

    def __init__(self):
        self.vectors = np.empty((64, 128), dtype=np.float32)
        self.people = np.empty(64, dtype=np.int64)
        self.counts = Counter()
        self.size = 0

    def add(self, person_id, vector):
        if self.counts[person_id] >= 8:
            return
        if self.size == len(self.people):
            self.people = np.resize(self.people, self.size * 2)
            self.vectors = np.resize(self.vectors, (self.size * 2, 128))
        self.people[self.size] = person_id
        self.vectors[self.size] = vector
        self.counts[person_id] += 1
        self.size += 1

    def restore(self, size):
        self.counts.subtract(self.people[size:self.size])
        self.size = size

    def match(self, vector):
        if not self.size:
            return None
        scores = np.einsum("ij,j->i", self.vectors[:self.size], vector)
        best = float(scores.max())
        # Keep the existing conservative cutoff and deterministic tie break.
        return int(self.people[:self.size][scores == best].max()) if best >= .48 else None


class People:
    def __init__(self, library):
        self.library = library
        self.models = library.data / "models"
        self.faces = library.data / "faces"
        self.faces.mkdir(exist_ok=True)

    def ready(self):
        return all((self.models / name).is_file() for name in MODELS)

    def install(self, progress, cancelled):
        self.models.mkdir(exist_ok=True)
        for index, (name, (folder, checksum)) in enumerate(MODELS.items()):
            target = self.models / name
            if target.exists() and file_hash(target) == checksum:
                continue
            progress(index, 2, f"Downloading {folder} (about 39 MB total)")
            url = f"https://media.githubusercontent.com/media/opencv/opencv_zoo/{ZOO_COMMIT}/models/{folder}/{name}"
            temporary = target.with_suffix(".download")
            try:
                with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as output:
                    received = 0
                    while block := response.read(256 * 1024):
                        if cancelled():
                            return {"cancelled": True}
                        received += len(block)
                        if received > 50 * 1024 * 1024:
                            raise ValueError("Model download exceeded its expected size.")
                        output.write(block)
                        progress(index, 2, f"Downloading {folder}: {received / 1024 / 1024:.1f} MB")
                if file_hash(temporary) != checksum:
                    raise ValueError("Model checksum mismatch. Please retry the download.")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        progress(2, 2, "People models ready")
        return {"ready": True}

    @staticmethod
    def _filters(scope):
        date = scope.get("date", "")
        return {"library": scope.get("library", ""), "year": date[:4], "month": date if len(date) == 7 else "",
                "kind": "Photos" if scope.get("photos_only") else "", "media_type": "image", "pending_faces": True}

    def queue(self, scope):
        where, args = self.library.where(self._filters(scope))
        with self.library.connect() as db:
            pending = db.execute(f"SELECT count(*) FROM photos p WHERE {where}", args).fetchone()[0]
        return {"pending": pending, "in_run": min(pending, scope.get("limit") or pending)}

    def analyze(self, progress, cancelled, *, scope=None, checkpoint=None):
        if not self.ready():
            raise ValueError("Download the people models first.")
        for name, (_, checksum) in MODELS.items():
            model = self.models / name
            if model.stat().st_size > 50 * 1024 * 1024 or file_hash(model) != checksum:
                raise ValueError("A people model is damaged. Download the models again.")
        scope = scope or {}
        rows = self.library.analysis_rows(self._filters(scope), checkpoint, limit=scope.get("limit") or None)
        matcher = self._references()
        duplicates = Counter(row["sha256"] for row in rows if row["face_state"] == "pending")
        cached = OrderedDict()

        def analyze_photo(photo):
            if photo["face_state"] == "done":
                # A restart can fall between committing the faces and recording the receipt.
                with self.library.connect() as db:
                    count = db.execute("SELECT count(*) FROM faces WHERE photo_id=?", (photo["id"],)).fetchone()[0]
                return {"faces": count}
            start = matcher.size
            digest = photo["sha256"]
            try:
                known = cached.get(digest)
                faces = self._detect(photo, known, cancelled)
                if faces is None:
                    return None
                self._save_faces(photo, faces, matcher)
                if duplicates[digest] > 1:
                    cached[digest] = faces
                    cached.move_to_end(digest)
                    if len(cached) > 32:
                        cached.popitem(last=False)
                return {"faces": len(faces), "reused": known is not None}
            except (OSError, ValueError, TypeError) as error:
                matcher.restore(start)
                return {"error": {"file": photo["filename"], "reason": str(error)}}

        outcomes = run_items(
            rows, photo_identity, analyze_photo, progress, cancelled, checkpoint,
            check_sources=lambda pending: self.library.check_analysis_sources(
                photo for photo in pending if photo["face_state"] == "pending"
            ),
        )
        errors = [item["error"] for item in outcomes if "error" in item]
        if not cancelled():
            progress(len(rows), len(rows), "People analysis complete")
        return {"faces": sum(item.get("faces", 0) for item in outcomes),
                "reused": sum(item.get("reused", False) for item in outcomes),
                "error_count": len(errors), "errors": errors[:30], "cancelled": cancelled()}

    def _references(self):
        matcher = FaceMatcher()
        with self.library.connect() as db:
            rows = db.execute(
                """SELECT person_id,embedding FROM (
                     SELECT f.person_id,f.embedding,row_number() OVER (PARTITION BY f.person_id ORDER BY f.id) AS rank
                     FROM faces f JOIN photos p ON p.id=f.photo_id WHERE p.missing=0
                   ) WHERE rank<=8"""
            )
            for row in rows:
                try:
                    matcher.add(row["person_id"], embedding(json.loads(row["embedding"])))
                except (ValueError, TypeError):
                    continue
        return matcher

    def _detect(self, photo, known, cancelled):
        path, root = Path(photo["path"]), Path(photo["source_root"])
        with open_source(path, root) as source:
            info = os.fstat(source.fileno())
            if (info.st_size, info.st_mtime_ns) != (photo["size"], photo["mtime_ns"]):
                raise ValueError("Source changed since indexing. Rescan before finding people.")
            identity = file_identity(info)
            faces = known if known is not None else process_faces(path, root, self.models, expected=identity)
            digest = scan_hash(source, info, cancelled)
            if digest is None:
                return None
            if digest != photo["sha256"]:
                raise ValueError("Source contents changed. Rescan before finding people.")
            with open_source(path, root) as current:
                require_file_identity(current, identity)
        return faces

    def _save_faces(self, photo, faces, matcher):
        with self.library.connect() as db:
            changed = db.execute(
                "UPDATE photos SET face_state='done' WHERE id=? AND sha256=? AND missing=0 AND face_state='pending'",
                (photo["id"], photo["sha256"]),
            ).rowcount
            if not changed:
                raise ValueError("The indexed photo changed. Rescan before finding people.")
            for face in faces:
                vector = embedding(face["embedding"])
                person_id = matcher.match(vector)
                if person_id is None:
                    person_id = db.execute("INSERT INTO persons(name) VALUES ('Unnamed person')").lastrowid
                    db.execute("UPDATE persons SET name=? WHERE id=?", (f"Person {person_id}", person_id))
                face_id = db.execute(
                    "INSERT INTO faces(photo_id,person_id,embedding) VALUES (?,?,?)",
                    (photo["id"], person_id, json.dumps(vector.tolist())),
                ).lastrowid
                (self.faces / f"{face_id}.jpg").write_bytes(base64.b64decode(face["jpeg"], validate=True))
                matcher.add(person_id, vector)

    def rename(self, person_id: int, name: str):
        name = name.strip()
        if not name or len(name) > 80:
            raise ValueError("Use a name between 1 and 80 characters.")
        with self.library.connect() as db:
            if not db.execute("UPDATE persons SET name=? WHERE id=?", (name, person_id)).rowcount:
                raise ValueError("Person not found.")

    def merge(self, source: int, target: int):
        if source == target:
            raise ValueError("Choose two different people.")
        with self.library.connect() as db:
            if (
                db.execute("SELECT count(*) FROM persons WHERE id IN (?,?)", (source, target)).fetchone()[0]
                != 2
            ):
                raise ValueError("Person not found.")
            db.execute("UPDATE faces SET person_id=? WHERE person_id=?", (target, source))
            db.execute("DELETE FROM persons WHERE id=?", (source,))

    def split(self, person_id: int, photo_id: int):
        with self.library.connect() as db:
            if not db.execute(
                "SELECT 1 FROM faces WHERE person_id=? AND photo_id=?", (person_id, photo_id)
            ).fetchone():
                raise ValueError("That photo is not in this group.")
            new_id = db.execute("INSERT INTO persons(name) VALUES ('Separated person')").lastrowid
            db.execute("UPDATE persons SET name=? WHERE id=?", (f"Person {new_id}", new_id))
            db.execute(
                "UPDATE faces SET person_id=? WHERE person_id=? AND photo_id=?", (new_id, person_id, photo_id)
            )
            db.execute("DELETE FROM persons WHERE id NOT IN (SELECT person_id FROM faces)")
        return {"person_id": new_id}
