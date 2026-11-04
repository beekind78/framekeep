import struct
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from framekeep import clipboard
from framekeep.app import create_app
from framekeep.editing import save_edit
from framekeep.library import Library, file_hash
from tests.test_live_photos import live_movie, still_file


@pytest.fixture
def catalog(tmp_path):
    source, output = tmp_path / "source", tmp_path / "output"
    source.mkdir()
    output.mkdir()
    still_file(source / "live.JPG")
    (source / "motion.MOV").write_bytes(live_movie())
    exif = Image.Exif()
    exif[274], exif[306] = 6, "2026:04:03 12:00:00"
    Image.new("RGB", (400, 240), "blue").save(source / "ordinary.jpg", exif=exif)
    library = Library(tmp_path / "data")
    library_id = library.add(str(source))
    library.scan(library_id, lambda *args: None, lambda: False)
    photo = next(row for row in library.photos({}, None)["photos"] if row["filename"] == "ordinary.jpg")
    library.update_photo(photo["id"], True, "keep this tag")
    library.assign_kind([photo["id"]], "Illustrations")
    return library, library_id, source, output, photo


def test_remove_rescan_restore_keeps_originals_tags_categories_and_live_bundle(catalog):
    library, library_id, source, output, photo = catalog
    hashes = {path.name: file_hash(path) for path in source.iterdir()}
    preview = library.plan(str(output), "date", {}, False)
    assert library.remove_photo(photo["id"])["removed"] == 1
    with pytest.raises(ValueError):
        library.consume_plan(preview["token"])
    with pytest.raises(ValueError):
        library.photo(photo["id"])
    library.scan(library_id, lambda *args: None, lambda: False)
    assert library.photos({}, None)["total"] == 2  # Removed image stays hidden across scans.
    assert library.removed_items()["total"] == 1
    restored_library = library.restore_photo(photo["id"])
    library.scan(restored_library, lambda *args: None, lambda: False)
    restored = library.photo(photo["id"])
    assert restored["tags"] == "keep this tag" and restored["favorite"] == 1
    assert restored["kind_override"] == "Illustrations"
    live = library.photos({"live_only": True}, None)["photos"][0]
    assert library.remove_photo(live["id"])["removed"] == 2
    assert library.summary()["live_total"] == 0
    assert len(library.removed_items()["photos"]) == 1
    library.scan(library.restore_photo(live["id"]), lambda *args: None, lambda: False)
    assert library.summary()["live_total"] == 1
    library.remove_library(library_id)
    assert not library.summary()["libraries"] and library.summary()["total"] == 0
    with pytest.raises(ValueError, match="removed"):
        library.scan(library_id, lambda *args: None, lambda: False)
    assert library.removed_items()["sources"][0]["id"] == library_id
    library.scan(library.restore_library(library_id), lambda *args: None, lambda: False)
    assert library.summary()["total"] == 2 and library.photo(photo["id"])["tags"] == "keep this tag"
    assert hashes == {path.name: file_hash(path) for path in source.iterdir()}


def test_source_restore_does_not_restore_individually_removed_items(catalog):
    library, library_id, _, _, photo = catalog
    library.remove_photo(photo["id"])
    library.remove_library(library_id)
    library.scan(library.restore_library(library_id), lambda *args: None, lambda: False)
    assert library.removed_items()["total"] == 1
    with pytest.raises(ValueError):
        library.photo(photo["id"])


def test_removed_sample_folder_can_be_restored_inside_app_data(catalog):
    library, _, _, _, _ = catalog
    sample = library.data / "demo-library"
    sample.mkdir()
    Image.new("RGB", (40,30), "green").save(sample / "sample.jpg")
    sample_id = library.add(str(sample), demo=True)
    library.scan(sample_id, lambda *args: None, lambda: False)
    library.remove_library(sample_id)
    assert library.restore_library(sample_id) == sample_id
    library.scan(sample_id, lambda *args: None, lambda: False)
    assert library.photos({"library":sample_id}, None)["total"] == 1


def test_edited_copy_uses_full_resolution_orientation_crop_and_never_overwrites(catalog):
    library, _, source, output, photo = catalog
    before = file_hash(source / "ordinary.jpg")
    existing = output / "ordinary-edited.jpg"
    existing.write_bytes(b"keep existing file")
    result = save_edit(library, photo["id"], str(output), 1, [.25,.25,.75,.75], lambda *args: None, lambda: False)
    assert Path(result["edited_copy"]).name == "ordinary-edited (2).jpg"
    assert (result["width"], result["height"]) == (200,120)
    with Image.open(result["edited_copy"]) as image:
        assert image.size == (200,120) and image.getexif()[274] == 1
        assert image.getexif()[306] == "2026:04:03 12:00:00"
        assert 34853 not in image.getexif()  # No source GPS data in the derivative.
    assert existing.read_bytes() == b"keep existing file"
    assert file_hash(source / "ordinary.jpg") == before
    with pytest.raises(ValueError, match="separate"):
        save_edit(library, photo["id"], str(source), 0, [0,0,1,1], lambda *args: None, lambda: False)
    with pytest.raises(ValueError, match="crop"):
        save_edit(library, photo["id"], str(output), 0, [.8,0,.2,1], lambda *args: None, lambda: False)
    with library.connect() as db:
        db.execute("UPDATE photos SET sha256=? WHERE id=?", ("b" * 64, photo["id"]))
    with pytest.raises(ValueError, match="contents changed"):
        save_edit(library, photo["id"], str(output), 0, [0,0,1,1], lambda *args: None, lambda: False)
    assert len(list(output.iterdir())) == 2


def test_clipboard_file_format_is_unicode_double_terminated_and_copy_only(monkeypatch):
    paths = ["C:\\photos\\家族.jpg", "C:\\photos\\motion.MOV"]
    payload = clipboard.file_payload(paths)
    assert struct.unpack("<IiiII", payload[:20]) == (20,0,0,0,1)
    assert payload[20:].decode("utf-16-le") == "\0".join(paths) + "\0\0"
    published = []
    monkeypatch.setattr(clipboard, "_publish", lambda formats: published.extend(formats))
    clipboard.copy_files(paths)
    assert published[0] == (15, payload)
    assert published[1] == ("Preferred DropEffect", struct.pack("<I", 1))


def test_actions_require_auth_origin_visibility_and_idle_tasks(catalog, monkeypatch):
    library, library_id, source, output, photo = catalog
    app = create_app(library.data)
    writes, gate = [], threading.Event()
    monkeypatch.setattr(clipboard, "copy_files", lambda paths: writes.append(paths))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        base = f"/api/photos/{photo['id']}"
        for method, path, body in (("DELETE", base, None), ("DELETE", f"/api/libraries/{library_id}", None),
                ("GET", "/api/removed", None), ("POST", base+"/clipboard", {}),
                ("POST", base+"/edit", {"destination":str(output)}), ("POST", base+"/restore", {})):
            assert client.request(method,path,json=body).status_code == 401
        token = client.post("/api/bootstrap", json={"ticket":app.state.access.ticket()}).json()["token"]
        headers = {"X-Framekeep-Token":token}
        assert client.delete(base, headers={**headers,"Origin":"https://outside.example"}).status_code == 403
        live = library.photos({"live_only": True}, None)["photos"][0]
        response = client.post(f"/api/photos/{live['id']}/clipboard", headers=headers, json={})
        assert response.json()["copied"] == 2 and len(writes[0]) == 2
        assert {Path(path).name for path in writes[0]} == {"live.JPG","motion.MOV"}
        app.state.jobs.submit("held task", lambda *args: gate.wait(3))
        try:
            assert client.delete(base, headers=headers).status_code == 400
        finally:
            gate.set()
        for _ in range(100):
            if not app.state.jobs.busy():
                break
            time.sleep(.01)
        assert client.delete(base, headers=headers).status_code == 200
        assert client.get(base+"/thumbnail", headers=headers).status_code == 400
        assert client.post(base+"/clipboard", headers=headers, json={}).status_code == 400
        assert client.post(base+"/edit", headers=headers, json={"destination":str(output)}).status_code == 400
        assert client.get("/api/removed", headers=headers).json()["total"] == 1
        assert (source / "ordinary.jpg").is_file()
