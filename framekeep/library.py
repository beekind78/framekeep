"""Local indexing, metadata, queries, and copy-only organization."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from pillow_heif import register_heif_opener

from .category_model import CATEGORIES, MODEL_NAME, MODEL_VERSION, initial_category
from .checkpoints import run_items
from .image_processing import MAX_FILE_BYTES, POLICY_VERSION, TruncatedJPEGError, process_image, process_video
from .live_metadata import VERSION as LIVE_VERSION
from .media_types import PHOTO_EXTENSIONS, media_kind
from .output_files import (
    ApprovedOutput,
    new_output_file,
    new_output_folder,
    output_directory,
    pinned_directory,
)
from .paths import recovery_directory
from .source_files import file_identity, open_source, require_file_identity, source_entries

register_heif_opener()
EXTENSIONS = PHOTO_EXTENSIONS
MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
CATEGORY_FOLDERS = {
    "Photos": "Photos",
    "Screenshots": "Screenshot",
    "Illustrations": "Illustration",
    "Videos": "Video",
}
LIVE_STILL = (
    "EXISTS (SELECT 1 FROM live_photo_pairs lp JOIN photos m ON m.id=lp.motion_id "
    "WHERE lp.still_id=p.id AND lp.still_sha=p.sha256 AND lp.motion_sha=m.sha256 AND m.missing=0)"
)
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS libraries (
 id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
 demo INTEGER NOT NULL DEFAULT 0, scanned_at TEXT
);
CREATE TABLE IF NOT EXISTS forgotten_id_limits (
 table_name TEXT PRIMARY KEY, highest_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS category_review_batches (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, item_count INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS photos (
 id INTEGER PRIMARY KEY, library_id INTEGER NOT NULL REFERENCES libraries(id),
 path TEXT UNIQUE NOT NULL, filename TEXT NOT NULL, extension TEXT NOT NULL,
 size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, captured_at TEXT NOT NULL,
 date_source TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
 camera TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, sha256 TEXT NOT NULL,
 favorite INTEGER NOT NULL DEFAULT 0, tags TEXT NOT NULL DEFAULT '',
 missing INTEGER NOT NULL DEFAULT 0, face_state TEXT NOT NULL DEFAULT 'pending',
 policy_version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS photos_dates ON photos(captured_at);
CREATE INDEX IF NOT EXISTS photos_hashes ON photos(sha256);
CREATE INDEX IF NOT EXISTS photos_library ON photos(library_id);
CREATE TABLE IF NOT EXISTS persons (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS faces (
 id INTEGER PRIMARY KEY, photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
 person_id INTEGER NOT NULL REFERENCES persons(id), embedding TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS faces_person ON faces(person_id);
CREATE INDEX IF NOT EXISTS faces_photo ON faces(photo_id);
CREATE TABLE IF NOT EXISTS visual_fingerprints (
 photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
 sha256 TEXT NOT NULL, phash TEXT NOT NULL, dhash TEXT NOT NULL,
 aspect REAL NOT NULL, color TEXT NOT NULL, texture REAL NOT NULL, version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS live_photo_metadata (
 photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
 sha256 TEXT NOT NULL, identifier TEXT NOT NULL, version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS live_photo_pairs (
 still_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
 motion_id INTEGER UNIQUE NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
 still_sha TEXT NOT NULL, motion_sha TEXT NOT NULL
);
"""


def file_hash(path: Path, root: Path | None = None) -> str:
    digest = hashlib.sha256()
    with open_source(path, root) if root is not None else path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def scan_hash(stream, info, cancelled) -> str | None:
    digest = hashlib.sha256()
    remaining = info.st_size
    while remaining:
        if cancelled():
            return None
        block = stream.read(min(remaining, 1024 * 1024))
        if not block:
            raise ValueError("File changed during indexing; rescan to retry.")
        remaining -= len(block)
        digest.update(block)
    require_file_identity(stream, file_identity(info))
    return digest.hexdigest() if not cancelled() else None


def copy_verified_original(
    source: Path, source_root: Path, sha256: str, root: Path | ApprovedOutput, relative: Path,
) -> Path:
    with open_source(source, source_root) as input_file:
        info = os.fstat(input_file.fileno())
        if hashlib.file_digest(input_file, "sha256").hexdigest() != sha256:
            raise ValueError("Source changed since indexing. Rescan before copying.")
        input_file.seek(0)
        with new_output_file(root, relative, first_suffix=1) as (target, output):
            shutil.copyfileobj(input_file, output, 1024 * 1024)
            output.flush()
            output.seek(0)
            if hashlib.file_digest(output, "sha256").hexdigest() != sha256:
                raise ValueError("Copy verification failed.")
            os.utime(target if os.name == "nt" else output.fileno(), ns=(info.st_atime_ns, info.st_mtime_ns))
    return target


def date_metadata(image: Image.Image, path: Path, modified: float) -> tuple[str, str, str]:
    """Return a date with its provenance; never claim a fallback is an EXIF date."""
    exif = image.getexif()
    try:
        extra = exif.get_ifd(34665)
    except (KeyError, TypeError, ValueError, SyntaxError):
        extra = {}
    for key in (36867, 36868, 306):
        value = extra.get(key) or exif.get(key)
        if value:
            try:
                date = datetime.strptime(str(value).strip()[:19], "%Y:%m:%d %H:%M:%S")
                if date.year >= 1900:
                    return date.isoformat(), "Camera metadata", camera_name(exif)
            except ValueError:
                pass
    date, source = fallback_date(path, modified)
    return date, source, camera_name(exif)


def fallback_date(path: Path, modified: float) -> tuple[str, str]:
    # IMG_20200131_123456, WhatsApp IMG-20200131-WA0001, and ISO names.
    match = re.search(
        r"(?<!\d)((?:19|20)\d{2})[-_]?([01]\d)[-_]?([0-3]\d)(?:[T_ -]([0-2]\d)[-:]?([0-5]\d)[-:]?([0-5]\d))?",
        path.stem,
    )
    if match:
        try:
            values = [int(v or 0) for v in match.groups()]
            return datetime(*values).isoformat(), "Filename"
        except ValueError:
            pass
    return datetime.fromtimestamp(modified).isoformat(timespec="seconds"), "File modified"


def camera_name(exif) -> str:
    parts = [str(exif.get(key, "")).strip() for key in (271, 272)]
    return " ".join(dict.fromkeys(part for part in parts if part))


def safe_segment(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:90].rstrip(" .")
    if not value or value.upper().split(".")[0] in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        return "Unnamed"
    return value


class Library:
    def __init__(self, data: Path):
        self.data = data.resolve()
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.thumbs = self.data / "thumbnails"
        self.thumbs.mkdir(parents=True, exist_ok=True)
        self.database = self.data / "library.sqlite3"
        with self.connect() as db:
            db.executescript(SCHEMA)
            if "policy_version" not in {row[1] for row in db.execute("PRAGMA table_info(photos)")}:
                db.execute("ALTER TABLE photos ADD COLUMN policy_version INTEGER NOT NULL DEFAULT 0")
            columns = {row[1] for row in db.execute("PRAGMA table_info(photos)")}
            for name, declaration in {
                "kind_source": "TEXT NOT NULL DEFAULT 'pending'",
                "kind_score": "REAL",
                "kind_suggestion": "TEXT",
                "category_version": "INTEGER NOT NULL DEFAULT 0",
                "kind_override": "TEXT",
                "media_type": "TEXT NOT NULL DEFAULT 'image'",
                "duration": "REAL NOT NULL DEFAULT 0",
                "removed_group": "TEXT",
                "kind_batch": "TEXT",
            }.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE photos ADD COLUMN {name} {declaration}")
            if "removed" not in {row[1] for row in db.execute("PRAGMA table_info(libraries)")}:
                db.execute("ALTER TABLE libraries ADD COLUMN removed INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE INDEX IF NOT EXISTS photos_category_batch ON photos(kind_batch) WHERE kind_batch IS NOT NULL")
            if "kind_source" not in columns:
                for row in db.execute(
                    "SELECT p.id,p.filename,p.camera,p.kind,l.demo FROM photos p JOIN libraries l ON l.id=p.library_id"
                ).fetchall():
                    category = initial_category(
                        Path(row["filename"]), row["camera"], row["kind"] == "Animated"
                    )
                    if row["demo"] and category["kind"] not in ("Screenshots", "Animated"):
                        category.update(kind="Illustrations", kind_source="sample")
                    db.execute(
                        "UPDATE photos SET kind=?,kind_source=?,kind_suggestion=? WHERE id=?",
                        (category["kind"], category["kind_source"], category["kind"], row["id"]),
                    )
        self.plans: dict[str, dict] = {}
        self.plan_lock = threading.Lock()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def next_catalog_id(db, table):
        # Keep stale gallery and restore actions from targeting an unrelated reimport.
        return db.execute(
            f"SELECT max(COALESCE((SELECT max(id) FROM {table}),0),"
            "COALESCE((SELECT highest_id FROM forgotten_id_limits WHERE table_name=?),0))+1",
            (table,),
        ).fetchone()[0]

    def add(self, source: str, demo: bool = False) -> int:
        path = Path(source).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError("Choose an existing folder.")
        if not demo and (path.is_relative_to(self.data) or self.data.is_relative_to(path)):
            raise ValueError("Choose a photo folder outside Framekeep's application data.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT id, path FROM libraries"):
                existing = Path(row["path"])
                if existing == path:
                    db.execute("UPDATE libraries SET removed=0 WHERE id=?", (row["id"],))
                    return row["id"]
                if existing.is_relative_to(path) or path.is_relative_to(existing):
                    raise ValueError("This folder overlaps an existing library. Rescan that library instead.")
            return db.execute(
                "INSERT INTO libraries(id,path,name,demo) VALUES (?,?,?,?)",
                (self.next_catalog_id(db, "libraries"), str(path), path.name, demo),
            ).lastrowid

    def scan(self, library_id: int, progress, cancelled, checkpoint=None):
        with self.connect() as db:
            library = db.execute("SELECT * FROM libraries WHERE id=? AND removed=0", (library_id,)).fetchone()
        if library is None:
            raise ValueError("This source was removed from Framekeep. Restore it before rescanning.")
        root = Path(library["path"])
        category_models = self.data / "models" / "categories"
        detect_categories = not library["demo"] and (category_models / MODEL_NAME).is_file()
        traversal_errors, skipped_entries = [], []
        try:
            if checkpoint:
                with pinned_directory(root) as (_, descriptor):
                    info = os.fstat(descriptor)
                    identity = [info.st_dev, info.st_ino]
                    scope = checkpoint.scope()
                    if scope is None:
                        checkpoint.save_scope(identity)
                    elif json.loads(scope) != identity:
                        raise ValueError("Reconnect the original source folder, or discard this task and rescan.")
            files = list(source_entries(root, traversal_errors.append, skipped_entries.append, cancelled))
        except FileNotFoundError:
            raise ValueError("The source folder is unavailable. Reconnect your drive and try again.") from None
        if cancelled():
            return {"cancelled": True, "indexed": 0}
        errors = [{"file": "Folder access", "reason": e} for e in traversal_errors + skipped_entries]
        outcomes = run_items(
            files,
            lambda item: (str(item[0]), json.dumps([POLICY_VERSION, *file_identity(item[1])]), item[0].name),
            lambda item: self._scan_file(item[0], root, library_id, library["demo"],
                                         category_models if detect_categories else None, cancelled),
            progress, cancelled, checkpoint,
        )
        errors.extend(outcome["error"] for outcome in outcomes if "error" in outcome)
        indexed = sum(outcome.get("indexed", 0) for outcome in outcomes)
        if cancelled():
            return {"cancelled": True, "indexed": indexed, "errors": errors[:30]}
        seen = {str(path) for path, _ in files}
        with self.connect() as db:
            # Do not mark unreadable subtrees missing after a partial directory walk.
            if not traversal_errors:
                for row in db.execute(
                    "SELECT id,path FROM photos WHERE library_id=?", (library_id,)
                ).fetchall():
                    if row["path"] not in seen:
                        db.execute("UPDATE photos SET missing=1 WHERE id=?", (row["id"],))
            db.execute(
                "UPDATE libraries SET scanned_at=? WHERE id=?", (datetime.now().isoformat(), library_id)
            )
            db.execute("DELETE FROM persons WHERE id NOT IN (SELECT person_id FROM faces)")
        progress(len(files), len(files), "Scan complete")
        self.rebuild_live_pairs(library_id)
        media = Counter(outcome.get("media") for outcome in outcomes)
        skipped = Counter(outcome.get("skipped") for outcome in outcomes)
        return {
            "library_id": library_id,
            "indexed": indexed,
            "found": media["photo"],
            "videos": media["video"],
            "media_total": media["photo"] + media["video"],
            "scanned_files": len(files),
            "extension_mismatches": sum(outcome.get("mismatch", False) for outcome in outcomes),
            "skipped_media": {name: skipped[name] for name in ("videos", "metadata", "other")},
            "error_count": len(errors),
            "errors": errors[:30],
            "jpeg_recovery": {"library_id": library_id,
                              "files": [outcome["recovery"] for outcome in outcomes if "recovery" in outcome]},
        }

    def _scan_file(self, path, root, library_id, demo, category_models, cancelled):
        try:
            with open_source(path, root) as stream:
                stat = os.fstat(stream.fileno())
                kind = media_kind(stream.read(128))
            is_video = kind == "video"
            if kind == "metadata" or (kind == "unknown" and path.suffix.lower() not in EXTENSIONS):
                category = {"video": "videos", "metadata": "metadata"}.get(kind, "other")
                with self.connect() as db:
                    db.execute("UPDATE photos SET missing=1 WHERE path=?", (str(path),))
                return {"skipped": category}
            if not is_video and not 0 < stat.st_size <= MAX_FILE_BYTES:
                raise ValueError("Image exceeds the 256 MiB encoded-file limit.")
            with self.connect() as db:
                existing = db.execute("SELECT * FROM photos WHERE path=?", (str(path),)).fetchone()
                if existing and existing["removed_group"]:
                    return {}
                if (
                    existing
                    and existing["mtime_ns"] == stat.st_mtime_ns
                    and existing["size"] == stat.st_size
                    and existing["policy_version"] == POLICY_VERSION
                    and existing["media_type"] == ("video" if is_video else "image")
                    and (is_video or (self.thumbs / f"{existing['id']}.jpg").is_file())
                ):
                    db.execute("UPDATE photos SET missing=0 WHERE id=?", (existing["id"],))
                    return {"media": "video" if is_video else "photo",
                            "mismatch": existing["extension"].lower() != path.suffix.lstrip(".").lower()}
            expected = file_identity(stat)
            with open_source(path, root) as stream:
                require_file_identity(stream, expected)
                processed = (
                    process_video(path, root, expected=expected)
                    if is_video
                    else process_image(
                        path, root, 480, category_models=category_models,
                        expected=expected,
                    )
                )
                digest = scan_hash(stream, stat, cancelled)
                if digest is None:
                    return None
                with open_source(path, root) as current_file:
                    require_file_identity(current_file, expected)
            metadata = processed["metadata"]
            if demo and metadata["kind"] not in ("Screenshots", "Animated"):
                metadata.update(
                    kind="Illustrations", kind_source="sample", kind_suggestion="Illustrations"
                )
            if (metadata["mtime_ns"], metadata["size"]) != (stat.st_mtime_ns, stat.st_size):
                raise ValueError("File changed during indexing; rescan to retry.")
            date, source, camera = metadata["captured_at"], metadata["date_source"], metadata["camera"]
            width, height, kind = metadata["width"], metadata["height"], metadata["kind"]
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT id FROM photos WHERE path=?", (str(path),)).fetchone()
                photo_id = current["id"] if current else self.next_catalog_id(db, "photos")
                values = (
                    photo_id,
                    library_id,
                    str(path),
                    path.name,
                    metadata["extension"],
                    stat.st_size,
                    stat.st_mtime_ns,
                    date,
                    source,
                    width,
                    height,
                    camera,
                    kind,
                    digest,
                    metadata["kind_source"],
                    metadata["kind_score"],
                    metadata["kind_suggestion"],
                    metadata["category_version"],
                    "video" if is_video else "image",
                    metadata.get("duration", 0),
                )
                db.execute(
                    """INSERT INTO photos(id,library_id,path,filename,extension,size,mtime_ns,captured_at,date_source,width,height,camera,kind,sha256,kind_source,kind_score,kind_suggestion,category_version,media_type,duration)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                    extension=excluded.extension,size=excluded.size,mtime_ns=excluded.mtime_ns,captured_at=excluded.captured_at,
                    media_type=excluded.media_type,duration=excluded.duration,
                    date_source=excluded.date_source,width=excluded.width,height=excluded.height,
                    camera=excluded.camera,
                    kind=CASE WHEN photos.sha256=excluded.sha256 AND photos.category_version>=excluded.category_version AND photos.category_version>0 THEN photos.kind ELSE excluded.kind END,
                    kind_source=CASE WHEN photos.sha256=excluded.sha256 AND photos.category_version>=excluded.category_version AND photos.category_version>0 THEN photos.kind_source ELSE excluded.kind_source END,
                    kind_score=CASE WHEN photos.sha256=excluded.sha256 AND photos.category_version>=excluded.category_version AND photos.category_version>0 THEN photos.kind_score ELSE excluded.kind_score END,
                    kind_suggestion=CASE WHEN photos.sha256=excluded.sha256 AND photos.category_version>=excluded.category_version AND photos.category_version>0 THEN photos.kind_suggestion ELSE excluded.kind_suggestion END,
                    category_version=CASE WHEN photos.sha256=excluded.sha256 AND photos.category_version>=excluded.category_version THEN photos.category_version ELSE excluded.category_version END,
                    kind_override=CASE WHEN photos.sha256=excluded.sha256 THEN photos.kind_override ELSE NULL END,
                    kind_batch=CASE WHEN photos.sha256=excluded.sha256 THEN photos.kind_batch ELSE NULL END,
                    sha256=excluded.sha256,missing=0,face_state='pending'""",
                    values,
                )
                db.execute("DELETE FROM faces WHERE photo_id=?", (photo_id,))
                db.execute("UPDATE photos SET policy_version=? WHERE id=?", (POLICY_VERSION, photo_id))
                if "live_identifier" in metadata:
                    self.save_live_metadata(db, photo_id, digest, metadata)
                if not is_video:
                    self._save_thumbnail(photo_id, processed["jpeg"])
                    if processed.get("fingerprint"):
                        self.save_fingerprint(db, photo_id, digest, processed["fingerprint"])
            return {"indexed": 1, "media": "video" if is_video else "photo",
                    "mismatch": metadata["extension"].lower() != path.suffix.lstrip(".").lower()}
        except (
            OSError,
            ValueError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
            SyntaxError,
        ) as error:
            outcome = {"error": {"file": path.name, "reason": str(error)}}
            if isinstance(error, TruncatedJPEGError):
                try:
                    with open_source(path, root) as stream:
                        current = os.fstat(stream.fileno())
                        if file_identity(current) == file_identity(stat):
                            digest = scan_hash(stream, current, cancelled)
                            if digest is None:
                                return None
                            outcome["recovery"] = {
                                "path": str(path), "size": current.st_size, "mtime_ns": current.st_mtime_ns,
                                "sha256": digest,
                            }
                except (OSError, ValueError):
                    pass  # A changed or unavailable file can be checked again on the next scan.
            # A changed, unreadable file must not keep stale metadata or face matches.
            with self.connect() as db:
                db.execute("UPDATE photos SET missing=1 WHERE path=?", (str(path),))
            return outcome

    def _save_thumbnail(self, photo_id, jpeg):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.thumbs, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(jpeg)
            temporary.replace(self.thumbs / f"{photo_id}.jpg")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def analysis_rows(self, filters, checkpoint, limit=None):
        scope = checkpoint.scope() if checkpoint else None
        if scope is None:
            rows = self.photos(filters, limit)["photos"]
            if checkpoint:
                checkpoint.save_scope([row["id"] for row in rows])
            return rows
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT p.*,l.path AS source_root FROM json_each(?) work JOIN photos p ON p.id=work.value "
                "JOIN libraries l ON l.id=p.library_id WHERE p.missing=0 AND l.removed=0 ORDER BY work.key", (scope,)
            )]

    @staticmethod
    def check_analysis_sources(rows):
        for source in sorted({row["source_root"] for row in rows}):
            try:
                with pinned_directory(Path(source)):
                    pass
            except (OSError, ValueError):
                raise ValueError(f"Reconnect the source folder {source} before resuming.") from None

    def where(self, filters: dict):
        terms, args = ["p.missing=0"], []
        if filters.get("pending_faces"):
            terms.append("p.face_state='pending'")
        if filters.get("pending_categories") == "new":
            terms.append("p.kind_override IS NULL AND p.category_version=0 AND p.kind_source='pending'")
        elif filters.get("pending_categories") == "older":
            terms.append("p.kind_override IS NULL AND p.kind_source='uncertain' AND p.category_version<?")
            args.append(MODEL_VERSION)
        if filters.get("collapse_live"):
            terms.append("NOT EXISTS (SELECT 1 FROM live_photo_pairs lp JOIN photos s ON s.id=lp.still_id "
                         "WHERE lp.motion_id=p.id AND lp.motion_sha=p.sha256 AND lp.still_sha=s.sha256 AND s.missing=0)")
        if filters.get("live_only"):
            terms.append(LIVE_STILL)
        for key, expression in {
            "library": "p.library_id=?",
            "year": "substr(p.captured_at,1,4)=?",
            "month": "substr(p.captured_at,1,7)=?",
            "type": "p.extension=?",
            "kind": "COALESCE(p.kind_override,p.kind)=?",
            "media_type": "p.media_type=?",
        }.items():
            if filters.get(key):
                terms.append(expression)
                args.append(str(filters[key]))
        if filters.get("q"):
            terms.append(
                "(p.filename LIKE ? ESCAPE '\\' OR p.tags LIKE ? ESCAPE '\\' OR p.camera LIKE ? ESCAPE '\\')"
            )
            query = str(filters["q"]).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args.extend([f"%{query}%"] * 3)
        if filters.get("favorite"):
            terms.append("p.favorite=1")
        if filters.get("duplicates"):
            terms.append(
                "p.sha256 IN (SELECT sha256 FROM photos WHERE missing=0 GROUP BY sha256 HAVING count(*)>1)"
            )
        if filters.get("person"):
            terms.append("p.id IN (SELECT photo_id FROM faces WHERE person_id=?)")
            args.append(int(filters["person"]))
        if filters.get("ids"):
            ids = filters["ids"]
            terms.append(f"p.id IN ({','.join('?' for _ in ids)})")
            args.extend(ids)
        return " AND ".join(terms), args

    def photos(self, filters: dict, limit: int | None = 120, offset: int = 0):
        where, args = self.where(filters)
        orders = {
            "newest": "p.captured_at DESC,p.id DESC",
            "oldest": "p.captured_at,p.id",
            "name": "p.filename COLLATE NOCASE,p.id",
            "size": "p.size DESC,p.id DESC",
        }
        order = orders.get(filters.get("sort"), orders["newest"])
        with self.connect() as db:
            total = db.execute(f"SELECT count(*) FROM photos p WHERE {where}", args).fetchone()[0]
            facet_where, facet_args = self.where({**filters, "kind": ""})
            kind_counts = [
                dict(row)
                for row in db.execute(
                    "SELECT COALESCE(p.kind_override,p.kind) AS value,count(*) AS count FROM photos p "
                    f"WHERE {facet_where} GROUP BY value ORDER BY value",
                    facet_args,
                )
            ]
            sql = f"""SELECT p.*, (SELECT path FROM libraries WHERE id=p.library_id) AS source_root,
                (SELECT count(*) FROM photos d WHERE d.sha256=p.sha256 AND d.missing=0) AS copies,
                (SELECT group_concat(person_id) FROM (SELECT DISTINCT person_id FROM faces WHERE photo_id=p.id)) AS person_ids
                FROM photos p WHERE {where} ORDER BY {order}"""
            if limit is not None:
                sql += " LIMIT ? OFFSET ?"
                args += [limit, offset]
            rows = [dict(row) for row in db.execute(sql, args)]
        for row in rows:
            row["automatic_kind"] = row["kind"]
            row["kind"] = row["kind_override"] or row["kind"]
            row["person_ids"] = [int(x) for x in (row["person_ids"] or "").split(",") if x]
        self.decorate_live(rows)
        return {"total": total, "photos": rows, "kind_counts": kind_counts}

    @staticmethod
    def save_live_metadata(db, photo_id, sha256, metadata):
        db.execute("INSERT OR REPLACE INTO live_photo_metadata(photo_id,sha256,identifier,version) VALUES (?,?,?,?)",
                   (photo_id, sha256, metadata.get("live_identifier", ""), LIVE_VERSION))

    @staticmethod
    def live_pairs(db):
        return [dict(row) for row in db.execute(
            "SELECT lp.*,s.library_id FROM live_photo_pairs lp JOIN photos s ON s.id=lp.still_id "
            "JOIN photos m ON m.id=lp.motion_id WHERE s.missing=0 AND m.missing=0 "
            "AND s.sha256=lp.still_sha AND m.sha256=lp.motion_sha AND s.library_id=m.library_id"
        )]

    def rebuild_live_pairs(self, library_id):
        with self.connect() as db:
            db.execute("DELETE FROM live_photo_pairs WHERE still_id IN (SELECT id FROM photos WHERE library_id=?) "
                       "OR motion_id IN (SELECT id FROM photos WHERE library_id=?)", (library_id, library_id))
            rows = db.execute(
                "SELECT p.id,p.sha256,p.media_type,l.identifier FROM photos p JOIN live_photo_metadata l "
                "ON l.photo_id=p.id AND l.sha256=p.sha256 WHERE p.library_id=? AND p.missing=0 "
                "AND l.identifier!='' AND l.version=? ORDER BY p.id", (library_id, LIVE_VERSION)
            )
            groups = {}
            for row in rows:
                groups.setdefault(row["identifier"], {"image": [], "video": []})[row["media_type"]].append(row)
            for group in groups.values():
                if len(group["image"]) == len(group["video"]) == 1:
                    still, motion = group["image"][0], group["video"][0]
                    db.execute("INSERT INTO live_photo_pairs(still_id,motion_id,still_sha,motion_sha) VALUES (?,?,?,?)",
                               (still["id"], motion["id"], still["sha256"], motion["sha256"]))

    def decorate_live(self, rows):
        with self.connect() as db:
            pairs = self.live_pairs(db)
        stills = {pair["still_id"]: pair["motion_id"] for pair in pairs}
        motions = {pair["motion_id"]: pair["still_id"] for pair in pairs}
        for row in rows:
            row["live_motion_id"] = stills.get(row["id"])
            row["live_still_id"] = motions.get(row["id"])

    @staticmethod
    def save_fingerprint(db, photo_id, sha256, fingerprint):
        db.execute(
            "INSERT OR REPLACE INTO visual_fingerprints(photo_id,sha256,phash,dhash,aspect,color,texture,version) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (photo_id, sha256, fingerprint["phash"], fingerprint["dhash"], fingerprint["aspect"],
             json.dumps(fingerprint["color"]), fingerprint["texture"], fingerprint["version"]),
        )

    def albums(self, filters: dict):
        """Read automatic category/date albums without creating or moving files."""
        # Album counts always represent memories, including each Live Photo once.
        where, args = self.where({**filters, "collapse_live": True})
        grouped = {}
        with self.connect() as db:
            months = db.execute(
                "SELECT COALESCE(p.kind_override,p.kind) AS kind,substr(p.captured_at,1,7) AS month,"
                "count(*) AS count,sum(p.media_type='image') AS photos,sum(p.media_type='video') AS videos,"
                f"sum({LIVE_STILL}) AS live,"
                "sum(p.size) AS bytes FROM photos p "
                f"WHERE {where} GROUP BY COALESCE(p.kind_override,p.kind),substr(p.captured_at,1,7) ORDER BY month DESC",
                args,
            ).fetchall()
            for row in months:
                album = grouped.setdefault(
                    row["kind"],
                    {
                        "kind": row["kind"],
                        "count": 0,
                        "photos": 0,
                        "videos": 0,
                        "live": 0,
                        "bytes": 0,
                        "months": [],
                        "years": [],
                        "covers": [],
                    },
                )
                for field in ("count", "photos", "videos", "live", "bytes"):
                    album[field] += row[field]
                album["months"].append({"value": row["month"], "count": row["count"]})
            for kind, album in grouped.items():
                year_counts = {}
                for month in album["months"]:
                    year = month["value"][:4]
                    year_counts[year] = year_counts.get(year, 0) + month["count"]
                album["years"] = [
                    {"value": year, "count": count}
                    for year, count in sorted(year_counts.items(), reverse=True)
                ]
                album["covers"] = [
                    dict(row)
                    for row in db.execute(
                        f"SELECT p.id,p.filename,p.media_type,p.duration,p.mtime_ns,{LIVE_STILL} AS live FROM photos p "
                        f"WHERE {where} AND COALESCE(p.kind_override,p.kind)=? "
                        "ORDER BY p.captured_at DESC,p.id DESC LIMIT 4",
                        [*args, kind],
                    )
                ]
        order = ("Photos", "Screenshots", "Illustrations", "Downloads", "Videos", "Needs review", "Animated")
        albums = sorted(
            grouped.values(),
            key=lambda album: (
                order.index(album["kind"]) if album["kind"] in order else len(order),
                album["kind"],
            ),
        )
        return {
            "total": sum(a["count"] for a in albums),
            "photos": sum(a["photos"] for a in albums),
            "videos": sum(a["videos"] for a in albums),
            "live": sum(a["live"] for a in albums),
            "bytes": sum(a["bytes"] for a in albums),
            "albums": albums,
        }

    def photo(self, photo_id: int):
        with self.connect() as db:
            row = db.execute(
                "SELECT p.*, l.path AS source_root FROM photos p JOIN libraries l ON l.id=p.library_id "
                "WHERE p.id=? AND p.missing=0",
                (photo_id,),
            ).fetchone()
            if row is None:
                raise ValueError("This photo is unavailable. Rescan the source folder.")
            result = dict(row)
            result["automatic_kind"] = result["kind"]
            result["kind"] = result["kind_override"] or result["kind"]
            self.decorate_live([result])
            return result

    def summary(self):
        visible, _ = self.where({"collapse_live": True})
        with self.connect() as db:
            counts = dict(
                db.execute("""SELECT count(*) AS total, coalesce(sum(media_type='image'),0) AS image_total,
                coalesce(sum(media_type='video'),0) AS video_total, coalesce(sum(size),0) AS bytes,
                coalesce(sum(favorite),0) AS favorites, coalesce(sum(date_source='File modified'),0) AS fallback_dates
                FROM photos p WHERE """ + visible).fetchone()
            )
            counts["file_total"] = db.execute("SELECT count(*) FROM photos WHERE missing=0").fetchone()[0]
            counts["bytes"] = db.execute("SELECT coalesce(sum(size),0) FROM photos WHERE missing=0").fetchone()[0]
            counts["live_total"] = len(self.live_pairs(db))
            duplicate = dict(
                db.execute("""SELECT coalesce(sum(n-1),0) AS extra_copies,coalesce(sum(n),0) AS photos,
                count(*) AS groups, coalesce(sum((n-1)*size),0) AS bytes
                FROM (SELECT count(*) n, size FROM photos WHERE missing=0 GROUP BY sha256 HAVING count(*)>1)""").fetchone()
            )
            facets = {}
            for key, expression in {
                "years": "substr(captured_at,1,4)",
                "months": "substr(captured_at,1,7)",
                "types": "extension",
                "kinds": "COALESCE(kind_override,kind)",
            }.items():
                facets[key] = [
                    dict(row)
                    for row in db.execute(
                        f"SELECT {expression} AS value,count(*) AS count FROM photos p WHERE {visible} GROUP BY {expression} ORDER BY value DESC"
                    )
                ]
            libraries = [
                dict(row)
                for row in db.execute("""SELECT l.*,count(p.id) AS count,
                (SELECT count(*) FROM photos f WHERE f.library_id=l.id AND f.missing=0) AS file_count FROM libraries l
                LEFT JOIN photos p ON p.library_id=l.id AND """ + visible + " WHERE l.removed=0 GROUP BY l.id ORDER BY l.id")
            ]
            people = [
                dict(row)
                for row in db.execute("""SELECT pe.*,count(DISTINCT f.photo_id) AS count,min(f.id) AS face_id
                FROM persons pe JOIN faces f ON f.person_id=pe.id JOIN photos p ON p.id=f.photo_id
                WHERE p.missing=0 GROUP BY pe.id ORDER BY count DESC,pe.id""")
            ]
        return {**counts, "duplicates": duplicate, **facets, "libraries": libraries, "people": people}

    def update_photo(self, photo_id: int, favorite: bool | None, tags: str | None):
        self.photo(photo_id)
        with self.connect() as db:
            if favorite is not None:
                db.execute("UPDATE photos SET favorite=? WHERE id=?", (int(favorite), photo_id))
            if tags is not None:
                db.execute("UPDATE photos SET tags=? WHERE id=?", (tags.strip()[:1000], photo_id))

    def original_files(self, photo_id):
        return self.original_files_many([photo_id])

    def selected_units(self, ids):
        ids = list(dict.fromkeys(ids))
        if not 1 <= len(ids) <= 5000:
            raise ValueError("Select between 1 and 5,000 items.")
        rows = self.photos({"ids": ids}, limit=None)["photos"]
        if len(rows) != len(ids):
            raise ValueError("A selected item is unavailable. Refresh the gallery and select again.")
        return list(self.organization_units(rows))

    def original_files_many(self, ids):
        return [photo for _, unit in self.selected_units(ids) for photo in unit]

    def invalidate_plans(self):
        with self.plan_lock:
            self.plans.clear()

    def remove_photo(self, photo_id):
        return self.remove_photos([photo_id])

    def remove_photos(self, ids):
        units = self.selected_units(ids)
        rows = [photo for _, unit in units for photo in unit]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in rows)
            if db.execute(f"SELECT count(*) FROM photos WHERE missing=0 AND id IN ({placeholders})",
                          [row["id"] for row in rows]).fetchone()[0] != len(rows):
                raise ValueError("A selected item changed. Refresh the gallery and select again.")
            for _, unit in units:
                group = uuid.uuid4().hex
                db.executemany("UPDATE photos SET removed_group=?,missing=1 WHERE id=?",
                               [(group, row["id"]) for row in unit])
        self.invalidate_plans()
        return {"removed": len(rows), "items": len(units)}

    def remove_library(self, library_id):
        with self.connect() as db:
            if not db.execute("UPDATE libraries SET removed=1 WHERE id=? AND removed=0", (library_id,)).rowcount:
                raise ValueError("Source not found.")
            db.execute("UPDATE photos SET missing=1 WHERE library_id=?", (library_id,))
        self.invalidate_plans()
        return {"removed": True}

    def removed_items(self, offset=0, limit=100):
        with self.connect() as db:
            sources = [dict(row) for row in db.execute("SELECT id,name,path FROM libraries WHERE removed=1 ORDER BY id DESC")]
            total = db.execute("SELECT count(DISTINCT p.removed_group) FROM photos p JOIN libraries l ON l.id=p.library_id "
                               "WHERE p.removed_group IS NOT NULL AND l.removed=0").fetchone()[0]
            rows = [dict(row) for row in db.execute(
                "SELECT min(p.id) id,count(*) files,l.name source,"
                "(SELECT q.filename FROM photos q WHERE q.removed_group=p.removed_group ORDER BY q.media_type,q.id LIMIT 1) filename "
                "FROM photos p JOIN libraries l ON l.id=p.library_id WHERE p.removed_group IS NOT NULL AND l.removed=0 "
                "GROUP BY p.removed_group ORDER BY min(p.id) DESC LIMIT ? OFFSET ?", (limit, offset))]
        return {"sources": sources, "photos": rows, "total": total, "offset": offset, "limit": limit}

    def forget_removed(self, item_id: int, *, source: bool = False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if source:
                if not db.execute("SELECT 1 FROM libraries WHERE id=? AND removed=1", (item_id,)).fetchone():
                    raise ValueError("This folder is no longer in Removed items.")
                selection, key = "library_id=?", item_id
            else:
                item = db.execute(
                    "SELECT p.removed_group FROM photos p JOIN libraries l ON l.id=p.library_id "
                    "WHERE p.id=? AND p.removed_group IS NOT NULL AND p.missing=1 AND l.removed=0",
                    (item_id,),
                ).fetchone()
                if item is None:
                    raise ValueError("This file is no longer in Removed items.")
                selection, key = "removed_group=?", item["removed_group"]

            selected_photos = f"SELECT id FROM photos WHERE {selection}"
            photo_ids = [row[0] for row in db.execute(selected_photos, (key,))]
            faces = db.execute(
                f"SELECT id,person_id FROM faces WHERE photo_id IN ({selected_photos})", (key,)
            ).fetchall()
            db.execute(f"DELETE FROM photos WHERE {selection}", (key,))
            db.executemany(
                "DELETE FROM persons WHERE id=? AND NOT EXISTS (SELECT 1 FROM faces WHERE person_id=persons.id)",
                [(person_id,) for person_id in {face["person_id"] for face in faces}],
            )
            if source:
                db.execute("DELETE FROM libraries WHERE id=?", (item_id,))

            limits = []
            if photo_ids:
                limits.append(("photos", max(photo_ids)))
            if source:
                limits.append(("libraries", item_id))
            db.executemany(
                "INSERT INTO forgotten_id_limits VALUES (?,?) ON CONFLICT(table_name) "
                "DO UPDATE SET highest_id=max(highest_id,excluded.highest_id)", limits,
            )

        self.invalidate_plans()
        skipped = 0
        face_ids = [face["id"] for face in faces]
        for directory, ids in (("thumbnails", photo_ids), ("faces", face_ids)):
            if not ids:
                continue
            try:
                with output_directory(self.data, Path(directory)) as folder:
                    if folder != self.data / directory:
                        raise ValueError("Preview cache moved outside application data.")
                    for preview_id in ids:
                        try:
                            (folder / f"{preview_id}.jpg").unlink(missing_ok=True)
                        except OSError:
                            skipped += 1
            except (OSError, ValueError):
                skipped += len(ids)
        return {"forgotten": len(photo_ids), "skipped_previews": skipped}

    def restore_library(self, library_id):
        with self.connect() as db:
            row = db.execute("SELECT path,demo FROM libraries WHERE id=? AND removed=1", (library_id,)).fetchone()
        if not row:
            raise ValueError("Removed source not found.")
        return self.add(row["path"], demo=bool(row["demo"]))

    def restore_photo(self, photo_id):
        with self.connect() as db:
            row = db.execute("SELECT p.removed_group,p.library_id,l.path FROM photos p JOIN libraries l ON l.id=p.library_id "
                             "WHERE p.id=? AND p.removed_group IS NOT NULL AND l.removed=0", (photo_id,)).fetchone()
            if not row:
                raise ValueError("Removed item not found. Restore its source folder first if needed.")
            if not Path(row["path"]).is_dir():
                raise ValueError("Reconnect the source drive before restoring this item.")
            db.execute("UPDATE photos SET removed_group=NULL WHERE removed_group=?", (row["removed_group"],))
        self.invalidate_plans()
        return row["library_id"]

    def assign_kind(self, ids: list[int], kind: str | None):
        if kind is not None and kind not in CATEGORIES:
            raise ValueError("Choose an available image category.")
        ids = list(dict.fromkeys(ids))
        if not 1 <= len(ids) <= 5000:
            raise ValueError("Select between 1 and 5,000 photos.")
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute(
                f"SELECT count(*) FROM photos WHERE missing=0 AND media_type='image' AND id IN ({placeholders})",
                ids,
            ).fetchone()[0]
            if count != len(ids):
                raise ValueError("A selected photo is unavailable. Refresh the gallery.")
            db.execute(f"UPDATE photos SET kind_override=?,kind_batch=NULL WHERE id IN ({placeholders})", [kind, *ids])
        return {"updated": count}

    def output_folder(self, destination: str, *, allow_source_children: bool = False):
        destination = Path(destination).expanduser().resolve(strict=True)
        if not destination.is_dir():
            raise ValueError("Choose an existing output folder.")
        self._validate_output_location(destination, allow_source_children=allow_source_children)
        return destination

    def recovery_output_folder(self, destination: str) -> Path:
        default = recovery_directory()
        if destination.strip() and Path(destination).expanduser() != default:
            return self.output_folder(destination, allow_source_children=True)

        home = default.parent.resolve(strict=True)
        self._validate_output_location(home / default.name, allow_source_children=True)
        with output_directory(home, Path(default.name)) as folder:
            return folder

    def _validate_output_location(self, destination: Path, *, allow_source_children: bool):
        if destination.is_relative_to(self.data) or self.data.is_relative_to(destination):
            raise ValueError("Choose an output folder outside application data.")
        with self.connect() as db:
            for row in db.execute("SELECT path FROM libraries"):
                source = Path(row[0])
                if destination.is_relative_to(source) or (not allow_source_children and source.is_relative_to(destination)):
                    raise ValueError("Choose an output folder separate from your source libraries.")

    def organization_units(self, rows):
        """A selected Live Photo always includes both original resources."""
        lookup = {row["id"]: row for row in rows}
        missing = sorted({row.get("live_motion_id") or row.get("live_still_id") for row in rows} - {None} - lookup.keys())
        for offset in range(0, len(missing), 500):
            lookup.update({row["id"]: row for row in self.photos({"ids": missing[offset:offset + 500]}, None)["photos"]})
        used = set()
        for row in rows:
            if row["id"] in used:
                continue
            anchor = lookup[row["live_still_id"]] if row.get("live_still_id") else row
            unit = [anchor]
            if anchor.get("live_motion_id"):
                unit.append(lookup[anchor["live_motion_id"]])
            used.update(photo["id"] for photo in unit)
            yield anchor, unit

    def plan(
        self,
        destination: str,
        layout: str,
        filters: dict,
        skip_duplicates: bool,
        *,
        create_run_folder: bool = True,
    ):
        destination = self.output_folder(destination)
        approved = ApprovedOutput.inspect(destination)
        with self.connect() as db:
            names = {row["id"]: row["name"] for row in db.execute("SELECT * FROM persons")}
        rows = self.photos(filters, limit=None)["photos"]
        if not rows:
            raise ValueError("There are no matching photos or videos to organize.")
        entries, hashes, skipped = [], set(), 0
        for anchor, unit in self.organization_units(rows):
            bundle = tuple(photo["sha256"] for photo in unit)
            if skip_duplicates and bundle in hashes:
                skipped += len(unit)
                continue
            hashes.add(bundle)
            date = anchor["captured_at"]
            if layout == "type":
                folder = Path(anchor["extension"])
            elif layout == "people":
                people = sorted({safe_segment(names.get(pid, "Unknown")) for pid in anchor["person_ids"]})
                folder = Path("People") / safe_segment(" + ".join(people) if people else "Unassigned")
            elif layout == "date_type":
                folder = Path(date[:4]) / date[5:7] / anchor["extension"]
            elif layout == "date":
                folder = Path(date[:4]) / date[5:7]
            elif layout == "kind":
                folder = Path(safe_segment(anchor["kind"]))
            elif layout == "date_kind":
                folder = Path(date[:4]) / date[5:7] / safe_segment(anchor["kind"])
            elif layout == "kind_date":
                category = CATEGORY_FOLDERS.get(anchor["kind"], safe_segment(anchor["kind"]))
                folder = Path(category) / date[:4] / MONTH_NAMES[int(date[5:7]) - 1]
            else:
                raise ValueError("Unknown folder layout.")
            for photo in unit:
                original_suffix = Path(photo["filename"]).suffix
                suffix = (original_suffix if original_suffix.lstrip(".").upper() == photo["extension"]
                          else "." + photo["extension"].lower())
                entries.append({
                    "id": photo["id"],
                    "source": photo["path"],
                    "source_root": photo["source_root"],
                    "relative": str(folder / (safe_segment(Path(anchor["filename"]).stem) + suffix)),
                    "sha256": photo["sha256"],
                    "size": photo["size"],
                    "media_type": photo["media_type"],
                })
        token = uuid.uuid4().hex
        plan = {
            "token": token,
            "destination": str(destination),
            "approved_output": approved,
            "entries": entries,
            "skipped": skipped,
            "bytes": sum(e["size"] for e in entries),
            "layout": layout,
            "create_run_folder": create_run_folder,
        }
        with self.plan_lock:
            self.plans[token] = plan
            while len(self.plans) > 8:
                self.plans.pop(next(iter(self.plans)))
        return {
            "token": token,
            "count": len(entries),
            "skipped": skipped,
            "bytes": plan["bytes"],
            "destination": str(destination),
            "create_run_folder": create_run_folder,
            "photos": sum(e["media_type"] == "image" for e in entries),
            "videos": sum(e["media_type"] == "video" for e in entries),
            "folders": list(CATEGORY_FOLDERS.values()) if layout == "kind_date" else [],
            "sample": [e["relative"] for e in entries[:8]],
            "fallback_dates": sum(p["date_source"] == "File modified" for p in rows),
        }

    def consume_plan(self, token: str):
        with self.plan_lock:
            plan = self.plans.pop(token, None)
        if not plan:
            raise ValueError("This preview has expired. Preview the organization again.")
        return plan

    def export(self, plan: dict, progress, cancelled):
        # A unique run folder plus exclusive file creation prevents overwriting any existing file.
        approved = plan["approved_output"]
        identifier = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        nested = plan.get("create_run_folder", True)
        copied = copied_photos = copied_videos = 0
        errors = []
        manifest_name = "manifest.csv" if nested else "Framekeep-manifest-" + identifier + ".csv"
        with output_directory(approved) as root:
            self._validate_output_location(root, allow_source_children=False)
            if shutil.disk_usage(root).free < plan["bytes"] + 5 * 1024 * 1024:
                raise ValueError("There is not enough free space for these copies.")
            run_folder = new_output_folder(approved, "Framekeep-" + identifier) if nested else approved
            with output_directory(run_folder) as run:
                if plan["layout"] == "kind_date":
                    for folder in CATEGORY_FOLDERS.values():
                        with output_directory(run_folder, Path(folder)):
                            pass
                with new_output_file(run_folder, Path(manifest_name), csv_text=True) as (_, manifest):
                    writer = csv.writer(manifest)
                    writer.writerow(["source", "copy", "sha256", "status"])
                    for i, entry in enumerate(plan["entries"]):
                        if cancelled():
                            break
                        progress(i, len(plan["entries"]), Path(entry["source"]).name)
                        source, relative = Path(entry["source"]), Path(entry["relative"])
                        try:
                            target = copy_verified_original(
                                source, Path(entry["source_root"]), entry["sha256"], run_folder, relative,
                            )
                            copied += 1
                            copied_videos += entry.get("media_type") == "video"
                            copied_photos += entry.get("media_type", "image") == "image"
                            writer.writerow(
                                [str(source), str(target.relative_to(run)), entry["sha256"], "verified"]
                            )
                        except (OSError, ValueError) as error:
                            errors.append({"file": source.name, "reason": str(error)})
                            writer.writerow([str(source), "", entry["sha256"], str(error)])
                        manifest.flush()
        progress(copied, len(plan["entries"]), "Copies verified")
        return {
            "copied": copied,
            "copied_photos": copied_photos,
            "copied_videos": copied_videos,
            "destination": str(run),
            "manifest": str(run / manifest_name),
            "errors": errors[:30],
            "error_count": len(errors),
            "cancelled": cancelled(),
            "skipped": plan["skipped"],
        }
