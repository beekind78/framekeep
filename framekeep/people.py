"""On-device YuNet detection and SFace similarity suggestions. No cloud inference."""

import base64
import json
import urllib.request
from pathlib import Path

import numpy as np

from .image_processing import process_image
from .library import file_hash

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

    def analyze(self, progress, cancelled):
        if not self.ready():
            raise ValueError("Download the people models first.")
        for name, (_, checksum) in MODELS.items():
            if file_hash(self.models / name) != checksum:
                raise ValueError("A people model is damaged. Download the models again.")
        with self.library.connect() as db:
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT p.*,l.path AS source_root FROM photos p JOIN libraries l ON l.id=p.library_id "
                    "WHERE p.missing=0 AND p.media_type='image' AND p.face_state='pending' ORDER BY p.id"
                )
            ]
            references = [
                (r["person_id"], np.array(json.loads(r["embedding"]), dtype=np.float32))
                for r in db.execute("SELECT person_id,embedding FROM faces")
            ]
        # Keep a bounded collection of references per person for large phone backups.
        exemplars = {}
        for person_id, vector in references:
            exemplars.setdefault(person_id, [])
            if len(exemplars[person_id]) < 8:
                exemplars[person_id].append(vector)
        errors, found = [], 0
        for index, photo in enumerate(rows):
            if cancelled():
                break
            progress(index, len(rows), photo["filename"])
            try:
                processed = process_image(
                    Path(photo["path"]), Path(photo["source_root"]), 1600, models=self.models
                )
                with self.library.connect() as db:
                    for face in processed["faces"]:
                        vector = np.array(face["embedding"], dtype=np.float32)
                        matches = [
                            (max(float(np.dot(vector, ref)) for ref in refs), pid)
                            for pid, refs in exemplars.items()
                        ]
                        best = max(matches, default=(-1, None))
                        # Stricter than OpenCV's example threshold: prefer extra groups over false merges.
                        person_id = best[1] if best[0] >= 0.48 else None
                        if person_id is None:
                            person_id = db.execute(
                                "INSERT INTO persons(name) VALUES ('Unnamed person')"
                            ).lastrowid
                            db.execute(
                                "UPDATE persons SET name=? WHERE id=?", (f"Person {person_id}", person_id)
                            )
                            exemplars[person_id] = []
                        if len(exemplars[person_id]) < 8:
                            exemplars[person_id].append(vector)
                        face_id = db.execute(
                            "INSERT INTO faces(photo_id,person_id,embedding) VALUES (?,?,?)",
                            (photo["id"], person_id, json.dumps(vector.tolist())),
                        ).lastrowid
                        (self.faces / f"{face_id}.jpg").write_bytes(
                            base64.b64decode(face["jpeg"], validate=True)
                        )
                        found += 1
                    db.execute("UPDATE photos SET face_state='done' WHERE id=?", (photo["id"],))
            except (OSError, ValueError) as error:
                errors.append({"file": photo["filename"], "reason": str(error)})
        progress(len(rows), len(rows), "People analysis complete")
        return {"faces": found, "error_count": len(errors), "errors": errors[:30], "cancelled": cancelled()}

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
