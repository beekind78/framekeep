import csv
import struct
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from framekeep.app import create_app
from framekeep.categories import Categories
from framekeep.image_processing import process_video
from framekeep.library import Library, file_hash
from framekeep.output_files import output_directory
from framekeep.people import People
from tests.media_fixtures import atom, video_bytes


@pytest.mark.parametrize("version,legacy,extended", [(0, False, False), (1, False, True), (0, True, False)])
def test_video_dates_duration_and_mislabeled_suffix(tmp_path, version, legacy, extended):
    path = tmp_path / "not_a_photo.HEIC"
    path.write_bytes(video_bytes(version=version, legacy=legacy, extended=extended))
    metadata = process_video(path, tmp_path)["metadata"]
    expected = datetime(2026, 4, 3, 4, tzinfo=UTC).astimezone().replace(tzinfo=None).isoformat()
    assert metadata["captured_at"] == expected
    assert metadata["date_source"] == "Video metadata"
    assert metadata["duration"] == 5.5
    assert metadata["extension"] == "MOV" and metadata["media_type"] == "video"


@pytest.mark.parametrize(
    "data,reason",
    [
        (video_bytes(has_video=False), "no video track"),
        (struct.pack(">I4s", 100000, b"moov"), "boundary"),
        (atom(b"free", b"") * 4097, "excessive atoms"),
    ],
)
def test_invalid_or_nonvideo_containers_fail_within_limits(tmp_path, data, reason):
    path = tmp_path / "video.MOV"
    path.write_bytes(data)
    with pytest.raises(ValueError, match=reason):
        process_video(path, tmp_path)


def test_video_without_creation_date_uses_filename_date(tmp_path):
    path = tmp_path / "VID_20250203_040506.mp4"
    path.write_bytes(video_bytes(date=datetime(1904, 1, 1, tzinfo=UTC), brand=b"mp42"))
    metadata = process_video(path, tmp_path)["metadata"]
    assert metadata["captured_at"] == "2025-02-03T04:05:06"
    assert metadata["date_source"] == "Filename" and metadata["extension"] == "MP4"


def test_category_year_named_month_direct_copy_matches_example_and_never_overwrites(tmp_path):
    source, output = tmp_path / "source", tmp_path / "output"
    source.mkdir()
    output.mkdir()
    for name, date, color in (
        ("photo.jpg", "2026:04:02 12:00:00", "red"),
        ("shot.png", "2025:01:02 12:00:00", "green"),
        ("drawing.jpg", "2026:03:02 12:00:00", "blue"),
    ):
        exif = Image.Exif()
        exif[306] = date
        Image.new("RGB", (80, 50), color).save(source / name, exif=exif)
    (source / "wrong_suffix.HEIC").write_bytes(video_bytes())
    original_hashes = {p.name: file_hash(p) for p in source.iterdir()}
    library = Library(tmp_path / "data")
    library_id = library.add(str(source))
    scan = library.scan(library_id, lambda *args: None, lambda: False)
    assert scan["found"] == 3 and scan["videos"] == 1 and scan["error_count"] == 0
    rows = {p["filename"]: p for p in library.photos({}, None)["photos"]}
    for name, kind in (
        ("photo.jpg", "Photos"),
        ("shot.png", "Screenshots"),
        ("drawing.jpg", "Illustrations"),
    ):
        library.assign_kind([rows[name]["id"]], kind)
    collision = output / "Photos" / "2026" / "April" / "photo.jpg"
    collision.parent.mkdir(parents=True)
    collision.write_bytes(b"keep the existing file")
    preview = library.plan(str(output), "kind_date", {}, False, create_run_folder=False)
    assert preview["photos"] == 3 and preview["videos"] == 1 and not preview["create_run_folder"]
    assert set(preview["sample"]) == {
        str(Path("Photos/2026/April/photo.jpg")),
        str(Path("Screenshot/2025/January/shot.png")),
        str(Path("Illustration/2026/March/drawing.jpg")),
        str(Path("Video/2026/April/wrong_suffix.mov")),
    }
    result = library.export(library.consume_plan(preview["token"]), lambda *args: None, lambda: False)
    assert result["copied"] == 4 and result["copied_videos"] == 1 and result["copied_photos"] == 3
    assert result["error_count"] == 0 and Path(result["destination"]) == output
    assert collision.read_bytes() == b"keep the existing file"
    assert file_hash(collision.with_name("photo (1).jpg")) == original_hashes["photo.jpg"]
    assert {p.name for p in output.iterdir() if p.is_dir()} == {
        "Photos",
        "Screenshot",
        "Illustration",
        "Video",
    }
    assert file_hash(output / "Video/2026/April/wrong_suffix.mov") == original_hashes["wrong_suffix.HEIC"]
    with Path(result["manifest"]).open(encoding="utf-8-sig", newline="") as stream:
        assert all(row["status"] == "verified" for row in csv.DictReader(stream))
    assert original_hashes == {p.name: file_hash(p) for p in source.iterdir()}
    assert library.scan(library_id, lambda *args: None, lambda: False)["indexed"] == 0
    with pytest.raises(ValueError):
        library.assign_kind([rows["wrong_suffix.HEIC"]["id"]], "Photos")


def test_category_detection_never_sends_videos_to_image_worker(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "video.mov").write_bytes(video_bytes())
    library = Library(tmp_path / "data")
    library.scan(library.add(str(source)), lambda *args: None, lambda: False)
    service = Categories(library)
    monkeypatch.setattr(service, "ready", lambda: True)
    result = service.analyze({}, lambda *args: None, lambda: False)
    assert result["categorized"] == 0 and not result["errors"]
    monkeypatch.setattr("framekeep.people.MODELS", {})
    people = People(library)
    result = people.analyze(lambda *args: None, lambda: False)
    assert result["faces"] == 0 and not result["errors"]


def test_api_previews_and_creates_category_year_month_output_with_video(tmp_path):
    source, output = tmp_path / "source", tmp_path / "output"
    source.mkdir()
    output.mkdir()
    (source / "video.mp4").write_bytes(video_bytes(brand=b"mp42"))
    app = create_app(tmp_path / "data")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.post("/api/bootstrap", json={"ticket": app.state.access.ticket()}).json()["token"]
        headers = {"X-Framekeep-Token": token}

        def finished():
            for _ in range(200):
                job = client.get("/api/job", headers=headers).json()["job"]
                if job["state"] in ("complete", "failed", "cancelled"):
                    assert job["state"] == "complete", job
                    return job["result"]
                time.sleep(0.02)
            raise AssertionError("Generated-video task did not complete.")

        assert client.post("/api/scan", json={"path": str(source)}, headers=headers).status_code == 200
        assert finished()["videos"] == 1
        video = client.get("/api/photos?kind=Videos", headers=headers).json()["photos"][0]
        assert client.get(f"/api/photos/{video['id']}/preview", headers=headers).status_code == 400
        assert client.get(f"/api/photos/{video['id']}/thumbnail", headers=headers).status_code == 400
        body = {"destination": str(output), "layout": "kind_date", "create_run_folder": False}
        assert client.post("/api/organize/preview", json=body).status_code == 401
        preview = client.post("/api/organize/preview", json=body, headers=headers)
        assert preview.status_code == 200
        assert preview.json()["videos"] == 1 and not preview.json()["create_run_folder"]
        assert (
            client.post(
                "/api/organize/copy", json={"token": preview.json()["token"]}, headers=headers
            ).status_code
            == 200
        )
        result = finished()
        assert result["copied_videos"] == 1 and Path(result["destination"]) == output
        assert (output / "Video/2026/April/video.mp4").is_file()


def test_direct_output_rejects_existing_junction_or_symlink(tmp_path):
    root, outside = tmp_path / "output", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "Photos"
    if sys.platform == "win32":
        response = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert response.returncode == 0, response.stderr
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError), output_directory(root, Path("Photos/2026/April")):
            pass
        assert not list(outside.iterdir())
    finally:
        link.rmdir() if sys.platform == "win32" else link.unlink()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows output directory handle sharing")
def test_windows_output_directory_cannot_be_swapped_while_copying(tmp_path):
    root = tmp_path / "output"
    root.mkdir()
    with output_directory(root, Path("Photos/2026/April")), pytest.raises(OSError):
        (root / "Photos").rename(root / "moved")
    (root / "Photos").rename(root / "moved")
    assert (root / "moved").is_dir()
