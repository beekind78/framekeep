"""Loopback-only HTTP application. The browser is a UI, never an upload destination."""

import asyncio
import base64
import os
import subprocess
import sys
import threading
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import ClientDisconnect

from . import clipboard
from .access import SESSION_TTL, TICKET_TTL, VERSION, LocalAccess
from .categories import Categories
from .category_model import Category
from .category_review import CategoryReview, Suggestion
from .demo import create_demo
from .editing import (
    recover_jpegs,
    recovery_preview,
    repairable_photo,
    review_photo_repair,
    review_recovery_matches,
    save_edit,
    save_photo_repair,
)
from .image_processing import process_image
from .jobs import Jobs
from .library import Library
from .live_photos import LivePhotos
from .motion import Motion
from .output_files import ApprovedOutput
from .paths import data_directory, protect_default_data, recovery_directory
from .people import People
from .review import Review
from .source_files import open_source, source_path
from .video import Video
from .video_policy import PlaybackQuality

STATIC = Path(__file__).parent / "static"
BOOTSTRAP_MAX_BYTES = 1024


class ScanRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class PhotoUpdate(BaseModel):
    favorite: bool | None = None
    tags: str | None = Field(default=None, max_length=1000)
    kind: Category | None = None


class PhotoFilters(BaseModel):
    q: str = Field(default="", max_length=500)
    year: str = Field(default="", max_length=4)
    month: str = Field(default="", max_length=7)
    type: str = Field(default="", max_length=10)
    kind: str = Field(default="", max_length=30)
    media_type: Literal["", "image", "video"] = ""
    library: int | Literal[""] = ""
    person: int | Literal[""] = ""
    sort: Literal["newest", "oldest", "name", "size"] = "newest"
    favorite: bool = False
    duplicates: bool = False
    collapse_live: bool = False
    live_only: bool = False
    pending_categories: Literal["", "new", "older"] = ""
    ids: list[int] | None = Field(default=None, min_length=1, max_length=5000)


class PlanRequest(BaseModel):
    destination: str = Field(min_length=1, max_length=4096)
    layout: Literal["date", "date_type", "type", "people", "kind", "date_kind", "kind_date"] = "date"
    filters: PhotoFilters = Field(default_factory=PhotoFilters)
    skip_duplicates: bool = True
    create_run_folder: bool = True


class ExportRequest(BaseModel):
    token: str


class RecoveryScan(BaseModel):
    scan_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class JobAction(BaseModel):
    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class RecoveryRequest(RecoveryScan):
    destination: str = Field(default="", max_length=4096)
    enhance: bool = False
    prefer_larger: bool = False
    trim_edge: bool = False
    approved_matches: list[int] = Field(default_factory=list, max_length=20000)


class RepairRequest(BaseModel):
    choice: Literal["crop", "match"]
    destination: str = Field(default="", max_length=4096)


class CategoryAssignment(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)
    kind: Category | None


class CategoryAnalysis(BaseModel):
    filters: PhotoFilters = Field(default_factory=PhotoFilters)


class PeopleAnalysis(BaseModel):
    library: Annotated[int, Field(gt=0)] | Literal[""] = ""
    date: str = Field(default="", pattern=r"^(?:\d{4}(?:-(?:0[1-9]|1[0-2]))?)?$")
    photos_only: bool = False
    limit: Literal[0, 250, 1000, 5000] = 0


class BatchPreview(CategoryAnalysis):
    kind: Suggestion | None = None
    offset: int = Field(default=0, ge=0)


class BatchGroup(BaseModel):
    id: int = Field(gt=0)
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class BatchApproval(CategoryAnalysis):
    kind: Suggestion
    groups: list[BatchGroup] = Field(min_length=1, max_length=24)


class BatchUndo(BaseModel):
    batch_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class AcceptAllSuggestions(CategoryAnalysis):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class NameRequest(BaseModel):
    name: str


class MergeRequest(BaseModel):
    target: int


class SplitRequest(BaseModel):
    photo_id: int


class RevealRequest(BaseModel):
    photo_id: int


class BootstrapRequest(BaseModel):
    ticket: str = Field(min_length=32, max_length=128)


class ClipboardRequest(BaseModel):
    mode: Literal["files", "path", "name"] = "files"


class SelectedItems(BaseModel):
    ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=5000)


class SelectedClipboard(SelectedItems):
    mode: Literal["files", "path", "name"] = "files"


Fraction = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class ImageEditRequest(BaseModel):
    destination: str = Field(min_length=1, max_length=4096)
    turns: int = Field(default=0, ge=0, le=3)
    crop: tuple[Fraction, Fraction, Fraction, Fraction] = (0, 0, 1, 1)


def default_data():
    return data_directory()


def create_app(data_dir: Path | None = None, *, access: LocalAccess | None = None):
    if data_dir is None:
        data_dir = default_data()
        protect_default_data(data_dir)
    library = Library(data_dir)
    people = People(library)
    access = access or LocalAccess()
    picker_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        jobs.interrupt()
        video.close()
        jobs.close()

    categories = Categories(library)
    category_review = CategoryReview(library)
    review = Review(library, categories)
    live_photos, motion = LivePhotos(library), Motion(library)
    video = Video(library)
    jobs = Jobs(library.connect, {
        "scan": library.scan, "review": review.analyze, "categories": categories.analyze,
        "people": lambda scope, progress, cancelled, checkpoint: people.analyze(
            progress, cancelled, scope=scope, checkpoint=checkpoint,
        ),
    })
    app = FastAPI(title="Framekeep", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.library, app.state.people, app.state.jobs = library, people, jobs
    app.state.access = access
    app.state.categories = categories
    app.state.review = review
    app.state.live_photos, app.state.motion = live_photos, motion
    app.state.video = video
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        path = request.scope["path"]
        if path == "/api" or path.startswith("/api/"):
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host')}"
            if (origin and origin != expected) or request.headers.get("sec-fetch-site") in (
                "cross-site",
                "same-site",
            ):
                return JSONResponse(
                    {"detail": "This request is not from the Framekeep origin."}, status_code=403
                )
            public = (path == "/api/health" and request.method == "GET") or (
                path == "/api/bootstrap" and request.method == "POST"
            )
            controlled = path in ("/api/launch", "/api/shutdown")
            authorized = (
                access.controls(request.headers.get("x-framekeep-control", ""))
                if controlled
                else (access.authenticates(request.headers.get("x-framekeep-token", "")))
            )
            if not public and not authorized:
                return JSONResponse(
                    {"detail": "Open Start Framekeep.cmd to unlock this session."}, status_code=401
                )
        response = await call_next(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Cross-Origin-Resource-Policy": "same-origin",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; img-src 'self' blob:; media-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            }
        )
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(OSError)
    async def disk_error(request, error):
        return JSONResponse(
            {"detail": f"Folder or file unavailable: {error.strerror or str(error)}"}, status_code=400
        )

    def idle():
        with jobs.while_idle():
            pass

    @app.get("/api/health")
    def health(challenge: str = Query(min_length=64, max_length=64)):
        return {
            "app": "framekeep",
            "version": VERSION,
            "instance": access.instance,
            "proof": access.proof(challenge),
        }

    @app.post("/api/launch")
    def launch():
        return {"ticket": access.ticket(), "expires_in": TICKET_TTL}

    @app.post("/api/bootstrap")
    async def bootstrap(request: Request):
        length = request.headers.get("content-length")
        if length is not None and (not length.isascii() or not length.isdecimal()):
            raise HTTPException(400, "Invalid launch request length.")
        if length is not None and (len(length) > 10 or int(length) > BOOTSTRAP_MAX_BYTES):
            raise HTTPException(413, "Launch request is too large.")
        payload = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(payload) + len(chunk) > BOOTSTRAP_MAX_BYTES:
                        raise HTTPException(413, "Launch request is too large.")
                    payload.extend(chunk)
        except TimeoutError:
            raise HTTPException(408, "Launch request timed out.") from None
        except ClientDisconnect:
            raise HTTPException(400, "Launch request was interrupted.") from None
        try:
            body = BootstrapRequest.model_validate_json(payload)
        except ValidationError:
            raise HTTPException(422, "Invalid launch request.") from None
        try:
            token = access.exchange(body.ticket)
        except ValueError as error:
            raise HTTPException(401, str(error)) from None
        return {"token": token, "expires_in": SESSION_TTL}

    @app.get("/api/session")
    def session():
        return {
            "app": "framekeep",
            "version": VERSION,
            "people_ready": people.ready(),
            "categories_ready": categories.ready(),
            "motion_ready": motion.ready(),
            "video_ready": video.ready(),
            "data_dir": str(library.data),
            "recovery_output": str(recovery_directory()),
        }

    @app.get("/api/summary")
    def summary():
        return library.summary()

    @app.get("/api/photos")
    def photos(
        request: Request, limit: int = Query(default=120, ge=1, le=240), offset: int = Query(default=0, ge=0)
    ):
        filters = dict(request.query_params)
        for flag in ("favorite", "duplicates", "collapse_live", "live_only"):
            filters[flag] = filters.get(flag) == "1"
        return library.photos(filters, limit, offset)

    @app.get("/api/albums")
    def albums(request: Request):
        filters = dict(request.query_params)
        for flag in ("favorite", "duplicates", "collapse_live", "live_only"):
            filters[flag] = filters.get(flag) == "1"
        return library.albums(filters)

    @app.patch("/api/photos/{photo_id}")
    def update_photo(photo_id: int, body: PhotoUpdate):
        if "kind" in body.model_fields_set:
            idle()
            library.assign_kind([photo_id], body.kind)
        library.update_photo(photo_id, body.favorite, body.tags)
        return {"ok": True}

    @app.get("/api/photos/{photo_id}/matches")
    def photo_matches(photo_id: int):
        return review.matches(photo_id)

    @app.delete("/api/photos/{photo_id}")
    def remove_photo(photo_id: int):
        idle()
        return library.remove_photo(photo_id)

    @app.post("/api/photos/remove")
    def remove_selected(body: SelectedItems):
        idle()
        return library.remove_photos(body.ids)

    @app.delete("/api/libraries/{library_id}")
    def remove_library(library_id: int):
        idle()
        return library.remove_library(library_id)

    @app.get("/api/removed")
    def removed(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100)):
        return library.removed_items(offset, limit)

    @app.delete("/api/removed/photos/{photo_id}")
    def forget_photo(photo_id: int):
        with jobs.while_idle():
            return library.forget_removed(photo_id)

    @app.delete("/api/removed/libraries/{library_id}")
    def forget_library(library_id: int):
        with jobs.while_idle():
            return library.forget_removed(library_id, source=True)

    def restore_scan(library_id):
        return jobs.submit_resumable("scan", "Rescanning library", library_id)

    @app.post("/api/photos/{photo_id}/restore")
    def restore_photo(photo_id: int):
        with jobs.while_idle():
            return restore_scan(library.restore_photo(photo_id))

    @app.post("/api/libraries/{library_id}/restore")
    def restore_library(library_id: int):
        with jobs.while_idle():
            return restore_scan(library.restore_library(library_id))

    def copy_originals(ids, mode):
        ids = list(dict.fromkeys(ids))
        rows = library.original_files_many(ids)
        if mode == "name":
            names = {row["id"]: row["filename"] for row in rows}
            clipboard.copy_text("\r\n".join(names[photo_id] for photo_id in ids))
            return {"copied": len(ids)}
        with ExitStack() as stack:
            paths = []
            for row in rows:
                stream = stack.enter_context(open_source(Path(row["path"]), Path(row["source_root"])))
                info = os.fstat(stream.fileno())
                if (info.st_size, info.st_mtime_ns) != (row["size"], row["mtime_ns"]):
                    raise ValueError("Original changed since indexing. Rescan this source.")
                paths.append(Path(row["path"]))
            if mode == "files":
                clipboard.copy_files(paths)
            else:
                clipboard.copy_text("\r\n".join(library.photo(photo_id)["path"] for photo_id in ids))
        return {"copied": len(paths) if mode == "files" else len(ids)}

    @app.post("/api/photos/{photo_id}/clipboard")
    def copy_original(photo_id: int, body: ClipboardRequest):
        return copy_originals([photo_id], body.mode)

    @app.post("/api/photos/clipboard")
    def copy_selected(body: SelectedClipboard):
        return copy_originals(body.ids, body.mode)

    @app.post("/api/libraries/{library_id}/clipboard")
    def copy_source_name(library_id: int):
        with library.connect() as db:
            row = db.execute("SELECT name FROM libraries WHERE id=? AND removed=0", (library_id,)).fetchone()
        if row is None:
            raise ValueError("This source folder is unavailable.")
        clipboard.copy_text(row["name"])
        return {"copied": 1}

    @app.post("/api/photos/{photo_id}/edit")
    def edit_image(photo_id: int, body: ImageEditRequest):
        idle()
        if library.photo(photo_id)["media_type"] != "image":
            raise ValueError("Image editing is available for still photos only.")
        output = ApprovedOutput.inspect(library.output_folder(body.destination))
        return jobs.submit("Saving edited copy", lambda progress, cancelled:
                           save_edit(library, photo_id, output, body.turns, body.crop, progress, cancelled))

    @app.post("/api/libraries/{library_id}/reveal")
    def reveal_library(library_id: int):
        with library.connect() as db:
            row = db.execute("SELECT path FROM libraries WHERE id=? AND removed=0", (library_id,)).fetchone()
        if not row or not Path(row["path"]).is_dir():
            raise ValueError("The source folder is unavailable.")
        path = source_path(Path(row["path"]), Path(row["path"]))
        if sys.platform == "win32":
            subprocess.Popen(["explorer.exe", str(path)])
        return {"ok": True}

    @app.get("/api/duplicates/similar")
    def similar_photos(
        request: Request, limit: int = Query(default=20, ge=1, le=40), offset: int = Query(default=0, ge=0)
    ):
        filters = dict(request.query_params)
        for flag in ("favorite", "duplicates", "collapse_live", "live_only"):
            filters[flag] = filters.get(flag) == "1"
        return review.similar(filters, limit, offset)

    @app.post("/api/review/analyze")
    def analyze_review(body: CategoryAnalysis):
        return jobs.submit_resumable("review", "Reviewing categories and similar photos", body.filters.model_dump())

    @app.post("/api/review/batch-preview")
    def preview_category_batch(body: BatchPreview):
        return category_review.preview(body.filters.model_dump(), body.kind, body.offset)

    @app.post("/api/review/batch-approve")
    def approve_category_batch(body: BatchApproval):
        with jobs.while_idle():
            return category_review.approve(body.filters.model_dump(), body.kind,
                                           {group.id: group.revision for group in body.groups})

    @app.get("/api/review/batch-history")
    def category_batch_history(offset: int = Query(default=0, ge=0, le=1_000_000)):
        return category_review.history(offset)

    @app.post("/api/review/accept-all/preview")
    def preview_all_suggestions(body: CategoryAnalysis):
        return category_review.preview_all(body.filters.model_dump())

    @app.post("/api/review/accept-all")
    def accept_all_suggestions(body: AcceptAllSuggestions):
        with jobs.while_idle():
            return category_review.approve_all(body.filters.model_dump(), body.revision)

    @app.post("/api/review/batch-undo")
    def undo_category_batch(body: BatchUndo):
        with jobs.while_idle():
            return category_review.undo(body.batch_id)

    @app.get("/api/photos/{photo_id}/thumbnail")
    def thumbnail(photo_id: int):
        if library.photo(photo_id)["media_type"] == "video":
            return Response(video.thumbnail(photo_id), media_type="image/jpeg")
        path = library.thumbs / f"{photo_id}.jpg"
        if not path.is_file():
            raise HTTPException(404, "Thumbnail unavailable; rescan this library.")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/photos/{photo_id}/preview")
    def preview(photo_id: int):
        photo = library.photo(photo_id)
        if photo["media_type"] == "video":
            raise ValueError("Video previews are not generated. Use Show original in folder.")
        processed = process_image(Path(photo["path"]), Path(photo["source_root"]), 2200)
        return Response(processed["jpeg"], media_type="image/jpeg")

    @app.post("/api/scan")
    def scan(body: ScanRequest):
        with jobs.while_idle():
            library_id = library.add(body.path)
            return jobs.submit_resumable("scan", "Indexing photos", library_id)

    @app.post("/api/live-photos/analyze")
    def link_live_photos(body: CategoryAnalysis):
        idle()
        return jobs.submit("Linking Live Photos", lambda progress, cancelled:
                           live_photos.analyze(body.filters.model_dump(), progress, cancelled))

    @app.post("/api/photos/{photo_id}/motion")
    def live_motion(photo_id: int):
        return Response(motion.preview(photo_id), media_type="video/mp4")

    @app.post("/api/photos/{photo_id}/playback")
    def prepare_playback(photo_id: int, quality: PlaybackQuality = "720p"):
        return video.prepare(photo_id, quality)

    @app.get("/api/playback/{job_id}")
    def playback_status(job_id: str):
        return video.status(job_id)

    @app.delete("/api/playback/{job_id}")
    def cancel_playback(job_id: str):
        return video.cancel(job_id)

    @app.get("/api/photos/{photo_id}/playback")
    def playback_file(photo_id: int, quality: PlaybackQuality = "720p"):
        return FileResponse(video.file(photo_id, quality), media_type="video/mp4")

    @app.post("/api/libraries/{library_id}/rescan")
    def rescan(library_id: int):
        with jobs.while_idle():
            with library.connect() as db:
                if not db.execute("SELECT 1 FROM libraries WHERE id=? AND removed=0", (library_id,)).fetchone():
                    raise ValueError("Library not found.")
            return restore_scan(library_id)

    @app.post("/api/demo")
    def demo():
        idle()

        def work(progress, cancelled):
            progress(0, 0, "Creating original demo illustrations…")
            folder = create_demo(library.data)
            library_id = library.add(str(folder), demo=True)
            return library.scan(library_id, progress, cancelled)

        return jobs.submit("Opening sample library", work)

    @app.get("/api/job")
    def job():
        current = jobs.snapshot()
        if current and current.get("result"):
            recovery = current["result"].pop("jpeg_recovery", None)
            current["result"].pop("jpeg_repair", None)
            if recovery:
                current["result"]["recoverable_jpegs"] = len(recovery["files"])
        return {"job": current}

    def completed_recovery(job_id):
        current = jobs.snapshot()
        if not current or current["id"] != job_id or current["state"] != "complete":
            raise ValueError("Open the latest completed scan or match review.")
        result = current.get("result") or {}
        recovery = result.get("jpeg_recovery")
        if not recovery or not recovery["files"]:
            raise ValueError("This scan found no JPEGs eligible for recovery.")
        return result, recovery

    @app.post("/api/recovery/jpeg/review")
    def start_recovery_review(body: RecoveryScan):
        with jobs.while_idle():
            _, recovery = completed_recovery(body.scan_id)
            return jobs.submit("Reviewing larger copies", lambda progress, cancelled:
                               review_recovery_matches(library, recovery, progress, cancelled))

    @app.get("/api/recovery/jpeg/review/{job_id}/preview/{index}")
    def damaged_recovery_preview(job_id: str, index: int):
        result, recovery = completed_recovery(job_id)
        candidate = next((item for item in result.get("review_candidates", []) if item["index"] == index), None)
        if candidate is None:
            raise HTTPException(404, "This comparison is unavailable.")
        return Response(recovery_preview(library, recovery, candidate), media_type="image/jpeg")

    @app.post("/api/recovery/jpeg")
    def recover_jpeg_copies(body: RecoveryRequest):
        with jobs.while_idle():
            result, recovery = completed_recovery(body.scan_id)
            if body.prefer_larger and "review_candidates" not in result:
                raise ValueError("Review the larger matches before saving copies.")
            candidates = {item["index"]: item for item in result.get("review_candidates", [])}
            if body.approved_matches and (not body.prefer_larger or not set(body.approved_matches) <= candidates.keys()):
                raise ValueError("An approved match is no longer in this review.")
            approved = {index: candidates[index] for index in body.approved_matches}
            root = ApprovedOutput.inspect(library.recovery_output_folder(body.destination))
            return jobs.submit("Recovering JPEG copies", lambda progress, cancelled: recover_jpegs(
                library, recovery, root, progress, cancelled, enhance=body.enhance,
                approved=approved, trim_edge=body.trim_edge,
            ))

    @app.post("/api/job/cancel")
    def cancel(body: JobAction | None = None):
        jobs.cancel(body.job_id if body else None)
        return {"ok": True}

    @app.post("/api/job/pause")
    def pause(body: JobAction):
        jobs.pause(body.job_id)
        return {"ok": True}

    @app.post("/api/job/resume")
    def resume(body: JobAction):
        return jobs.resume(body.job_id)

    def completed_repair(job_id):
        current = jobs.snapshot()
        if current and current["id"] == job_id and current["state"] == "complete":
            repair = (current.get("result") or {}).get("jpeg_repair")
            if repair:
                return repair
        raise ValueError("Open a new repair review for this photo.")

    @app.post("/api/photos/{photo_id}/repair")
    def inspect_photo_repair(photo_id: int):
        with jobs.while_idle():
            repairable_photo(library, photo_id)
            return jobs.submit("Inspecting JPEG for repair", lambda progress, cancelled:
                               review_photo_repair(library, photo_id, progress, cancelled))

    @app.get("/api/recovery/repair/{job_id}/preview")
    def repaired_crop_preview(job_id: str):
        preview = completed_repair(job_id)["preview"]
        if not preview:
            raise HTTPException(404, "No crop is available for this photo.")
        return Response(base64.b64decode(preview, validate=True), media_type="image/jpeg")

    @app.post("/api/recovery/repair/{job_id}")
    def save_repair(job_id: str, body: RepairRequest):
        with jobs.while_idle():
            repair = completed_repair(job_id)
            if not repair[body.choice]:
                raise ValueError("This repair option is unavailable.")
            root = ApprovedOutput.inspect(library.recovery_output_folder(body.destination))
            return jobs.submit("Repairing JPEG copy", lambda progress, cancelled:
                               save_photo_repair(library, repair, body.choice, root, progress, cancelled))

    @app.post("/api/shutdown")
    def shutdown():
        callback = getattr(app.state, "shutdown", None)
        if callback is None:
            raise ValueError("Stop the server from its launcher or terminal.")
        jobs.interrupt()
        callback()
        return {"ok": True}

    @app.post("/api/pick-folder")
    def pick_folder():
        if not picker_lock.acquire(blocking=False):
            raise ValueError("A folder picker is already open. Check your taskbar.")
        try:
            # A separate process gives Tk its own main thread on Windows.
            script = "import tkinter as t; from tkinter import filedialog; r=t.Tk(); r.withdraw(); r.attributes('-topmost',True); p=filedialog.askdirectory(title='Framekeep — choose a folder',parent=r); print(p); r.destroy()"
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            completed = subprocess.run(
                [sys.executable, "-c", script],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=180,
                creationflags=flags,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            if completed.returncode:
                raise ValueError("Folder picker unavailable. Paste the folder path into the field instead.")
            return {"path": completed.stdout.strip()}
        except subprocess.TimeoutExpired:
            raise ValueError("Folder selection timed out. Try again or paste a folder path.") from None
        finally:
            picker_lock.release()

    @app.post("/api/organize/preview")
    def plan(body: PlanRequest):
        idle()
        return library.plan(
            body.destination,
            body.layout,
            body.filters.model_dump(),
            body.skip_duplicates,
            create_run_folder=body.create_run_folder,
        )

    @app.post("/api/organize/copy")
    def copy(body: ExportRequest):
        idle()
        plan = library.consume_plan(body.token)
        return jobs.submit(
            "Organizing verified copies",
            lambda progress, cancelled: library.export(plan, progress, cancelled),
        )

    @app.post("/api/people/install")
    def install():
        return jobs.submit("Downloading people models", people.install)

    @app.post("/api/categories/install")
    def install_categories():
        return jobs.submit("Downloading category model", categories.install)

    @app.post("/api/categories/analyze")
    def analyze_categories(body: CategoryAnalysis):
        idle()
        if not categories.ready():
            raise ValueError("Download the category model first.")
        return jobs.submit_resumable("categories", "Detecting image categories", body.filters.model_dump())

    @app.post("/api/categories/assign")
    def assign_categories(body: CategoryAssignment):
        idle()
        return library.assign_kind(body.ids, body.kind)

    @app.post("/api/people/queue")
    def people_queue(body: PeopleAnalysis):
        return people.queue(body.model_dump())

    @app.post("/api/people/analyze")
    def analyze_people(body: PeopleAnalysis | None = None):
        if not people.ready():
            raise ValueError("Download the people models first.")
        return jobs.submit_resumable("people", "Finding familiar faces", (body or PeopleAnalysis()).model_dump())

    @app.get("/api/people/{person_id}/faces")
    def person_faces(person_id: int):
        with library.connect() as db:
            return {
                "faces": [
                    dict(r)
                    for r in db.execute(
                        "SELECT f.id,f.photo_id,p.filename FROM faces f JOIN photos p ON p.id=f.photo_id WHERE f.person_id=? AND p.missing=0 ORDER BY f.id",
                        (person_id,),
                    )
                ]
            }

    @app.get("/api/faces/{face_id}")
    def face_image(face_id: int):
        path = people.faces / f"{face_id}.jpg"
        with library.connect() as db:
            exists = db.execute("SELECT 1 FROM faces f JOIN photos p ON p.id=f.photo_id WHERE f.id=? AND p.missing=0", (face_id,)).fetchone()
        if not exists or not path.is_file():
            raise HTTPException(404, "Face preview unavailable.")
        return FileResponse(path, media_type="image/jpeg")

    @app.patch("/api/people/{person_id}")
    def rename(person_id: int, body: NameRequest):
        idle()
        people.rename(person_id, body.name)
        return {"ok": True}

    @app.post("/api/people/{person_id}/merge")
    def merge(person_id: int, body: MergeRequest):
        idle()
        people.merge(person_id, body.target)
        return {"ok": True}

    @app.post("/api/people/{person_id}/split")
    def split(person_id: int, body: SplitRequest):
        idle()
        return people.split(person_id, body.photo_id)

    @app.post("/api/reveal")
    def reveal(body: RevealRequest):
        photo = library.photo(body.photo_id)
        path = source_path(Path(photo["path"]), Path(photo["source_root"]))
        if not path.is_file():
            raise ValueError("The original is unavailable. Reconnect the source drive.")
        if sys.platform == "win32":
            subprocess.Popen(["explorer.exe", "/select,", str(path)])
        return {"path": str(path)}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def home():
        return FileResponse(STATIC / "index.html")

    return app
