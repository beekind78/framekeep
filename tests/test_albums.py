import pytest
from fastapi.testclient import TestClient
from PIL import Image

from framekeep.app import create_app
from framekeep.library import Library, file_hash
from tests.media_fixtures import video_bytes


@pytest.fixture
def album_library(tmp_path):
    library = Library(tmp_path / "data")
    source, other = tmp_path / "backup", tmp_path / "other"
    source.mkdir()
    other.mkdir()
    entries = [
        ("april-one.jpg", "2026:04:03 12:00:00", "Photos"),
        ("april-two.jpg", "2026:04:04 12:00:00", "Photos"),
        ("january.jpg", "2025:01:02 12:00:00", "Photos"),
        ("screen.jpg", "2026:04:03 12:00:00", "Screenshots"),
        ("drawing.jpg", "2026:03:03 12:00:00", "Illustrations"),
        ("pending.jpg", "2026:04:03 12:00:00", None),
    ]
    for name, date, _ in entries:
        exif = Image.Exif()
        exif[306] = date
        Image.new("RGB", (60, 40), "green").save(source / name, exif=exif)
    (source / "clip.HEIC").write_bytes(video_bytes())
    Image.new("RGB", (60, 40), "orange").save(other / "foreign.jpg")
    library_id = library.add(str(source))
    library.scan(library_id, lambda *args: None, lambda: False)
    other_id = library.add(str(other))
    library.scan(other_id, lambda *args: None, lambda: False)
    rows = {p["filename"]: p for p in library.photos({}, None)["photos"]}
    for name, _, kind in entries:
        if kind:
            library.assign_kind([rows[name]["id"]], kind)
    library.assign_kind([rows["foreign.jpg"]["id"]], "Photos")
    return library, library_id, source, rows


def test_albums_group_manual_categories_by_year_month_and_source(album_library):
    library, library_id, _, rows = album_library
    data = library.albums({"library": library_id})
    assert (data["total"], data["photos"], data["videos"]) == (7, 6, 1)
    albums = {album["kind"]: album for album in data["albums"]}
    assert {kind: album["count"] for kind, album in albums.items()} == {
        "Photos": 3,
        "Screenshots": 1,
        "Illustrations": 1,
        "Videos": 1,
        "Needs review": 1,
    }
    assert albums["Photos"]["years"] == [{"value": "2026", "count": 2}, {"value": "2025", "count": 1}]
    assert albums["Photos"]["months"] == [{"value": "2026-04", "count": 2}, {"value": "2025-01", "count": 1}]
    assert all(
        cover["id"] != rows["foreign.jpg"]["id"] for album in data["albums"] for cover in album["covers"]
    )
    assert all("path" not in cover for album in data["albums"] for cover in album["covers"])
    assert albums["Videos"]["covers"][0]["media_type"] == "video"


def test_album_date_search_and_favorite_filters_match_gallery(album_library):
    library, library_id, _, rows = album_library
    filters = {"library": library_id, "kind": "Photos", "year": "2026", "month": "2026-04"}
    assert library.albums(filters)["total"] == library.photos(filters, None)["total"] == 2
    assert library.albums({"library": library_id, "q": "january"})["albums"][0]["count"] == 1
    library.update_photo(rows["screen.jpg"]["id"], True, "reviewed")
    data = library.albums({"library": library_id, "favorite": True, "q": "reviewed"})
    assert data["total"] == 1 and data["albums"][0]["kind"] == "Screenshots"


def test_manual_correction_immediately_moves_membership_in_app(album_library):
    library, library_id, _, rows = album_library
    library.assign_kind([rows["screen.jpg"]["id"]], "Illustrations")
    data = library.albums({"library": library_id})
    counts = {album["kind"]: album["count"] for album in data["albums"]}
    assert "Screenshots" not in counts and counts["Illustrations"] == 2
    assert library.albums({"library": library_id, "kind": "Screenshots"})["total"] == 0
    library.assign_kind([rows["screen.jpg"]["id"]], None)
    assert library.albums({"library": library_id, "kind": "Needs review"})["total"] == 2


def test_album_api_is_authenticated_and_does_not_move_or_copy_files(album_library):
    library, library_id, source, _ = album_library
    app = create_app(library.data)
    before = {p.name: file_hash(p) for p in source.iterdir()}
    with library.connect() as db:
        records = [tuple(row) for row in db.execute("SELECT * FROM photos ORDER BY id")]
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/albums").status_code == 401
        token = client.post("/api/bootstrap", json={"ticket": app.state.access.ticket()}).json()["token"]
        headers = {"X-Framekeep-Token": token}
        assert (
            client.get("/api/albums", headers={**headers, "Origin": "https://other.example"}).status_code
            == 403
        )
        response = client.get(f"/api/albums?library={library_id}&favorite=0", headers=headers)
        assert response.status_code == 200 and response.json()["total"] == 7
        assert client.get(f"/api/albums?library={library_id}&year=2025", headers=headers).json()["total"] == 1
    assert before == {p.name: file_hash(p) for p in source.iterdir()}
    with library.connect() as db:
        assert records == [tuple(row) for row in db.execute("SELECT * FROM photos ORDER BY id")]


def test_empty_album_and_missing_records_do_not_create_membership(album_library):
    library, library_id, _, rows = album_library
    with library.connect() as db:
        db.execute("UPDATE photos SET missing=1 WHERE id=?", (rows["screen.jpg"]["id"],))
    data = library.albums({"library": library_id, "kind": "Screenshots"})
    assert data == {"total": 0, "photos": 0, "videos": 0, "live": 0, "bytes": 0, "albums": []}


def test_large_album_has_bounded_covers_and_complete_gallery_pagination(album_library):
    library, library_id, source, rows = album_library
    with library.connect() as db:
        seed = dict(db.execute("SELECT * FROM photos WHERE id=?", (rows["april-one.jpg"]["id"],)).fetchone())
        columns = [key for key in seed if key != "id"]
        for index in range(125):
            entry = {**seed, "path": str(source / f"cached-{index}.jpg"), "filename": f"cached-{index}.jpg"}
            db.execute(
                f"INSERT INTO photos({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                [entry[key] for key in columns],
            )
    filters = {"library": library_id, "kind": "Photos"}
    album = library.albums(filters)["albums"][0]
    assert album["count"] == 128 and len(album["covers"]) == 4
    first = library.photos(filters, 120)
    second = library.photos(filters, 120, 120)
    assert first["total"] == 128 and len(first["photos"]) == 120 and len(second["photos"]) == 8
    assert not {p["id"] for p in first["photos"]} & {p["id"] for p in second["photos"]}
