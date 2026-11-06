import time

from fastapi.testclient import TestClient
from PIL import Image

from framekeep.app import create_app
from framekeep.categories import Categories
from framekeep.category_model import MODEL_VERSION


def authenticate(app, client):
    token = client.post("/api/bootstrap", json={"ticket": app.state.access.ticket()}).json()["token"]
    client.headers["X-Framekeep-Token"] = token


def test_batch_preview_approval_and_persistent_undo(tmp_path):
    source, external, data = (tmp_path / name for name in ("source", "external", "data"))
    source.mkdir()
    external.mkdir()
    Image.new("RGB", (100, 80), "purple").save(source / "a.jpg")
    exact = (source / "a.jpg").read_bytes()
    (source / "copy.jpg").write_bytes(exact)
    (external / "external.jpg").write_bytes(exact)
    for name, color in (("excluded", "green"), ("pending", "yellow"), ("older", "orange")):
        Image.new("RGB", (100, 80), color).save(source / (name + ".jpg"))

    app = create_app(data)
    library = app.state.library
    library_id = library.add(str(source))
    library.scan(library_id, lambda *args: None, lambda: False)
    library.scan(library.add(str(external)), lambda *args: None, lambda: False)
    ids = {row["filename"]: row["id"] for row in library.photos({}, None)["photos"]}
    with library.connect() as db:
        db.execute("UPDATE photos SET kind_source='uncertain',kind_suggestion='Illustrations',"
                   "kind_score=.6,category_version=? WHERE filename!='pending.jpg'", (MODEL_VERSION,))
        db.execute("UPDATE photos SET kind_suggestion='Screenshots',category_version=? WHERE filename='older.jpg'",
                   (MODEL_VERSION - 1,))

    scope = {"library": library_id, "kind": "Needs review"}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/api/review/batch-preview", json={"filters": scope}).status_code == 401
        authenticate(app, client)
        preview = client.post("/api/review/batch-preview", json={"filters": scope}).json()
        assert preview["pending"] == preview["older"] == 1
        assert preview["suggestions"] == {"Illustrations": 3, "Screenshots": 1}
        assert preview["total"] == 2 and preview["kind"] == "Illustrations"
        identical = next(group for group in preview["groups"] if group["count"] == 2)
        selected = {"id": identical["id"], "revision": identical["revision"]}
        request = {"filters": scope, "kind": "Illustrations", "groups": [selected]}

        library.assign_kind([ids["copy.jpg"]], "Screenshots")
        assert client.post("/api/review/batch-approve", json=request).status_code == 400
        assert library.photo(ids["a.jpg"])["kind"] == "Needs review"
        library.assign_kind([ids["copy.jpg"]], None)
        approved = client.post("/api/review/batch-approve", json=request).json()
        assert approved["updated"] == 2
        assert client.post("/api/review/batch-approve", json=request).status_code == 400
        assert library.photo(ids["excluded.jpg"])["kind"] == "Needs review"
        assert library.photo(ids["external.jpg"])["kind"] == "Needs review"
        # Choosing the same category manually is still a later edit.
        library.assign_kind([ids["copy.jpg"]], "Illustrations")

    app = create_app(data)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        authenticate(app, client)
        history = client.get("/api/review/batch-history").json()["batches"]
        assert [(batch["id"], batch["remaining"]) for batch in history] == [(approved["batch_id"], 1)]
        undo = client.post("/api/review/batch-undo", json={"batch_id": approved["batch_id"]}).json()
        assert undo == {"restored": 1, "skipped": 1}
        assert client.get("/api/review/batch-history").json()["batches"] == []

    assert library.photo(ids["a.jpg"])["kind"] == "Needs review"
    assert library.photo(ids["copy.jpg"])["kind"] == "Illustrations"
    assert (source / "a.jpg").read_bytes() == (source / "copy.jpg").read_bytes() == exact


def test_pending_and_older_analysis_use_separate_queues(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    for name, color in (("new", "purple"), ("old", "red"), ("current", "green")):
        Image.new("RGB", (100, 80), color).save(source / (name + ".jpg"))
    (source / "z-new-copy.jpg").write_bytes((source / "new.jpg").read_bytes())
    app = create_app(tmp_path / "data")
    library = app.state.library
    library.scan(library.add(str(source)), lambda *args: None, lambda: False)
    with library.connect() as db:
        db.executemany("UPDATE photos SET kind_source='uncertain',kind_suggestion='Photos',category_version=? WHERE filename=?",
                       [(MODEL_VERSION - 1, "old.jpg"), (MODEL_VERSION, "current.jpg")])
    monkeypatch.setattr(Categories, "ready", lambda self: True)
    attempted = []

    def predict(path, *_args, **_kwargs):
        attempted.append(path.name)
        info = path.stat()
        return {"metadata": {"mtime_ns": info.st_mtime_ns, "size": info.st_size, "kind": "Needs review",
                             "kind_source": "uncertain", "kind_suggestion": "Illustrations",
                             "kind_score": .55, "category_version": MODEL_VERSION}}

    monkeypatch.setattr("framekeep.categories.process_image", predict)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        authenticate(app, client)
        for lane in ("new", "older", "new", "older"):
            response = client.post("/api/categories/analyze", json={"filters": {
                "kind": "Needs review", "pending_categories": lane, "sort": "name",
            }})
            assert response.status_code == 200
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                job = client.get("/api/job").json()["job"]
                if job["state"] == "complete":
                    break
                time.sleep(.03)
            assert job["state"] == "complete", job
    assert attempted == ["new.jpg", "old.jpg"]
    rows = library.photos({"q": "new"}, None)["photos"]
    assert len(rows) == 2 and all(row["category_version"] == MODEL_VERSION for row in rows)


def test_accept_all_is_scoped_checks_staleness_and_undoes_mixed_categories(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for index, name in enumerate(("scope-photo", "scope-art", "scope-pending", "scope-manual", "outside")):
        Image.new("RGB", (40, 40), (index * 30, 30, 60)).save(source / (name + ".jpg"))
    app = create_app(tmp_path / "data")
    library = app.state.library
    library.scan(library.add(str(source)), lambda *args: None, lambda: False)
    ids = {row["filename"]: row["id"] for row in library.photos({}, None)["photos"]}
    with library.connect() as db:
        db.executemany("UPDATE photos SET kind_source='uncertain',kind_suggestion=?,category_version=? WHERE id=?", [
            ("Photos", MODEL_VERSION, ids["scope-photo.jpg"]),
            ("Illustrations", MODEL_VERSION - 1, ids["scope-art.jpg"]),
            ("Illustrations", MODEL_VERSION, ids["scope-manual.jpg"]),
            ("Screenshots", MODEL_VERSION, ids["outside.jpg"]),
        ])
    library.assign_kind([ids["scope-manual.jpg"]], "Needs review")
    scope = {"q": "scope-"}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/api/review/accept-all/preview", json={"filters": scope}).status_code == 401
        authenticate(app, client)
        preview = client.post("/api/review/accept-all/preview", json={"filters": scope}).json()
        assert preview["total"] == 2 and preview["older"] == 1
        assert preview["counts"] == {"Photos": 1, "Illustrations": 1}
        body = {"filters": scope, "revision": preview["revision"]}
        with library.connect() as db:
            db.execute("UPDATE photos SET kind_suggestion='Screenshots' WHERE id=?", (ids["scope-art.jpg"],))
        assert client.post("/api/review/accept-all", json=body).status_code == 400
        assert library.photo(ids["scope-photo.jpg"])["kind"] == "Needs review"
        body["revision"] = client.post("/api/review/accept-all/preview", json={"filters": scope}).json()["revision"]
        applied = client.post("/api/review/accept-all", json=body).json()
        assert applied["updated"] == 2
        assert library.photo(ids["scope-photo.jpg"])["kind"] == "Photos"
        assert library.photo(ids["scope-art.jpg"])["kind"] == "Screenshots"
        for untouched in ("scope-pending.jpg", "scope-manual.jpg", "outside.jpg"):
            assert library.photo(ids[untouched])["kind"] == "Needs review"
        history = client.get("/api/review/batch-history").json()["batches"]
        assert [(batch["id"], batch["kind"], batch["remaining"]) for batch in history] == [
            (applied["batch_id"], "AI suggestions", 2),
        ]
        assert client.post("/api/review/batch-undo", json={"batch_id": applied["batch_id"]}).json() == {
            "restored": 2, "skipped": 0,
        }
        assert client.post("/api/review/accept-all/preview", json={"filters": scope}).json()["total"] == 2
