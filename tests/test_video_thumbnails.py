import io
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import framekeep.image_processing as processing
import framekeep.video as video_module
from framekeep.app import create_app
from framekeep.image_processing import process_video_thumbnail
from framekeep.library import Library, file_hash
from framekeep.video import Video
from tests.media_fixtures import video_bytes


@pytest.fixture
def library(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "movie.HEIC").write_bytes(video_bytes())
    Image.new("RGB", (32, 24), "green").save(source / "still.jpg")
    library = Library(tmp_path / "data")
    library.scan(library.add(str(source)), lambda *args: None, lambda: False)
    movie = next(row for row in library.photos({}, None)["photos"] if row["media_type"] == "video")
    return library, source, movie


def jpeg():
    output = io.BytesIO()
    Image.new("RGB", (160, 90), "blue").save(output, "JPEG")
    return output.getvalue()


def test_authenticated_video_thumbnail_cache_and_source_changes(library, monkeypatch):
    library, source, movie = library
    calls = []
    monkeypatch.setattr(video_module, "ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(video_module, "process_video_thumbnail", lambda *args: calls.append(args) or jpeg())
    original = file_hash(source / "movie.HEIC")
    app = create_app(library.data)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        path = f"/api/photos/{movie['id']}/thumbnail"
        assert client.get(path).status_code == 401
        token = client.post("/api/bootstrap", json={"ticket": app.state.access.ticket()}).json()["token"]
        headers = {"X-Framekeep-Token": token}
        assert client.get(path, headers={**headers, "Origin": "https://outside.example"}).status_code == 403
        assert client.get(path, headers={**headers, "Sec-Fetch-Site": "cross-site"}).status_code == 403
        response = client.get(path, headers=headers)
        assert response.content == jpeg() and response.headers["content-type"] == "image/jpeg"
        assert response.headers["cache-control"] == "no-store"
        assert "img-src 'self' blob:" in response.headers["content-security-policy"]
        assert client.get(path + "?token=" + token).status_code == 401
        monkeypatch.setattr(video_module, "ffmpeg_executable", lambda: None)
        assert client.get(path, headers=headers).content == response.content  # Cached poster works offline.
        assert len(calls) == 1
        assert file_hash(source / "movie.HEIC") == original
        assert not list(app.state.video.folder.iterdir())  # No full-movie transcode was needed.
        (source / "movie.HEIC").write_bytes(b"changed")
        assert client.get(path, headers=headers).status_code == 400


def test_concurrent_reads_share_one_generated_thumbnail(library, monkeypatch):
    library, _, movie = library
    video, calls = Video(library), []
    monkeypatch.setattr(video_module, "ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(video_module, "process_video_thumbnail", lambda *args: calls.append(args) or jpeg())
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            images = list(pool.map(video.thumbnail, [movie["id"]] * 4))
        assert images == [jpeg()] * 4 and len(calls) == 1
    finally:
        video.close()


def test_failed_preparation_cleans_parent_owned_temporary_source(library, monkeypatch):
    library, _, movie = library
    movie = library.photo(movie["id"])
    directories = []

    def failed(request):
        work = Path(request["work"])
        directories.append(work)
        (work / "input.mov").write_bytes(b"private temporary copy")
        raise ValueError("worker timed out")

    monkeypatch.setattr(processing, "_run_worker", failed)
    with pytest.raises(ValueError, match="timed out"):
        process_video_thumbnail(movie, "unused")
    assert directories and not any(path.exists() for path in directories)


def test_missing_decoder_invalid_output_and_managed_cache_budget(library, monkeypatch):
    library, _, movie = library
    video = Video(library)
    try:
        monkeypatch.setattr(video_module, "ffmpeg_executable", lambda: None)
        with pytest.raises(ValueError, match="Install FFmpeg"):
            video.thumbnail(movie["id"])
        monkeypatch.setattr(video_module, "ffmpeg_executable", lambda: "ffmpeg")
        monkeypatch.setattr(video_module, "process_video_thumbnail", lambda *args: b"invalid")
        with pytest.raises(ValueError, match="not a JPEG"):
            video.thumbnail(movie["id"])
        assert not list(video.thumbnails.iterdir())
        older = video.thumbnails / ("a" * 64 + "-v1.jpg")
        older.write_bytes(jpeg())
        notes = video.thumbnails / "keep.txt"
        notes.write_text("keep")
        monkeypatch.setattr(video_module, "THUMBNAIL_BUDGET", len(jpeg()) + 1)
        monkeypatch.setattr(video_module, "process_video_thumbnail", lambda *args: jpeg())
        assert video.thumbnail(movie["id"]) == jpeg()
        assert not older.exists() and notes.exists() and len(list(video.thumbnails.glob("*.jpg"))) == 1
    finally:
        video.close()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="Optional FFmpeg integration")
@pytest.mark.parametrize("codec,suffix,duration,rotation", [
    ("libx264", ".MOV", 2, 0), ("libx265", ".HEIC", 2, 90), ("libx264", ".JPG", .08, 0),
])
def test_real_video_frame_hevc_rotation_short_clips_hash_and_original(library, codec, suffix, duration, rotation):
    library, source, _ = library
    ffmpeg = shutil.which("ffmpeg")
    base, path = source.parent / "generated.mov", source / ("encoded" + suffix)
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
               f"color=c=blue:size=640x360:rate=25:duration={duration}", "-c:v", codec, "-threads", "1"]
    if codec == "libx265":
        command += ["-x265-params", "pools=none:frame-threads=1:log-level=error", "-pix_fmt", "yuv420p10le"]
    command += [str(base)]
    subprocess.run(command, check=True, timeout=20, creationflags=flags)
    subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-display_rotation", str(rotation),
                    "-i", str(base), "-c", "copy", "-f", "mov", str(path)], check=True, timeout=10, creationflags=flags)
    before = file_hash(path), path.stat().st_size, path.stat().st_mtime_ns
    library.scan(library.add(str(source)), lambda *args: None, lambda: False)
    movie = next(row for row in library.photos({}, None)["photos"] if row["filename"] == path.name)
    video = Video(library)
    try:
        data = video.thumbnail(movie["id"])
        assert len(data) < 512 * 1024
        with Image.open(io.BytesIO(data)) as image:
            assert image.format == "JPEG" and max(image.size) <= 480
            assert (image.width < image.height) is bool(rotation)
            red, green, blue = image.convert("RGB").getpixel((image.width // 2, image.height // 2))
            assert blue > 200 and red < 30 and green < 30  # A real blue video frame, not a generic icon.
            assert not image.getexif()
        assert (file_hash(path), path.stat().st_size, path.stat().st_mtime_ns) == before
        assert not list(video.folder.iterdir())
        cached = next(video.thumbnails.glob("*.jpg"))
        cached.unlink()
        with library.connect() as db:
            db.execute("UPDATE photos SET sha256=? WHERE id=?", ("b" * 64, movie["id"]))
        with pytest.raises(ValueError, match="contents changed"):
            video.thumbnail(movie["id"])
        assert not list(video.thumbnails.glob("*.jpg"))
    finally:
        video.close()
