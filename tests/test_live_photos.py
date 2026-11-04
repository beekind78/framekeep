import hashlib
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import framekeep.motion as motion_module
from framekeep.app import create_app
from framekeep.image_processing import process_motion, process_photo_metadata, process_video
from framekeep.library import Library, file_hash
from framekeep.live_metadata import apple_identifier, identifier
from framekeep.live_photos import LivePhotos
from framekeep.motion import Motion
from tests.media_fixtures import atom, video_bytes

IDENTIFIER = "d4f6281e-016f-4c9e-a33e-060e2ca3e185"
OTHER = "637b0857-7be7-4d41-a956-b5f56d625fbf"


def maker_note(value=IDENTIFIER):
    raw = value.encode() + b"\0"
    return b"Apple iOS\0\x00\x01MM" + struct.pack(">HHHII", 1, 17, 2, len(raw), 32) + b"\0" * 4 + raw


def still_file(path, value=IDENTIFIER):
    exif = Image.Exif()
    exif[271] = "Apple"
    exif[272] = "iPhone"
    exif[34665] = {37500: maker_note(value), 36867: "2026:04:03 12:00:00"}
    Image.new("RGB", (96, 64), "#52865c").save(path, exif=exif)


def live_movie(value=IDENTIFIER, *, qt=False, reverse=False):
    key = b"com.apple.quicktime.content.identifier"
    keys = atom(b"keys", b"\0" * 4 + struct.pack(">I", 1) + struct.pack(">I", len(key) + 8) + b"mdta" + key)
    data = atom(b"data", struct.pack(">II", 1, 0) + value.encode())
    items = atom(b"ilst", atom(struct.pack(">I", 1), data))
    meta = atom(b"meta", (b"" if qt else b"\0" * 4) + (items + keys if reverse else keys + items))
    raw = video_bytes()
    position = raw.index(b"moov") - 4
    return raw[:position] + atom(b"moov", raw[position + 8:] + meta)


@pytest.fixture
def live_library(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    still_file(source / "renamed-photo.JPG")
    (source / "different-name.HEIC").write_bytes(live_movie())
    (source / "ordinary.MOV").write_bytes(video_bytes())
    library = Library(tmp_path / "data")
    library_id = library.add(str(source))
    result = library.scan(library_id, lambda *args: None, lambda: False)
    assert result["error_count"] == 0
    return library, source, library_id


@pytest.mark.parametrize("extension", ["JPG", "HEIC"])
def test_iphone_still_identifier_in_bounded_worker_without_changing_original(tmp_path, extension):
    path = tmp_path / ("photo." + extension)
    still_file(path)
    before = file_hash(path)
    assert process_photo_metadata(path, tmp_path)["metadata"]["live_identifier"] == IDENTIFIER
    assert file_hash(path) == before


@pytest.mark.parametrize("qt,reverse", [(False, False), (True, False), (False, True)])
def test_live_movie_identifier_supports_metadata_order_and_quicktime_layout(tmp_path, qt, reverse):
    path = tmp_path / "movie.HEIC"
    path.write_bytes(live_movie(qt=qt, reverse=reverse))
    assert process_video(path, tmp_path)["metadata"]["live_identifier"] == IDENTIFIER


def test_malformed_or_conflicting_identifiers_never_create_a_pair():
    assert identifier(IDENTIFIER.upper()) == IDENTIFIER
    assert identifier("not-a-content-identifier") == ""
    assert identifier(b"\xff") == ""
    assert apple_identifier(SimpleNamespace(get_ifd=lambda _: {37500: b"Apple iOS\0"})) == ""
    note = bytearray(maker_note())
    note[24:28] = struct.pack(">I", 999999)
    assert apple_identifier(SimpleNamespace(get_ifd=lambda _: {37500: bytes(note)})) == ""


def test_verified_pairs_collapse_only_motion_keep_manual_categories_and_all_originals(live_library):
    library, source, library_id = live_library
    before = {p.name: file_hash(p) for p in source.iterdir()}
    rows = library.photos({}, None)["photos"]
    still = next(row for row in rows if row["media_type"] == "image")
    library.assign_kind([still["id"]], "Illustrations")
    summary = library.summary()
    assert summary["file_total"] == 3 and summary["total"] == 2
    assert summary["live_total"] == 1 and summary["video_total"] == 1
    assert summary["libraries"][0]["count"] == 2 and summary["libraries"][0]["file_count"] == 3
    gallery = library.photos({"collapse_live": True}, None)
    assert gallery["total"] == 2
    assert library.photos({"live_only": True}, None)["total"] == 1
    assert library.albums({"collapse_live": True})["total"] == 2
    assert library.photo(still["id"])["kind"] == "Illustrations"
    service = LivePhotos(library)
    result = service.analyze({"library": library_id}, lambda *args: None, lambda: False)
    assert result["metadata_checked"] == 0 and result["live_photos"] == 1
    assert {p.name: file_hash(p) for p in source.iterdir()} == before


def test_live_photos_are_in_the_photo_album_and_never_in_video_album_counts(live_library):
    library, _, library_id = live_library
    data = library.albums({"library": library_id})
    albums = {album["kind"]: album for album in data["albums"]}
    assert (data["total"], data["photos"], data["videos"], data["live"]) == (2, 1, 1, 1)
    assert albums["Photos"]["count"] == albums["Photos"]["live"] == 1
    assert albums["Photos"]["covers"][0]["live"] == 1
    assert albums["Videos"]["count"] == 1 and albums["Videos"]["live"] == 0
    assert albums["Videos"]["covers"][0]["filename"] == "ordinary.MOV"
    assert library.albums({"kind": "Videos", "collapse_live": False})["total"] == 1
    assert library.photos({"library": library_id}, None)["total"] == 3  # Both original resources remain.


def test_album_live_counts_follow_search_favorites_manual_choices_and_missing_motion(live_library):
    library, _, library_id = live_library
    still = library.photos({"live_only": True}, 1)["photos"][0]
    library.update_photo(still["id"], True, "holiday")
    data = library.albums({"library": library_id, "q": "holiday", "favorite": True})
    assert data["total"] == data["live"] == 1 and data["videos"] == 0
    assert library.albums({"q": "ordinary"})["live"] == 0
    assert library.albums({"library": 99999})["live"] == 0
    library.assign_kind([still["id"]], "Illustrations")
    album = next(album for album in library.albums({})["albums"] if album["kind"] == "Illustrations")
    assert album["live"] == 1 and library.photo(still["id"])["kind_override"] == "Illustrations"
    with library.connect() as db:
        db.execute("UPDATE photos SET missing=1 WHERE id=?", (still["live_motion_id"],))
    assert library.albums({})["live"] == 0


def test_matching_names_or_dates_are_insufficient_and_ambiguity_or_source_replacement_unlinks(live_library):
    library, source, library_id = live_library
    still_file(source / "unrelated.JPG", OTHER)
    (source / "unrelated.MOV").write_bytes(video_bytes())
    library.scan(library_id, lambda *args: None, lambda: False)
    assert library.summary()["live_total"] == 1
    still_file(source / "ambiguous.JPG")
    library.scan(library_id, lambda *args: None, lambda: False)
    assert library.summary()["live_total"] == 0
    (source / "ambiguous.JPG").unlink()
    library.scan(library_id, lambda *args: None, lambda: False)
    assert library.summary()["live_total"] == 1
    motion = library.photos({"live_only": True}, 1)["photos"][0]["live_motion_id"]
    with library.connect() as db:
        db.execute("UPDATE photos SET sha256='new-content' WHERE id=?", (motion,))
    assert library.summary()["live_total"] == 0
    assert library.photos({"live_only": True}, None)["total"] == 0


@pytest.mark.parametrize("layout", ["date", "kind_date", "date_type", "type", "people"])
def test_copying_one_live_photo_preserves_both_resources_together_and_hash_verified(live_library, tmp_path, layout):
    library, source, _ = live_library
    still = library.photos({"live_only": True}, 1)["photos"][0]
    output = tmp_path / "output"
    output.mkdir()
    plan = library.plan(str(output), layout, {"ids": [still["id"]], "collapse_live": True}, True)
    assert plan["photos"] == plan["videos"] == 1
    assert len({Path(name).parent for name in plan["sample"]}) == 1
    assert len({Path(name).stem for name in plan["sample"]}) == 1
    result = library.export(library.consume_plan(plan["token"]), lambda *args: None, lambda: False)
    assert result["copied"] == 2 and result["error_count"] == 0
    copies = [p for p in Path(result["destination"]).rglob("*") if p.suffix.lower() in (".jpg", ".mov")]
    assert {file_hash(path) for path in copies} == {file_hash(source / "renamed-photo.JPG"), file_hash(source / "different-name.HEIC")}
    motion_plan = library.plan(str(output), layout, {"ids": [still["live_motion_id"]]}, True)
    assert motion_plan["count"] == 2


def test_live_endpoints_require_auth_origin_and_verified_pairs(live_library, monkeypatch):
    library, _, _ = live_library
    app = create_app(library.data)
    still = library.photos({"live_only": True}, 1)["photos"][0]
    monkeypatch.setattr(app.state.motion, "preview", lambda _: b"test-mp4")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        motion_path = f"/api/photos/{still['id']}/motion"
        assert client.post(motion_path).status_code == 401
        assert client.post("/api/live-photos/analyze", json={}).status_code == 401
        token = client.post("/api/bootstrap", json={"ticket": app.state.access.ticket()}).json()["token"]
        headers = {"X-Framekeep-Token": token}
        assert client.post(motion_path, headers={**headers, "Origin": "https://other.example"}).status_code == 403
        response = client.post(motion_path, headers=headers)
        assert response.content == b"test-mp4" and response.headers["content-type"] == "video/mp4"
        assert response.headers["cache-control"] == "no-store"
        assert "media-src 'self' blob:" in response.headers["content-security-policy"]
        assert client.get("/api/photos?collapse_live=1", headers=headers).json()["total"] == 2
        assert client.get("/api/photos?live_only=1", headers=headers).json()["total"] == 1
        assert client.get("/api/albums?collapse_live=1", headers=headers).json()["total"] == 2
        assert client.get("/api/albums", headers=headers).json()["total"] == 2
        assert client.get("/api/albums", headers=headers).json()["live"] == 1


def test_on_demand_motion_cache_and_changed_or_unpaired_sources_fail(live_library, monkeypatch):
    library, source, _ = live_library
    motion = Motion(library)
    still = library.photos({"live_only": True}, 1)["photos"][0]
    calls = []
    monkeypatch.setattr(motion_module.shutil, "which", lambda _: "ffmpeg")
    monkeypatch.setattr(motion_module, "process_motion", lambda *args: calls.append(args) or b"test-mp4")
    assert motion.preview(still["id"]) == motion.preview(still["id"]) == b"test-mp4"
    assert len(calls) == 1
    ordinary = next(row for row in library.photos({}, None)["photos"] if row["filename"] == "ordinary.MOV")
    with pytest.raises(ValueError, match="no verified"):
        motion.preview(ordinary["id"])
    (source / "different-name.HEIC").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        motion.preview(still["id"])


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="Optional FFmpeg integration")
def test_real_bounded_motion_conversion_is_playable_mp4_and_original_unchanged(tmp_path):
    ffmpeg = Path(shutil.which("ffmpeg"))
    source = tmp_path / "motion.MOV"
    command = [str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
               "-i", "testsrc2=size=160x96:rate=12:duration=1", "-c:v", "libx264", "-threads", "1",
               "-pix_fmt", "yuv420p", "-metadata", "com.apple.quicktime.content.identifier=" + IDENTIFIER,
               "-movflags", "use_metadata_tags", str(source)]
    subprocess.run(command, check=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    before = file_hash(source)
    assert process_video(source, tmp_path)["metadata"]["live_identifier"] == IDENTIFIER
    result = process_motion(source, tmp_path, ffmpeg, before)
    assert result[4:8] == b"ftyp" and len(result) < 8 * 1024 * 1024
    assert file_hash(source) == before
    with pytest.raises(ValueError, match="contents changed"):
        process_motion(source, tmp_path, ffmpeg, hashlib.sha256(b"different").hexdigest())
