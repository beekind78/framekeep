import base64
import threading
import time
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from framekeep import people
from framekeep.app import create_app
from framekeep.checkpoints import Checkpoint


def authenticate(app, client):
    token = client.post("/api/bootstrap", json={"ticket": app.state.access.ticket()}).json()["token"]
    client.headers["X-Framekeep-Token"] = token


def wait_for_job(client, state):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = client.get("/api/job").json()["job"]
        if job["state"] == state:
            return job
        assert job["state"] != "failed", job
        time.sleep(.03)
    raise AssertionError(f"Job did not reach {state}: {job}")


def fake_detection(monkeypatch):
    monkeypatch.setattr(people, "MODELS", {})
    calls = []
    image = BytesIO()
    Image.new("RGB", (112, 112), "green").save(image, "JPEG")
    face = {"embedding": [1.0] + [0.0] * 127, "jpeg": base64.b64encode(image.getvalue()).decode()}

    def detect(path, root, models, *, expected=None):
        assert expected
        calls.append(path.name)
        return [face]

    monkeypatch.setattr(people, "process_faces", detect)
    return calls


def index(library, path):
    source = library.add(str(path))
    library.scan(source, lambda *args: None, lambda: False)
    return source


def test_people_resume_original_section_and_reuse_identical_faces(tmp_path, monkeypatch):
    data, source, other = [tmp_path / name for name in ("data", "source", "other")]
    source.mkdir()
    other.mkdir()
    for name, color in (("first.jpg", "green"), ("a.jpg", "blue"), ("old.jpg", "gray"), ("art.jpg", "orange")):
        Image.new("RGB", (130, 100), color).save(source / name)
    (source / "b.jpg").write_bytes((source / "a.jpg").read_bytes())
    Image.new("RGB", (130, 100), "purple").save(other / "outside.jpg")
    app = create_app(data)
    library = app.state.library
    source_id = index(library, source)
    index(library, other)
    with library.connect() as db:
        db.execute("UPDATE photos SET captured_at='2025-04-01T00:00:00',kind='Photos'")
        db.execute("UPDATE photos SET captured_at='2025-04-20T00:00:00' WHERE filename='first.jpg'")
        db.execute("UPDATE photos SET captured_at='2024-04-01T00:00:00' WHERE filename='old.jpg'")
        db.execute("UPDATE photos SET kind='Illustrations' WHERE filename='art.jpg'")
    calls = fake_detection(monkeypatch)
    reached, release = threading.Event(), threading.Event()
    record = Checkpoint.record

    def hold_first(checkpoint, key, revision, outcome):
        record(checkpoint, key, revision, outcome)
        if not reached.is_set():
            reached.set()
            assert release.wait(10)

    monkeypatch.setattr(Checkpoint, "record", hold_first)
    scope = {"library": source_id, "date": "2025-04", "photos_only": True, "limit": 250}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/api/people/queue", json=scope).status_code == 401
        authenticate(app, client)
        assert client.post("/api/people/queue", json=scope).json() == {"pending": 3, "in_run": 3}
        assert client.post("/api/people/analyze", json={**scope, "date": "2025-13"}).status_code == 422
        task = client.post("/api/people/analyze", json=scope).json()
        try:
            assert reached.wait(10)
            assert client.post("/api/job/pause", json=task).status_code == 200
        finally:
            release.set()
        paused = wait_for_job(client, "paused")
        assert paused["resumable"] and (paused["done"], paused["total"]) == (1, 3)
        assert calls == ["first.jpg"]

    app = create_app(data)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        authenticate(app, client)
        restored = wait_for_job(client, "paused")
        assert restored["id"] == task["job_id"] and restored["done"] == 1
        assert client.post("/api/job/resume", json=task).status_code == 200
        complete = wait_for_job(client, "complete")
        assert complete["done"] == complete["total"] == 3
        assert complete["result"]["faces"] == 3 and complete["result"]["reused"] == 1
        assert client.post("/api/people/queue", json=scope).json() == {"pending": 0, "in_run": 0}
        assert client.post("/api/people/queue", json={}).json() == {"pending": 3, "in_run": 3}
        groups = client.get("/api/summary").json()["people"]
        assert len(groups) == 1 and groups[0]["count"] == 3
    assert len(calls) == 2 and calls[-1] in ("a.jpg", "b.jpg")


def test_people_recovers_photo_committed_before_its_receipt(tmp_path, monkeypatch):
    data, source = tmp_path / "data", tmp_path / "source"
    source.mkdir()
    Image.new("RGB", (130, 100), "green").save(source / "one.jpg")
    calls = fake_detection(monkeypatch)
    app = create_app(data)
    index(app.state.library, source)
    original = Checkpoint.record

    def interrupt_receipt(checkpoint, key, revision, outcome):
        raise RuntimeError("Stopped after committing face results.")

    monkeypatch.setattr(Checkpoint, "record", interrupt_receipt)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        authenticate(app, client)
        task = client.post("/api/people/analyze").json()
        assert wait_for_job(client, "paused")["done"] == 0
        assert client.get("/api/summary").json()["people"][0]["count"] == 1

    monkeypatch.setattr(Checkpoint, "record", original)
    app = create_app(data)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        authenticate(app, client)
        assert client.post("/api/job/resume", json=task).status_code == 200
        assert wait_for_job(client, "complete")["result"]["faces"] == 1
        assert client.get("/api/summary").json()["people"][0]["count"] == 1
        with app.state.library.connect() as db:
            assert db.execute("SELECT count(*) FROM faces").fetchone()[0] == 1
    assert calls == ["one.jpg"]
