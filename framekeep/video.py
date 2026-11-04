"""One cancellable playback preparation and a bounded, private MP4 cache."""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .image_processing import _worker_command, process_video_thumbnail
from .source_files import open_source, source_path
from .video_policy import MAX_DURATION, MAX_INPUT, MAX_OUTPUT, WALL_SECONDS

CACHE_BUDGET = 512 * 1024**2
CACHE_NAME = re.compile(r"[0-9a-f]{64}-v1\.mp4\Z")
THUMBNAIL_BUDGET = 64 * 1024**2
THUMBNAIL_NAME = re.compile(r"[0-9a-f]{64}-v1\.jpg\Z")
MAX_THUMBNAIL = 512 * 1024


def prepare_video(photo, ffmpeg, target, cancelled):
    request = {key: photo[key] for key in ("path", "sha256", "size", "mtime_ns")}
    request.update(root=photo["source_root"], ffmpeg=ffmpeg, target=str(target))
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    with subprocess.Popen(_worker_command("video_worker.py"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=flags, start_new_session=sys.platform != "win32",
            env={**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}) as process:
        try:
            process.stdin.write(json.dumps(request).encode())
            process.stdin.close()
            deadline = time.monotonic() + WALL_SECONDS
            while process.poll() is None:
                if cancelled():
                    raise ValueError("Playback preparation cancelled.")
                if time.monotonic() >= deadline:
                    raise ValueError("Playback preparation exceeded 3 minutes. Open the original instead.")
                if target.exists() and target.stat().st_size >= MAX_OUTPUT:
                    raise ValueError("Playback copy exceeded 256 MiB. Open the original instead.")
                time.sleep(0.1)
            raw = process.stdout.read(4097)
            if process.returncode or len(raw) > 4096:
                raise ValueError("Video decoder stopped or exceeded its resource limit.")
            try:
                result = json.loads(raw)
            except ValueError:
                raise ValueError("Video decoder returned an invalid result.") from None
            if not result.get("ok"):
                raise ValueError(result.get("error", "This video could not be prepared."))
        finally:
            if process.poll() is None:
                if sys.platform == "win32":
                    # Closing the worker also closes its kill-on-close job and FFmpeg child.
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


class Video:
    def __init__(self, library):
        self.library = library
        self.folder = library.data / "video_previews"
        self.folder.mkdir(exist_ok=True)
        source_path(self.folder, library.data)
        self.thumbnails = library.data / "video_thumbnails"
        self.thumbnails.mkdir(exist_ok=True)
        source_path(self.thumbnails, library.data)
        self.thumbnail_lock = threading.Lock()
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="framekeep-video")
        self.current = None
        self.stop = threading.Event()
        self.closed = False

    @staticmethod
    def ready():
        return shutil.which("ffmpeg") is not None

    def _source(self, photo_id, *, playback=True):
        photo = self.library.photo(photo_id)
        if photo["media_type"] != "video":
            raise ValueError("This item is a still image, not a video.")
        with open_source(Path(photo["path"]), Path(photo["source_root"])) as stream:
            info = os.fstat(stream.fileno())
            if (info.st_size, info.st_mtime_ns) != (photo["size"], photo["mtime_ns"]):
                raise ValueError("Video changed since indexing. Rescan this source.")
        if not 0 < photo["size"] <= MAX_INPUT:
            raise ValueError("Video previews support source files up to 2 GiB. You can still open the original.")
        if playback and not 0 < photo["duration"] <= MAX_DURATION:
            raise ValueError("In-app playback supports videos up to 30 minutes and 2 GiB. Open the original for this file.")
        if not re.fullmatch(r"[0-9a-f]{64}", photo["sha256"]):
            raise ValueError("Rescan this source before playing it.")
        return photo

    def thumbnail(self, photo_id):
        photo = self._source(photo_id, playback=False)
        with self.thumbnail_lock:
            target = self.thumbnails / (photo["sha256"] + "-v1.jpg")
            if target.exists():
                with open_source(target, self.thumbnails) as stream:
                    data = stream.read(MAX_THUMBNAIL + 1)
                if data.startswith(b"\xff\xd8\xff") and 0 < len(data) < MAX_THUMBNAIL:
                    return data
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise ValueError("Install FFmpeg and reopen Framekeep to generate video thumbnails.")
            data = process_video_thumbnail(photo, ffmpeg)
            if not data.startswith(b"\xff\xd8\xff") or not 0 < len(data) < MAX_THUMBNAIL:
                raise ValueError("Video thumbnail exceeded the output limit or was not a JPEG.")
            self._source(photo_id, playback=False)
            source_path(self.thumbnails, self.library.data)
            temporary = self.thumbnails / (uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(data)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            self._trim_cache(self.thumbnails, target, THUMBNAIL_BUDGET, THUMBNAIL_NAME)
            return data

    def _target(self, photo):
        return self.folder / (photo["sha256"] + "-v1.mp4")

    def _cached(self, photo):
        target = self._target(photo)
        if not target.exists():
            return None
        with open_source(target, self.folder) as stream:
            if not 0 < os.fstat(stream.fileno()).st_size < MAX_OUTPUT:
                return None
        return target

    def prepare(self, photo_id):
        photo = self._source(photo_id)
        with self.lock:
            if self.closed:
                raise ValueError("Framekeep is closing.")
            if self._cached(photo):
                return {"state": "ready", "id": None}
            if self.current and self.current["state"] == "preparing":
                if self.current["photo_id"] == photo_id and not self.stop.is_set():
                    return dict(self.current)
                raise ValueError("Another video is being prepared. Try Play video again in a moment.")
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise ValueError("Install FFmpeg and reopen Framekeep for in-app playback. You can still open the original.")
            self.stop.clear()
            job_id = uuid.uuid4().hex
            self.current = {"id": job_id, "photo_id": photo_id, "state": "preparing", "message": "Preparing full video locally…"}
            self.executor.submit(self._prepare, photo, ffmpeg, job_id)
            return dict(self.current)

    def _prepare(self, photo, ffmpeg, job_id):
        try:
            with tempfile.TemporaryDirectory(prefix="prepare-", dir=self.folder) as work:
                target = Path(work) / "playback.mp4"
                prepare_video(photo, ffmpeg, target, self.stop.is_set)
                if self.stop.is_set():
                    raise ValueError("Playback preparation cancelled.")
                self._source(photo["id"])
                if not 0 < target.stat().st_size < MAX_OUTPUT:
                    raise ValueError("Playback copy exceeded 256 MiB. Open the original instead.")
                with self.lock:
                    if self.stop.is_set():
                        raise ValueError("Playback preparation cancelled.")
                    target.replace(self._target(photo))
                    self._trim(self._target(photo))
            state, message = "ready", "Full video ready."
        except Exception as error:  # noqa: BLE001 -- background task boundary
            state, message = "cancelled" if self.stop.is_set() else "failed", str(error)[:400]
        with self.lock:
            if self.current and self.current["id"] == job_id:
                self.current.update(state=state, message=message)

    def _trim(self, keep):
        self._trim_cache(self.folder, keep, CACHE_BUDGET, CACHE_NAME)

    @staticmethod
    def _trim_cache(folder, keep, budget, pattern):
        files = []
        for path in folder.iterdir():
            if pattern.fullmatch(path.name):
                source_path(path, folder)
                files.append((path.stat().st_mtime_ns, path, path.stat().st_size))
        total = sum(item[2] for item in files)
        for _, path, size in sorted(files):
            if total <= budget:
                break
            if path != keep:
                try:
                    path.unlink()
                    total -= size
                except OSError:
                    pass  # A playback response may still hold this cache entry open.

    def status(self, job_id):
        with self.lock:
            if not self.current or self.current["id"] != job_id:
                raise ValueError("Playback request expired. Press Play video again.")
            return dict(self.current)

    def cancel(self, job_id):
        with self.lock:
            if self.current and self.current["id"] == job_id and self.current["state"] == "preparing":
                self.stop.set()
        return {"ok": True}

    def file(self, photo_id):
        photo = self._source(photo_id)
        with self.lock:
            target = self._cached(photo)
            if not target:
                raise ValueError("Press Play video to prepare this video first.")
            return target

    def close(self):
        with self.lock:
            self.closed = True
            self.stop.set()
        self.executor.shutdown(wait=True, cancel_futures=True)
