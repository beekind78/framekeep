import os
import time
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from framekeep import editing
from framekeep.app import create_app
from framekeep.image_processing import TruncatedJPEGError, process_image
from framekeep.library import Library


def completed_job(client, headers, job_id):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = client.get("/api/job", headers=headers).json()["job"]
        if job["id"] == job_id and job["state"] not in ("queued", "running"):
            assert job["state"] == "complete", job
            return job["result"]
        time.sleep(.025)
    pytest.fail("The recovery or scan job did not finish.")


def test_recovery_writes_readable_copies_only_for_unchanged_scan_failures(tmp_path):
    source, output = tmp_path / "source", tmp_path / "output"
    source.mkdir()
    output.mkdir()
    original, changed = source / "partial.jpg", source / "changed.jpg"
    exif = Image.Exif()
    exif[274], exif[306] = 6, "2020:04:02 09:10:11"
    Image.effect_noise((320, 240), 60).convert("RGB").save(original, exif=exif)
    partial = original.read_bytes()[: original.stat().st_size // 2]
    original.write_bytes(partial)
    changed.write_bytes(partial)
    (source / "unreadable.jpg").write_bytes(b"not a photo")
    existing = output / "partial-recovered.jpg"
    existing.write_bytes(b"existing file")

    app = create_app(tmp_path / "data")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.post("/api/bootstrap", json={"ticket": app.state.access.ticket()}).json()["token"]
        headers = {"X-Framekeep-Token": token}
        scan = client.post("/api/scan", headers=headers, json={"path": str(source)}).json()
        report = completed_job(client, headers, scan["job_id"])
        assert report["error_count"] == 3 and report["recoverable_jpegs"] == 2
        assert "jpeg_recovery" not in report

        stat = changed.stat()
        replacement = partial[:-1] + bytes([partial[-1] ^ 1])
        changed.write_bytes(replacement)
        os.utime(changed, ns=(stat.st_atime_ns, stat.st_mtime_ns))

        endpoint = "/api/recovery/jpeg"
        body = {"scan_id": scan["job_id"], "destination": str(output)}
        assert client.post(endpoint, json=body).status_code == 401
        assert client.post(endpoint, headers={**headers, "Origin": "https://other.example"}, json=body).status_code == 403
        assert client.post(endpoint, headers=headers, json={**body, "destination": str(source)}).status_code == 400
        job = client.post(endpoint, headers=headers, json=body).json()
        result = completed_job(client, headers, job["job_id"])
        assert result["recovered"] == 1 and result["error_count"] == 1
        assert result["errors"][0]["file"] == changed.name
        assert "contents changed" in result["errors"][0]["reason"]
        assert client.post(endpoint, headers=headers, json=body).status_code == 400

    folder = Path(result["destination"])
    recovered = folder / "partial-recovered.jpg"
    assert folder.parent == output and result["indexed_recovered"] == 1
    assert {path.name for path in folder.iterdir()} == {recovered.name}
    assert existing.read_bytes() == b"existing file"
    with Image.open(recovered, formats=("JPEG",)) as image:
        image.load()
        assert image.size == (240, 320)
        assert image.getexif()[306] == "2020:04:02 09:10:11"
    assert original.read_bytes() == partial and changed.read_bytes() == replacement
    with pytest.raises(TruncatedJPEGError):
        process_image(original, source, 480)


def test_recovery_uses_verified_larger_copy_of_rotated_partial_jpeg(tmp_path, monkeypatch):
    source, profile = tmp_path / "source", tmp_path / "profile"
    source.mkdir()
    profile.mkdir()
    monkeypatch.setattr(Path, "home", lambda: profile)
    output = profile / "Framekeep Recovered"
    full = Image.new("RGB", (900, 600), "powderblue")
    draw = ImageDraw.Draw(full)
    draw.rectangle((30, 240, 800, 590), fill="darkgreen")
    draw.ellipse((155, 90, 460, 435), fill="orange")
    draw.polygon([(530, 60), (840, 540), (620, 520)], fill="maroon")
    large = source / "clear.MOV"
    full.save(large, "JPEG", quality=95)
    small = source / "partial.jpg"
    full.resize((160, 106), Image.Resampling.LANCZOS).transpose(Image.Transpose.ROTATE_90).save(small, quality=93)
    partial = small.read_bytes()[:-32]
    small.write_bytes(partial)
    exact = large.read_bytes()

    def unexpected_download(*args):
        pytest.fail("A verified larger copy should be used before downloading an AI model.")

    monkeypatch.setattr(editing, "recovery_model", unexpected_download)
    app = create_app(tmp_path / "data")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.post("/api/bootstrap", json={"ticket": app.state.access.ticket()}).json()["token"]
        headers = {"X-Framekeep-Token": token}
        assert client.get("/api/session", headers=headers).json()["recovery_output"] == str(output)
        scan = client.post("/api/scan", headers=headers, json={"path": str(source)}).json()
        assert completed_job(client, headers, scan["job_id"])["recoverable_jpegs"] == 1
        save = {"scan_id": scan["job_id"], "destination": str(output), "prefer_larger": True, "enhance": True}
        assert client.post("/api/recovery/jpeg", headers=headers, json=save).status_code == 400
        review = client.post("/api/recovery/jpeg/review", headers=headers, json={"scan_id": scan["job_id"]}).json()
        candidates = completed_job(client, headers, review["job_id"])["review_candidates"]
        assert len(candidates) == 1 and candidates[0]["source"] == large.name
        index = candidates[0]["index"]
        preview = f"/api/recovery/jpeg/review/{review['job_id']}/preview/{index}"
        assert client.get(preview).status_code == 401
        with Image.open(BytesIO(client.get(preview, headers=headers).content)) as image:
            assert image.size == (160, 106)
        assert not output.exists()
        save.update(scan_id=review["job_id"], approved_matches=[index])
        job = client.post("/api/recovery/jpeg", headers=headers, json=save).json()
        result = completed_job(client, headers, job["job_id"])
        assert result["recovered"] == result["matched"] == 1
        assert Path(result["destination"]).parent == output
        assert result["enhanced"] == 0
        assert result["matches"] == [{"file": small.name, "source": large.name, "copy": "partial-library-match.jpg",
                                      "width": 900, "height": 600}]
        gallery = client.get(f"/api/photos?library={result['recovered_library']}", headers=headers).json()
        assert gallery["total"] == 1 and gallery["photos"][0]["filename"] == "partial-library-match.jpg"
        assert (Path(result["destination"]) / "partial-library-match.jpg").read_bytes() == large.read_bytes() == exact

        scan = client.post("/api/scan", headers=headers, json={"path": str(source)}).json()
        completed_job(client, headers, scan["job_id"])
        review = client.post("/api/recovery/jpeg/review", headers=headers, json={"scan_id": scan["job_id"]}).json()
        completed_job(client, headers, review["job_id"])
        save.update(scan_id=review["job_id"], approved_matches=[], enhance=False)
        del save["destination"]
        job = client.post("/api/recovery/jpeg", headers=headers, json=save).json()
        fallback = completed_job(client, headers, job["job_id"])
        assert fallback["matched"] == 0 and fallback["indexed_recovered"] == 1
        assert Path(fallback["destination"]).parent == output
        assert fallback["destination"] != result["destination"]
        assert [path.name for path in Path(fallback["destination"]).iterdir()] == ["partial-recovered.jpg"]
    assert small.read_bytes() == partial


def test_default_recovery_folder_cannot_be_created_inside_a_source(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(Path, "home", lambda: profile)
    library = Library(tmp_path / "data")
    library.add(str(profile))

    with pytest.raises(ValueError, match="separate from your source libraries"):
        library.recovery_output_folder("")
    assert not (profile / "Framekeep Recovered").exists()


def test_repair_indexed_jpeg_previews_and_saves_new_copies(tmp_path, monkeypatch):
    source, profile = tmp_path / "source", tmp_path / "profile"
    source.mkdir()
    profile.mkdir()
    monkeypatch.setattr(Path, "home", lambda: profile)
    output = profile / "Framekeep Recovered"
    complete = Image.new("RGB", (900, 600), "powderblue")
    draw = ImageDraw.Draw(complete)
    draw.ellipse((30, 15, 440, 580), fill="orange")
    draw.polygon([(490, 10), (850, 480), (620, 520)], fill="darkgreen")
    full, damaged = source / "scene.jpg", source / "scene-recovered.jpeg"
    complete.save(full, quality=95)
    complete.paste((128, 128, 128), (0, 192, 900, 600))
    complete.save(damaged, quality=94)
    original, partial = full.read_bytes(), damaged.read_bytes()

    app = create_app(tmp_path / "data")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.post("/api/bootstrap", json={"ticket": app.state.access.ticket()}).json()["token"]
        headers = {"X-Framekeep-Token": token}
        scan = client.post("/api/scan", headers=headers, json={"path": str(source)}).json()
        assert completed_job(client, headers, scan["job_id"])["found"] == 2
        photos = client.get("/api/photos", headers=headers).json()["photos"]
        rows = {photo["filename"]: photo["id"] for photo in photos}
        review_url = f"/api/photos/{rows[damaged.name]}/repair"
        assert client.post(review_url).status_code == 401
        review = client.post(review_url, headers=headers).json()
        result = completed_job(client, headers, review["job_id"])
        assert result["repair_review"]["has_crop"]
        assert result["repair_review"]["match"]["id"] == rows[full.name]
        assert "jpeg_repair" not in result
        assert not output.exists()
        save_url = f"/api/recovery/repair/{review['job_id']}"
        assert client.get(save_url + "/preview").status_code == 401
        response = client.get(save_url + "/preview", headers=headers)
        assert response.status_code == 200
        with Image.open(BytesIO(response.content)) as preview:
            assert preview.width == 480 and 95 <= preview.height <= 104
        assert client.post(save_url, headers=headers, json={"choice": "crop", "destination": str(source)}).status_code == 400

        save = client.post(save_url, headers=headers, json={"choice": "match"}).json()
        result = completed_job(client, headers, save["job_id"])
        folder = Path(result["destination"])
        assert folder.parent == output and result["indexed_recovered"] == result["matched"] == 1
        assert next(folder.iterdir()).read_bytes() == original
        assert client.post(save_url, headers=headers, json={"choice": "crop"}).status_code == 400

        review = client.post(review_url, headers=headers).json()
        completed_job(client, headers, review["job_id"])
        save = client.post(f"/api/recovery/repair/{review['job_id']}", headers=headers, json={"choice": "crop"}).json()
        result = completed_job(client, headers, save["job_id"])
        assert result["indexed_recovered"] == result["trimmed"] == 1
        gallery = client.get(f"/api/photos?library={result['recovered_library']}", headers=headers).json()
        assert gallery["total"] == 1
        repaired = Path(gallery["photos"][0]["path"])
        with Image.open(repaired) as image:
            image.load()
            assert image.width == 900 and 180 <= image.height <= 194

        review = client.post(f"/api/photos/{rows[full.name]}/repair", headers=headers).json()
        result = completed_job(client, headers, review["job_id"])["repair_review"]
        assert not result["has_crop"] and result["match"] is None
    assert full.read_bytes() == original and damaged.read_bytes() == partial


@pytest.mark.parametrize("edge", [(0, 0, 350, 300), (150, 0, 500, 300), (0, 0, 500, 200), (0, 100, 500, 300)])
def test_repair_detects_large_gray_padding_on_each_edge(edge):
    from framekeep.recovery_match import gray_edge_crop

    image = Image.new("RGB", (500, 300), "seagreen")
    image.paste((129, 129, 129), edge)
    box = gray_edge_crop(image)
    assert box is not None
    assert set(image.crop(box).get_flattened_data()) == {(46, 139, 87)}
