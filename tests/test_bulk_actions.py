import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from framekeep import clipboard
from framekeep.app import create_app
from framekeep.library import Library, file_hash
from tests.test_live_photos import live_movie, still_file


@pytest.fixture
def collection(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    still_file(root / "live.JPG")
    (root / "motion.MOV").write_bytes(live_movie())
    for name, color in (("one.jpg","blue"),("two.jpg","green")):
        Image.new("RGB",(80,60),color).save(root / name)
    library = Library(tmp_path / "data")
    source_id = library.add(str(root))
    library.scan(source_id, lambda *args:None, lambda:False)
    rows = {row["filename"]:row for row in library.photos({},None)["photos"]}
    return library, root, source_id, rows


def test_bulk_remove_deduplicates_live_resources_and_can_restore_each_item(collection):
    library, root, source_id, rows = collection
    hashes = {path.name:file_hash(path) for path in root.iterdir()}
    ordinary, live, motion = (rows[name]["id"] for name in ("one.jpg","live.JPG","motion.MOV"))
    library.update_photo(ordinary,True,"saved selection tag")
    library.assign_kind([ordinary],"Illustrations")
    selected = [ordinary,live,motion,ordinary]
    originals = library.original_files_many(selected)
    assert len(originals) == len({row["id"] for row in originals}) == 3
    result = library.remove_photos(selected)
    assert result == {"removed":3,"items":2}
    assert library.photos({},None)["total"] == 1
    assert library.removed_items()["total"] == 2
    library.scan(source_id,lambda *args:None,lambda:False)
    assert library.photos({},None)["total"] == 1
    for row in library.removed_items()["photos"]:
        library.scan(library.restore_photo(row["id"]),lambda *args:None,lambda:False)
    assert library.summary()["total"] == 3 and library.summary()["live_total"] == 1
    restored = library.photo(ordinary)
    assert restored["favorite"] and restored["tags"] == "saved selection tag" and restored["kind_override"] == "Illustrations"
    assert hashes == {path.name:file_hash(path) for path in root.iterdir()}


def test_invalid_or_stale_selection_cannot_partially_remove_valid_items(collection):
    library, _, _, rows = collection
    first, second = rows["one.jpg"]["id"], rows["two.jpg"]["id"]
    with pytest.raises(ValueError,match="unavailable"):
        library.remove_photos([first,999999])
    assert library.photo(first) and not library.removed_items()["total"]
    library.remove_photo(second)
    with pytest.raises(ValueError,match="unavailable"):
        library.remove_photos([first,second])
    assert library.photo(first) and library.removed_items()["total"] == 1


def test_batch_api_copies_one_clipboard_payload_and_enforces_access_and_limits(collection, monkeypatch):
    library, root, _, rows = collection
    files, texts = [], []
    monkeypatch.setattr(clipboard,"copy_files",lambda paths:files.append(paths))
    monkeypatch.setattr(clipboard,"copy_text",lambda text:texts.append(text))
    app = create_app(library.data)
    first, live, motion = (rows[name]["id"] for name in ("one.jpg","live.JPG","motion.MOV"))
    ids = [first,live,motion,first]
    with TestClient(app,base_url="http://127.0.0.1") as client:
        for path in ("/api/photos/clipboard","/api/photos/remove"):
            assert client.post(path,json={"ids":ids}).status_code == 401
        token = client.post("/api/bootstrap",json={"ticket":app.state.access.ticket()}).json()["token"]
        headers = {"X-Framekeep-Token":token}
        for path in ("/api/photos/clipboard","/api/photos/remove"):
            assert client.post(path,json={"ids":ids},headers={**headers,"Origin":"https://outside.example"}).status_code == 403
            for invalid in ([],[-1],["not-an-id"],[first]*5001):
                assert client.post(path,json={"ids":invalid},headers=headers).status_code == 422
        response = client.post("/api/photos/clipboard",json={"ids":ids},headers=headers)
        assert response.json() == {"copied":3} and len(files) == 1
        assert {Path(path).name for path in files[0]} == {"one.jpg","live.JPG","motion.MOV"}
        response = client.post("/api/photos/clipboard",json={"ids":[first,live,first],"mode":"path"},headers=headers)
        assert response.json() == {"copied":2} and texts == [str(root/"one.jpg")+"\r\n"+str(root/"live.JPG")]
        assert client.post("/api/photos/remove",json={"ids":[first,999999]},headers=headers).status_code == 400
        assert library.photo(first)
        response = client.post("/api/photos/remove",json={"ids":ids},headers=headers)
        assert response.json() == {"removed":3,"items":2}
        assert client.get("/api/removed",headers=headers).json()["total"] == 2


def test_bulk_actions_reject_changed_sources_and_active_catalog_tasks(collection, monkeypatch):
    library, root, _, rows = collection
    writes = []
    monkeypatch.setattr(clipboard,"copy_files",lambda paths:writes.append(paths))
    app = create_app(library.data)
    ids = [rows[name]["id"] for name in ("one.jpg","two.jpg")]
    with TestClient(app,base_url="http://127.0.0.1") as client:
        token = client.post("/api/bootstrap",json={"ticket":app.state.access.ticket()}).json()["token"]
        headers = {"X-Framekeep-Token":token}
        (root/"two.jpg").write_bytes(b"changed")
        assert client.post("/api/photos/clipboard",json={"ids":ids},headers=headers).status_code == 400
        assert writes == []
        app.state.jobs.submit("busy task",lambda progress,cancelled: time.sleep(.5))
        assert client.post("/api/photos/remove",json={"ids":ids},headers=headers).status_code == 400
        assert library.removed_items()["total"] == 0
