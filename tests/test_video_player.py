import hashlib
import io
import json
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import framekeep.video as video_module
from framekeep import paths, video_worker
from framekeep.app import create_app
from framekeep.library import Library, file_hash
from framekeep.video import Video, prepare_video
from framekeep.video_metadata import read_video_metadata
from framekeep.video_policy import MAX_PSSH_MARKERS, stage_video
from tests.media_fixtures import video_bytes


@pytest.mark.skipif(sys.platform != "win32", reason="Framekeep's private FFmpeg path is Windows-specific")
def test_private_ffmpeg_takes_precedence_over_path(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "user_directory", lambda: tmp_path)
    monkeypatch.setattr(paths.shutil, "which", lambda _: "system-ffmpeg.exe")
    assert paths.ffmpeg_executable() == "system-ffmpeg.exe"

    private = tmp_path / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
    private.parent.mkdir(parents=True)
    private.touch()
    assert paths.ffmpeg_executable() == str(private)


@pytest.mark.parametrize(
    "payload, message",
    [
        (b"pssh" * MAX_PSSH_MARKERS + b"pssh", "too many protection headers"),
        (b"dcomzlib", "Compressed QuickTime headers"),
    ],
)
def test_ffmpeg_input_guard_checks_across_staging_chunks(payload, message):
    content = b"\0" * (1024**2 - 2) + payload
    with pytest.raises(ValueError, match=message):
        stage_video(io.BytesIO(content), io.BytesIO(), len(content), hashlib.sha256(content).hexdigest())


@pytest.fixture
def video_library(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "movie.HEIC").write_bytes(video_bytes())
    (source / "another.MOV").write_bytes(video_bytes(brand=b"mp42"))
    Image.new("RGB", (32, 24), "green").save(source / "still.jpg")
    library = Library(tmp_path / "data")
    library.scan(library.add(str(source)), lambda *args: None, lambda: False)
    rows = library.photos({}, None)["photos"]
    movie = next(row for row in rows if row["filename"] == "movie.HEIC")
    return library, source, movie


def finished(video, job):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = video.status(job["id"])
        if status["state"] != "preparing":
            return status
        time.sleep(0.02)
    raise AssertionError("Playback did not finish")


def fake_conversion(photo, ffmpeg, target, cancelled):
    target.write_bytes(video_bytes())


def test_authenticated_prepare_status_cancel_and_seekable_cache(video_library, monkeypatch):
    library, source, movie = video_library
    monkeypatch.setattr(video_module, "ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(video_module, "prepare_video", fake_conversion)
    before = file_hash(source / "movie.HEIC")
    app = create_app(library.data)
    path = f"/api/photos/{movie['id']}/playback"
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for method, endpoint in (("POST", path), ("GET", path), ("GET", "/api/playback/unknown"), ("DELETE", "/api/playback/unknown")):
            assert client.request(method, endpoint).status_code == 401
        token = client.post("/api/bootstrap", json={"ticket": app.state.access.ticket()}).json()["token"]
        headers = {"X-Framekeep-Token": token}
        assert client.post(path, headers={**headers, "Origin": "https://outside.example"}).status_code == 403
        assert client.post(path, headers={**headers, "Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert client.get(path, headers=headers).status_code == 400
        job = client.post(path, headers=headers).json()
        assert finished(app.state.video, job)["state"] == "ready"
        assert client.get("/api/playback/" + job["id"], headers=headers).json()["state"] == "ready"
        assert client.delete("/api/playback/" + job["id"], headers=headers).json()["ok"]
        response = client.get(path, headers=headers)
        assert response.content == video_bytes()
        assert response.headers["content-type"] == "video/mp4"
        assert response.headers["cache-control"] == "no-store"
        assert "media-src 'self' blob:" in response.headers["content-security-policy"]
        ranged = client.get(path, headers={**headers, "Range": "bytes=8-19"})
        assert ranged.status_code == 206 and ranged.content == response.content[8:20]
        assert client.get(path + "?token=" + token).status_code == 401
        assert client.post(path, headers=headers).json()["state"] == "ready"
        high_path = path + "?quality=4k"
        assert client.get(high_path, headers=headers).status_code == 400
        assert client.post(path + "?quality=8k", headers=headers).status_code == 422
        high_job = client.post(high_path, headers=headers).json()
        assert high_job["quality"] == "4k" and finished(app.state.video, high_job)["state"] == "ready"
        assert client.get(high_path, headers=headers).content == response.content
        assert app.state.video.file(movie["id"], "4k") != app.state.video.file(movie["id"])
        assert not list(app.state.video.folder.glob("prepare-*"))
        (source / "movie.HEIC").write_bytes(b"changed")
        assert client.get(path, headers=headers).status_code == 400
    assert before == hashlib.sha256(video_bytes()).hexdigest()


def test_single_preparation_cancellation_cleanup_and_shutdown(video_library, monkeypatch):
    library, _, movie = video_library
    started, exited = threading.Event(), threading.Event()

    def slow(photo, ffmpeg, target, cancelled):
        target.write_bytes(b"partial")
        started.set()
        while not cancelled():
            time.sleep(0.01)
        exited.set()
        raise ValueError("cancelled")

    monkeypatch.setattr(video_module, "ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(video_module, "prepare_video", slow)
    video = Video(library)
    try:
        job = video.prepare(movie["id"])
        assert started.wait(3)
        assert video.prepare(movie["id"])["id"] == job["id"]
        other = next(row for row in library.photos({}, None)["photos"] if row["filename"] == "another.MOV")
        with pytest.raises(ValueError, match="Another video"):
            video.prepare(other["id"])
        video.cancel("unrelated-request")
        assert not video.stop.is_set()
        video.cancel(job["id"])
        assert finished(video, job)["state"] == "cancelled" and exited.is_set()
        assert not list(video.folder.iterdir())
        monkeypatch.setattr(video_module, "prepare_video", fake_conversion)
        assert finished(video, video.prepare(movie["id"]))["state"] == "ready"
        video._target(movie).unlink()
        monkeypatch.setattr(video_module, "prepare_video", slow)
        video.prepare(movie["id"])
        video.close()
        assert not list(video.folder.iterdir())
        with pytest.raises(ValueError, match="closing"):
            video.prepare(movie["id"])
    finally:
        video.close()


def test_images_missing_decoder_limits_and_failed_decode(video_library, monkeypatch):
    library, _, movie = video_library
    video = Video(library)
    try:
        still = next(row for row in library.photos({}, None)["photos"] if row["media_type"] == "image")
        with pytest.raises(ValueError, match="still image"):
            video.prepare(still["id"])
        monkeypatch.setattr(video_module, "ffmpeg_executable", lambda: None)
        with pytest.raises(ValueError, match="Install FFmpeg"):
            video.prepare(movie["id"])
        monkeypatch.setattr(video_module, "ffmpeg_executable", lambda: "ffmpeg")

        def failed(*args):
            raise ValueError("damaged video")

        monkeypatch.setattr(video_module, "prepare_video", failed)
        assert finished(video, video.prepare(movie["id"]))["message"] == "damaged video"
        with library.connect() as db:
            db.execute("UPDATE photos SET duration=1801 WHERE id=?", (movie["id"],))
        with pytest.raises(ValueError, match="30 minutes"):
            video.prepare(movie["id"])
        assert not list(video.folder.iterdir())
    finally:
        video.close()


def test_cache_budget_removes_only_managed_files(video_library, monkeypatch):
    library, _, movie = video_library
    video = Video(library)
    try:
        older = video.folder / ("a" * 64 + "-v1.mp4")
        older.write_bytes(b"old" * 10)
        unrelated = video.folder / "notes.txt"
        unrelated.write_text("keep")
        keep = video._target(movie)
        keep.write_bytes(b"new" * 10)
        monkeypatch.setattr(video_module, "CACHE_BUDGET", 40)
        video._trim(keep)
        assert not older.exists() and keep.exists() and unrelated.exists()
    finally:
        video.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_decoder_wrapper_terminates_on_timeout_or_cancel(tmp_path, monkeypatch, cancel):
    monkeypatch.setattr(video_module, "_worker_command", lambda _: [sys.executable, "-c", "import time; time.sleep(30)"])
    monkeypatch.setattr(video_module, "WALL_SECONDS", 0)
    photo = {"path": "unused", "source_root": "unused", "sha256": "a" * 64, "size": 1, "mtime_ns": 1}
    start = time.monotonic()
    with pytest.raises(ValueError, match="cancelled" if cancel else "3 minutes"):
        prepare_video(photo, "unused", tmp_path / "playback.mp4", lambda: cancel)
    assert time.monotonic() - start < 5


def test_incomplete_output_is_rejected_instead_of_showing_a_truncated_movie(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "longer.MOV"
    path.write_bytes(video_bytes().replace((5500).to_bytes(4, "big"), (8000).to_bytes(4, "big")))
    assert read_video_metadata(path, source)["metadata"]["duration"] == 8
    work = tmp_path / "work"
    work.mkdir()
    target = work / "playback.mp4"
    info = path.stat()
    request = {"path": str(path), "root": str(source), "target": str(target), "ffmpeg": "unused",
               "sha256": file_hash(path), "size": info.st_size, "mtime_ns": info.st_mtime_ns}

    def incomplete(*args, **kwargs):
        target.write_bytes(video_bytes())  # Successful encoder exit, but only 5.5 of 8 seconds.
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(video_worker.subprocess, "run", incomplete)
    with pytest.raises(ValueError, match="complete video"):
        video_worker.convert(request)
    assert file_hash(path) == request["sha256"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="Optional FFmpeg integration")
@pytest.mark.parametrize("codec,suffix", [("libx264", ".MOV"), ("libx265", ".HEIC"), ("libx265", ".JPG")])
def test_full_length_h264_hevc_and_mislabeled_videos_keep_audio_and_original(tmp_path, codec, suffix):
    ffmpeg = shutil.which("ffmpeg")
    source = tmp_path / "source"
    source.mkdir()
    path = source / ("movie" + suffix)
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
               "testsrc2=size=160x96:rate=12:duration=8", "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
               "-c:v", codec, "-threads", "1"]
    if codec == "libx265":
        command += ["-x265-params", "pools=none:frame-threads=1:log-level=error", "-pix_fmt", "yuv420p10le"]
    command += ["-c:a", "aac", "-f", "mov", str(path)]
    subprocess.run(command, check=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    before = (file_hash(path), path.stat().st_size, path.stat().st_mtime_ns)
    library = Library(tmp_path / "data")
    library.scan(library.add(str(source)), lambda *args: None, lambda: False)
    movie = library.photos({}, None)["photos"][0]
    video = Video(library)
    try:
        assert finished(video, video.prepare(movie["id"]))["state"] == "ready"
        playback = video.file(movie["id"])
        assert read_video_metadata(playback, playback.parent)["metadata"]["duration"] >= 8
        probe = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_streams", "-of", "json", str(playback)],
            capture_output=True, check=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        streams = json.loads(probe.stdout)["streams"]
        assert {stream["codec_name"] for stream in streams} == {"h264", "aac"}
        assert next(stream for stream in streams if stream["codec_type"] == "video")["pix_fmt"] == "yuv420p"
        assert (file_hash(path), path.stat().st_size, path.stat().st_mtime_ns) == before
        # A stale index with unchanged file stats must still fail the first decode's hash check.
        playback.unlink()
        with library.connect() as db:
            db.execute("UPDATE photos SET sha256=? WHERE id=?", ("a" * 64, movie["id"]))
        assert "contents changed" in finished(video, video.prepare(movie["id"]))["message"]
    finally:
        video.close()
