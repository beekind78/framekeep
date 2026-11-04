import hashlib
import io
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import framekeep.categories as category_service
from framekeep.app import create_app
from framekeep.categories import Categories
from framekeep.category_model import MODEL_NAME, MODEL_VERSION, PROMPTS_SHA256, decide_category
from framekeep.image_processing import process_image
from framekeep.library import SCHEMA, Library, file_hash


def test_bundled_prompt_vectors_keep_their_declared_integrity_hash():
    import framekeep.category_model as model

    bundled = Path(model.__file__).with_name("category_prompts.json")
    assert hashlib.sha256(bundled.read_bytes()).hexdigest() == PROMPTS_SHA256


def indexed_library(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name, color in (("one.jpg", "green"), ("two.jpg", "orange")):
        Image.new("RGB", (80, 50), color).save(source / name)
    library = Library(tmp_path / "data")
    library_id = library.add(str(source))
    library.scan(library_id, lambda *args: None, lambda: False)
    return library, library_id, source


def test_manual_categories_filter_counts_export_and_rescan(tmp_path):
    library, library_id, source = indexed_library(tmp_path)
    rows = library.photos({}, None)["photos"]
    first, second = rows[0]["id"], rows[1]["id"]
    originals = {p.name: file_hash(p) for p in source.iterdir()}
    library.assign_kind([first], "Illustrations")
    library.assign_kind([second], "Screenshots")
    filtered = library.photos({"kind": "Illustrations"}, None)
    assert filtered["total"] == 1 and filtered["photos"][0]["id"] == first
    assert {r["value"]: r["count"] for r in filtered["kind_counts"]} == {"Illustrations": 1, "Screenshots": 1}
    assert library.photo(first)["kind"] == "Illustrations"
    assert library.photo(first)["automatic_kind"] == "Needs review"
    with library.connect() as db:
        db.execute("UPDATE photos SET policy_version=0")
    library.scan(library_id, lambda *args: None, lambda: False)
    assert Library(library.data).photo(first)["kind"] == "Illustrations"
    output = tmp_path / "output"
    output.mkdir()
    plan = library.plan(str(output), "kind", {}, False)
    assert {Path(p).parts[0] for p in plan["sample"]} == {"Illustrations", "Screenshots"}
    result = library.export(library.consume_plan(plan["token"]), lambda *args: None, lambda: False)
    assert result["copied"] == 2 and result["error_count"] == 0
    dated = library.plan(str(output), "date_kind", {"kind": "Illustrations"}, False)
    assert Path(dated["sample"][0]).parts[2] == "Illustrations"
    assert originals == {p.name: file_hash(p) for p in source.iterdir()}
    library.assign_kind([first], None)
    assert library.photo(first)["kind"] == "Needs review"


def test_manual_category_is_reset_for_replaced_content_and_bulk_is_atomic(tmp_path):
    library, library_id, _source = indexed_library(tmp_path)
    row = library.photos({}, None)["photos"][0]
    library.assign_kind([row["id"]], "Photos")
    with pytest.raises(ValueError, match="unavailable"):
        library.assign_kind([row["id"], 99999], "Illustrations")
    assert library.photo(row["id"])["kind"] == "Photos"
    Image.new("RGB", (80, 50), "purple").save(Path(row["path"]))
    library.scan(library_id, lambda *args: None, lambda: False)
    assert library.photo(row["id"])["kind_override"] is None
    assert library.photo(row["id"])["kind"] == "Needs review"


def test_detection_keeps_manual_categories_and_skips_current_predictions(tmp_path, monkeypatch):
    library, _, _ = indexed_library(tmp_path)
    rows = library.photos({}, None)["photos"]
    library.assign_kind([rows[0]["id"]], "Illustrations")
    service = Categories(library)
    monkeypatch.setattr(service, "ready", lambda: True)
    calls = []

    def infer(path, root, size, **kwargs):
        calls.append(path)
        info = path.stat()
        return {
            "metadata": {
                "kind": "Screenshots",
                "kind_source": "model",
                "kind_score": 0.9,
                "kind_suggestion": "Screenshots",
                "category_version": MODEL_VERSION,
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
            }
        }

    monkeypatch.setattr(category_service, "process_image", infer)
    result = service.analyze({}, lambda *args: None, lambda: False)
    assert result["categorized"] == 1 and result["kept_manual"] == 1
    assert result["category_counts"] == {"Illustrations": 1, "Screenshots": 1}
    assert len(calls) == 1
    repeated = service.analyze({}, lambda *args: None, lambda: False)
    assert repeated["categorized"] == 0 and repeated["already_classified"] == 1
    assert len(calls) == 1


def test_uncertain_or_invalid_predictions_are_not_confident_labels():
    labels = ["Photos", "Screenshots", "Illustrations"]
    result = decide_category([0.25, 0.251, 0.24], labels)
    assert result["kind"] == "Needs review" and result["kind_suggestion"] == "Screenshots"
    assert decide_category([0.1, 0.05, 0.03], labels)["kind"] == "Needs review"
    assert decide_category([0.32, 0.2, 0.22], labels)["kind"] == "Photos"
    with pytest.raises(ValueError):
        decide_category([float("nan"), 0.2, 0.3], labels)


def test_legacy_categories_migrate_without_changing_dates_favorites_or_tags(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with sqlite3.connect(data / "library.sqlite3") as db:
        db.executescript(SCHEMA)
        db.execute(
            "INSERT INTO libraries(path,name,demo) VALUES (?,?,0)", (str(tmp_path / "source"), "source")
        )
        db.execute(
            "INSERT INTO photos(library_id,path,filename,extension,size,mtime_ns,captured_at,date_source,width,height,camera,kind,sha256,favorite,tags) "
            "VALUES (1,?,?, 'JPG',1,1,'2020-01-02T03:04:05','Camera metadata',10,10,'','Photos','hash',1,'holiday')",
            (str(tmp_path / "source" / "old.jpg"), "old.jpg"),
        )
    library = Library(data)
    row = library.photos({}, None)["photos"][0]
    assert row["kind"] == "Needs review" and row["kind_override"] is None
    assert row["favorite"] == 1 and row["tags"] == "holiday"
    assert row["captured_at"] == "2020-01-02T03:04:05"
    library.assign_kind([row["id"]], "Illustrations")
    assert Library(data).photo(row["id"])["kind"] == "Illustrations"


def test_filename_hint_does_not_classify_an_entire_backup_folder(tmp_path):
    source = tmp_path / "screenshots and photos"
    source.mkdir()
    for name in ("IMG_1234.png", "Screenshot_1234.png"):
        Image.new("RGB", (117, 253), "green").save(source / name)
    library = Library(tmp_path / "data")
    library.scan(library.add(str(source)), lambda *args: None, lambda: False)
    rows = {row["filename"]: row for row in library.photos({}, None)["photos"]}
    assert rows["IMG_1234.png"]["kind"] == "Needs review"
    assert rows["Screenshot_1234.png"]["kind"] == "Screenshots"


def test_camera_evidence_does_not_require_or_get_overridden_by_visual_model(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "IMG_0001.jpg"
    exif = Image.Exif()
    exif[271], exif[272] = "Apple", "iPhone"
    Image.new("RGB", (80, 50), "green").save(path, exif=exif)
    broken = tmp_path / "models"
    broken.mkdir()
    (broken / MODEL_NAME).write_bytes(b"untrusted model")
    result = process_image(path, source, 480, category_models=broken)["metadata"]
    assert result["kind"] == "Photos" and result["kind_source"] == "camera"
    assert "category_error" not in result


def test_camera_evidence_replaces_an_older_incorrect_model_guess(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    exif = Image.Exif()
    exif[271], exif[272] = "Apple", "iPhone"
    Image.new("RGB", (80, 50), "green").save(source / "IMG_0001.jpg", exif=exif)
    library = Library(tmp_path / "data")
    library.scan(library.add(str(source)), lambda *args: None, lambda: False)
    row = library.photos({}, None)["photos"][0]
    with library.connect() as db:
        db.execute("UPDATE photos SET kind='Screenshots',kind_source='model',category_version=1")
    service = Categories(library)
    monkeypatch.setattr(service, "ready", lambda: True)
    result = service.analyze({}, lambda *args: None, lambda: False)
    assert result["categorized"] == 1
    assert library.photo(row["id"])["kind"] == "Photos"
    assert library.photo(row["id"])["kind_source"] == "camera"


def test_bad_model_preserves_readable_photo_and_requires_repair(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    photo = source / "one.jpg"
    Image.new("RGB", (80, 50), "green").save(photo)
    library = Library(tmp_path / "data")
    service = Categories(library)
    service.directory.mkdir(parents=True)
    service.model.write_bytes(b"damaged model")
    assert not service.ready()
    result = process_image(photo, source, 480, category_models=service.directory)
    assert result["jpeg"].startswith(b"\xff\xd8\xff")
    assert result["metadata"]["kind"] == "Needs review"
    assert "integrity" in result["metadata"]["category_error"]


@pytest.mark.parametrize(
    "payload,expected_error", [(b"too long model", "expected size"), (b"bad!", "checksum")]
)
def test_model_download_rejects_untrusted_bytes(tmp_path, monkeypatch, payload, expected_error):
    service = Categories(Library(tmp_path / "data"))
    monkeypatch.setattr(category_service, "MODEL_BYTES", 4)
    monkeypatch.setattr(category_service, "MODEL_SHA256", hashlib.sha256(b"good").hexdigest())
    monkeypatch.setattr(
        category_service.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(payload)
    )
    with pytest.raises(ValueError, match=expected_error):
        service.install(lambda *args: None, lambda: False)
    assert not service.model.exists() and not service.model.with_suffix(".download").exists()


def test_category_api_auth_validation_and_manual_reset(tmp_path):
    app = create_app(tmp_path / "data")
    source = tmp_path / "source"
    source.mkdir()
    Image.new("RGB", (80, 50), "green").save(source / "one.jpg")
    library = app.state.library
    library.scan(library.add(str(source)), lambda *args: None, lambda: False)
    photo_id = library.photos({}, None)["photos"][0]["id"]
    with TestClient(app, base_url="http://127.0.0.1") as client:
        payload = {"ids": [photo_id], "kind": "Illustrations"}
        assert client.post("/api/categories/assign", json=payload).status_code == 401
        assert client.post("/api/categories/analyze", json={}).status_code == 401
        assert client.post("/api/categories/install", json={}).status_code == 401
        token = client.post("/api/bootstrap", json={"ticket": app.state.access.ticket()}).json()["token"]
        headers = {"X-Framekeep-Token": token}
        assert (
            client.post(
                "/api/categories/assign", json=payload, headers={**headers, "Origin": "https://other.example"}
            ).status_code
            == 403
        )
        assert client.post("/api/categories/assign", json=payload, headers=headers).status_code == 200
        assert client.get("/api/photos?kind=Illustrations", headers=headers).json()["total"] == 1
        assert (
            client.patch(f"/api/photos/{photo_id}", json={"kind": "invalid"}, headers=headers).status_code
            == 422
        )
        assert (
            client.patch(f"/api/photos/{photo_id}", json={"kind": None}, headers=headers).status_code == 200
        )
        assert library.photo(photo_id)["kind_override"] is None
        assert client.post("/api/categories/analyze", json={}, headers=headers).status_code == 400


@pytest.mark.parametrize(
    "sample,expected",
    [("camera.jpg", "Photos"), ("interface.jpg", "Screenshots"), ("drawing.jpg", "Illustrations")],
)
def test_real_visual_model_in_bounded_worker_when_assets_available(sample, expected, tmp_path):
    assets = os.environ.get("FRAMEKEEP_CATEGORY_TEST_ASSETS")
    if not assets:
        pytest.skip("Set FRAMEKEEP_CATEGORY_TEST_ASSETS for offline model integration checks.")
    root = Path(assets)
    assert (root / "runtime" / MODEL_NAME).is_file()
    # Remove filename/EXIF hints so this test exercises visual inference itself.
    clean = process_image(root / "validation" / sample, root / "validation", 480)["jpeg"]
    path = tmp_path / "image.jpg"
    path.write_bytes(clean)
    result = process_image(path, tmp_path, 480, category_models=root / "runtime")
    assert result["metadata"]["kind"] == expected
    assert result["metadata"]["kind_source"] == "model"
    assert "category_error" not in result["metadata"]
