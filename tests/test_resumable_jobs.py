import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from framekeep import categories, review
from framekeep.app import create_app
from framekeep.category_model import MODEL_VERSION
from framekeep.checkpoints import Checkpoint
from framekeep.image_processing import process_image
from framekeep.jobs import RemainingTime
from framekeep.library import Library


def authenticate(app, client):
    token = client.post("/api/bootstrap", json={"ticket": app.state.access.ticket()}).json()["token"]
    client.headers["X-Framekeep-Token"] = token


def test_eta_uses_new_work_and_resets_when_progress_changes_phase():
    estimate = RemainingTime()
    assert estimate.update(80, 100, 100) is None
    assert estimate.update(81, 100, 100.5) is None
    assert estimate.update(82, 100, 101) == 9
    assert estimate.update(0, 100, 102) is None
    assert estimate.update(1, 100, 103) == 99
    assert estimate.update(0, 10, 104) is None
    assert estimate.update(10, 10, 109) is None
    resumed = RemainingTime()
    assert resumed.update(82, 100, 1000) is None
    assert resumed.update(83, 100, 1002) == 34


def wait_for_job(client, state):
    until = time.monotonic() + 15
    while time.monotonic() < until:
        job = client.get("/api/job").json()["job"]
        if job["state"] == state:
            return job
        assert job["state"] != "failed", job
        time.sleep(.03)
    pytest.fail(f"Job did not reach {state}: {job}")


def pause_after(monkeypatch, count):
    reached, release = threading.Event(), threading.Event()
    saved = []
    record = Checkpoint.record

    def hold(checkpoint, key, revision, outcome):
        record(checkpoint, key, revision, outcome)
        saved.append(key)
        if len(saved) == count:
            reached.set()
            assert release.wait(10)

    monkeypatch.setattr(Checkpoint, "record", hold)
    return reached, release, saved


def test_scan_resumes_saved_files_and_rechecks_changes(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    for index in range(5):
        Image.new("RGB", (40, 32), (index * 40, 0, 30)).save(source / f"{index}.jpg")
    data = tmp_path / "data"
    attempted = []
    scan_file = Library._scan_file

    def track(library, path, *args):
        attempted.append(path)
        return scan_file(library, path, *args)

    monkeypatch.setattr(Library, "_scan_file", track)
    reached, release, saved = pause_after(monkeypatch, 3)
    app = create_app(data)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        authenticate(app, client)
        task = client.post("/api/scan", json={"path": str(source)}).json()
        try:
            assert reached.wait(10)
            assert client.post("/api/job/pause", json={"job_id": "0" * 32}).status_code == 400
            assert client.post("/api/job/pause", json=task).status_code == 200
        finally:
            release.set()
        paused = wait_for_job(client, "paused")
        assert (paused["done"], paused["total"]) == (3, 5)
        assert client.post("/api/scan", json={"path": str(source)}).status_code == 400
        assert client.get("/api/photos").json()["total"] == 3

    unchanged, changed, deleted = map(Path, saved)
    changed_bytes = Image.new("RGB", (51, 32), "yellow")
    changed_bytes.save(changed)
    deleted.unlink()
    added = source / "new.jpg"
    Image.new("RGB", (40, 32), "purple").save(added)
    moved = tmp_path / "offline"
    source.rename(moved)
    app = create_app(data)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/api/job/resume", json=task).status_code == 401
        authenticate(app, client)
        paused = wait_for_job(client, "paused")
        assert paused["id"] == task["job_id"] and paused["done"] == 3
        assert client.post("/api/job/resume", json=task).status_code == 200
        assert "unavailable" in wait_for_job(client, "paused")["message"].lower()
        moved.rename(source)
        attempted.clear()
        assert client.post("/api/job/resume", json=task).status_code == 200
        complete = wait_for_job(client, "complete")
        assert (complete["done"], complete["total"]) == (5, 5)
        assert complete["result"]["found"] == complete["result"]["indexed"] == 5
        assert set(attempted) == set(source.iterdir()) - {unchanged}
        assert changed in attempted and added in attempted
        assert {photo["filename"] for photo in client.get("/api/photos").json()["photos"]} == {
            path.name for path in source.iterdir()
        }
        with app.state.library.connect() as db:
            assert db.execute("SELECT count(*) FROM saved_job").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM job_receipts").fetchone()[0] == 0


@pytest.mark.parametrize("operation, service, report_field", [
    ("review", review, "category_updates"), ("categories", categories, "categorized"),
])
def test_analysis_resumes_same_scope_after_app_closes(tmp_path, monkeypatch, operation, service, report_field):
    source = tmp_path / "source"
    source.mkdir()
    for index in range(4):
        Image.new("RGB", (40, 32), (index * 40, 0, 30)).save(source / f"{index}.jpg")
    data = tmp_path / "data"
    app = create_app(data)
    library = app.state.library
    library.scan(library.add(str(source)), lambda *args: None, lambda: False)
    attempted = []

    def fake_categories(path, root, size, **kwargs):
        attempted.append(path.name)
        result = process_image(path, root, size)
        pending = path.name == "0.jpg"
        result["metadata"].update(kind="Needs review" if pending else "Illustrations",
                                  kind_source="uncertain" if pending else "model", kind_score=.8,
                                  kind_suggestion="Illustrations", category_version=MODEL_VERSION)
        return result

    monkeypatch.setattr(categories.Categories, "ready", lambda self: True)
    monkeypatch.setattr(service, "process_image", fake_categories)
    reached = threading.Event()
    record = Checkpoint.record

    def hold_until_shutdown(checkpoint, key, revision, outcome):
        record(checkpoint, key, revision, outcome)
        if len(attempted) == 2:
            reached.set()
            assert app.state.jobs.stop.wait(10)

    monkeypatch.setattr(Checkpoint, "record", hold_until_shutdown)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        authenticate(app, client)
        task = client.post(f"/api/{operation}/analyze", json={"filters": {"kind": "Needs review", "sort": "name"}}).json()
        assert reached.wait(10)
    assert attempted == ["0.jpg", "1.jpg"]

    app = create_app(data)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        authenticate(app, client)
        paused = wait_for_job(client, "paused")
        assert paused["id"] == task["job_id"] and (paused["done"], paused["total"]) == (2, 4)
        offline = tmp_path / "offline"
        source.rename(offline)
        assert client.post("/api/job/resume", json=task).status_code == 200
        paused = wait_for_job(client, "paused")
        assert paused["done"] == 2 and "Reconnect the source" in paused["message"]
        offline.rename(source)
        assert client.post("/api/job/resume", json=task).status_code == 200
        complete = wait_for_job(client, "complete")
        assert complete["result"][report_field] == 4
        assert complete["done"] == complete["total"] == 4
    assert attempted == ["0.jpg", "1.jpg", "2.jpg", "3.jpg"]
