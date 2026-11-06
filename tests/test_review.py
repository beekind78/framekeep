import os
import shutil
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

import framekeep.review as review_module
import framekeep.visual_similarity as similarity
from framekeep.app import create_app
from framekeep.category_model import MODEL_VERSION
from framekeep.library import Library, file_hash
from framekeep.review import Review
from tests.media_fixtures import video_bytes


def scene():
    pixels = np.zeros((600, 900, 3), dtype=np.uint8)
    pixels[:, :, 0] = np.linspace(35, 190, 600, dtype=np.uint8)[:, None]
    pixels[:, :, 1] = np.linspace(50, 220, 900, dtype=np.uint8)[None, :]
    pixels[:, :, 2] = 130
    image = Image.fromarray(pixels)
    draw = ImageDraw.Draw(image)
    draw.ellipse((80, 70, 330, 320), fill=(240, 190, 50))
    draw.polygon([(0, 590), (450, 140), (899, 590)], fill=(25, 90, 65))
    draw.rectangle((620, 300, 850, 530), fill=(150, 50, 80))
    return image


@pytest.fixture
def collection(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    image = scene()
    image.save(source / "original.png")
    image.resize((360, 240)).save(source / "smaller.jpg", quality=70)
    shutil.copy2(source / "original.png", source / "exact.png")
    Image.new("RGB", (300, 200), "white").save(source / "plain.png")
    image.transpose(Image.Transpose.FLIP_LEFT_RIGHT).save(source / "different.png")
    (source / "video.MOV").write_bytes(video_bytes())
    library = Library(tmp_path / "data")
    library_id = library.add(str(source))
    result = library.scan(library_id, lambda *args: None, lambda: False)
    assert not result["errors"]
    service = Review(library, SimpleNamespace(ready=lambda: False, directory=tmp_path / "models"))
    return library, service, source, library_id


def test_worker_fingerprints_find_resized_copies_but_not_exact_plain_or_other_images(collection):
    library, service, source, _ = collection
    before = {p.name: file_hash(p) for p in source.iterdir()}
    result = service.similar({})
    names = {p["id"]: p["filename"] for p in result["photos"]}
    assert result["image_total"] == result["analyzed"] == 5
    assert result["total_pairs"] == 1
    pair = result["pairs"][0]
    assert {names[pair["left"]], names[pair["right"]]} in (
        {"original.png", "smaller.jpg"}, {"exact.png", "smaller.jpg"}
    )
    assert library.summary()["duplicates"]["extra_copies"] == 1
    assert {p.name: file_hash(p) for p in source.iterdir()} == before
    original = next(p for p in library.photos({}, None)["photos"] if p["filename"] == "original.png")
    matches = service.matches(original["id"])
    assert matches["exact_total"] == 1 and matches["similar_total"] == 1
    assert matches["exact"][0]["filename"] == "exact.png"
    assert matches["similar"][0]["filename"] == "smaller.jpg"


def test_fingerprint_results_respect_filters_manual_edits_missing_and_replaced_content(collection):
    library, service, _, library_id = collection
    pair = service.similar({"library": library_id})["pairs"][0]
    library.assign_kind([pair["left"]], "Screenshots")
    assert next(p for p in service.similar({})["photos"] if p["id"] == pair["left"])["kind"] == "Screenshots"
    assert service.similar({"kind": "Screenshots"})["total_pairs"] == 0
    assert service.similar({"q": "smaller"})["total_pairs"] == 0
    assert service.similar({"library": 999})["image_total"] == 0
    with library.connect() as db:
        db.execute("UPDATE photos SET sha256='replaced' WHERE id=?", (pair["right"],))
    assert service.similar({})["total_pairs"] == 0
    assert service.similar({})["analyzed"] == 4
    with library.connect() as db:
        db.execute("UPDATE photos SET missing=1 WHERE filename='different.png'")
    assert service.similar({})["image_total"] == 4


def test_direct_pairs_do_not_transitively_claim_unmatched_images_and_limits_are_visible(monkeypatch):
    def entry(photo_id, code):
        return {"id": photo_id, "sha256": str(photo_id), "width": 100, "height": 100,
                "phash": f"{code:016x}", "dhash": "0" * 16,
                "aspect": 1, "color": [100, 100, 100], "texture": 30}

    rows = [entry(1, 0), entry(2, 0x3F), entry(3, 0xFFF)]
    pairs, limited = similarity.find_pairs(rows)
    assert not limited
    assert {(p["left"], p["right"]) for p in pairs} == {(1, 2), (2, 3)}
    monkeypatch.setattr(similarity, "MAX_COMPARISONS", 1)
    assert similarity.find_pairs(rows)[1]
    assert similarity.find_pairs(rows, cancelled=lambda: True)[1]


def test_review_refreshes_older_uncertain_ai_keeps_manual_categories_and_skips_videos(collection, monkeypatch):
    library, service, source, _ = collection
    rows = {p["filename"]: p for p in library.photos({}, None)["photos"]}
    library.assign_kind([rows["original.png"]["id"]], "Illustrations")
    with library.connect() as db:
        db.execute("UPDATE photos SET kind='Photos',kind_source='model',category_version=? WHERE filename!='smaller.jpg' AND kind_override IS NULL AND media_type='image'", (MODEL_VERSION,))
        db.execute("UPDATE photos SET kind='Needs review',kind_source='uncertain',kind_suggestion='Photos',"
                   "category_version=? WHERE filename='smaller.jpg'", (MODEL_VERSION - 1,))
    service.categories.ready = lambda: True
    calls = []

    def infer(path, root, size, **kwargs):
        calls.append(path.name)
        photo = rows[path.name]
        return {"metadata": {"size": photo["size"], "mtime_ns": photo["mtime_ns"],
                "kind": "Screenshots", "kind_source": "model", "kind_score": 0.8,
                "kind_suggestion": "Screenshots", "category_version": MODEL_VERSION},
                "fingerprint": similarity.fingerprint(scene())}

    monkeypatch.setattr(review_module, "process_image", infer)
    before = {p.name: file_hash(p) for p in source.iterdir()}
    result = service.analyze({}, lambda *args: None, lambda: False)
    assert calls == ["smaller.jpg"]
    assert result["category_updates"] == 1 and result["kept_manual"] == 1
    assert library.photo(rows["original.png"]["id"])["kind"] == "Illustrations"
    assert library.photo(rows["smaller.jpg"]["id"])["kind"] == "Screenshots"
    assert {p.name: file_hash(p) for p in source.iterdir()} == before
    assert service.analyze({}, lambda *args: None, lambda: False)["reviewed"] == 0


def test_review_rejects_changed_source_and_cancellation_keeps_existing_index(collection, monkeypatch):
    library, service, source, _ = collection
    photo = next(p for p in library.photos({}, None)["photos"] if p["filename"] == "smaller.jpg")
    with library.connect() as db:
        db.execute("DELETE FROM visual_fingerprints WHERE photo_id=?", (photo["id"],))
    target = source / photo["filename"]
    contents = target.read_bytes()
    target.write_bytes(contents[:-1] + bytes([contents[-1] ^ 1]))
    os.utime(target, ns=(target.stat().st_atime_ns, photo["mtime_ns"]))
    monkeypatch.setattr(review_module, "process_image", lambda *args, **kwargs: {
        "metadata": {"size": photo["size"], "mtime_ns": photo["mtime_ns"]},
        "fingerprint": similarity.fingerprint(scene()),
    })
    result = service.analyze({"ids": [photo["id"]]}, lambda *args: None, lambda: False)
    assert result["reviewed"] == 0 and result["error_count"] == 1
    assert "contents changed" in result["errors"][0]["reason"]
    assert service.analyze({}, lambda *args: None, lambda: True)["cancelled"]


def test_review_and_match_api_require_local_auth_and_support_pagination(collection):
    library, _, _, _ = collection
    app = create_app(library.data)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for path in ("/api/duplicates/similar", "/api/photos/1/matches"):
            assert client.get(path).status_code == 401
        assert client.post("/api/review/analyze", json={}).status_code == 401
        token = client.post("/api/bootstrap", json={"ticket": app.state.access.ticket()}).json()["token"]
        headers = {"X-Framekeep-Token": token}
        assert client.get("/api/duplicates/similar", headers={**headers, "Origin": "https://foreign.example"}).status_code == 403
        assert client.post("/api/review/analyze", json={}, headers={**headers, "Origin": "https://foreign.example"}).status_code == 403
        data = client.get("/api/duplicates/similar?limit=1", headers=headers).json()
        assert data["total_pairs"] == 1 and len(data["pairs"]) == 1
        assert client.get("/api/duplicates/similar?offset=1", headers=headers).json()["pairs"] == []
        assert client.get("/api/duplicates/similar?limit=41", headers=headers).status_code == 422
        assert client.get("/api/photos/9999/matches", headers=headers).status_code == 400
        photo_id = data["pairs"][0]["left"]
        assert client.get(f"/api/photos/{photo_id}/matches", headers=headers).status_code == 200


def test_perceptual_hashes_are_stable_for_resizing_and_reject_flat_or_changed_aspect():
    image = scene()
    first = {**similarity.fingerprint(image), "sha256": "first"}
    second = {**similarity.fingerprint(image.resize((300, 200))), "sha256": "second"}
    assert similarity.compare(first, second)
    assert not similarity.compare(first, {**second, "aspect": 1})
    assert not similarity.compare(first, {**second, "color": [255, 255, 255]})
    assert not similarity.compare(first, {**second, "sha256": "first"})
    flat = {**similarity.fingerprint(Image.new("RGB", (100, 100), "white")), "sha256": "flat"}
    assert not similarity.compare(flat, {**flat, "sha256": "flat-again"})
