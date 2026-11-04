from fastapi.testclient import TestClient
from PIL import Image

from framekeep.app import create_app
from framekeep.people import People


def test_api_rejects_foreign_mutations_and_preserves_filters(tmp_path):
    app = create_app(tmp_path / "data")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/session").status_code == 401
        bootstrap = client.post("/api/bootstrap", json={"ticket": app.state.access.ticket()})
        headers = {"X-Framekeep-Token": bootstrap.json()["token"]}
        assert "token" not in client.get("/api/session", headers=headers).json()
        assert client.post("/api/scan", json={"path": str(tmp_path)}).status_code == 401
        assert (
            client.post("/api/demo", headers={**headers, "Origin": "https://other.example"}).status_code
            == 403
        )
        assert (
            client.post("/api/demo", headers={**headers, "Sec-Fetch-Site": "cross-site"}).status_code == 403
        )
        assert client.get("/api/summary", headers={**headers, "Host": "attacker.example"}).status_code == 400
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/api/photos/123/thumbnail", headers=headers).status_code == 400
        assert client.get("/api/photos?limit=999999", headers=headers).status_code == 422
        assert client.get("/api/photos?favorite=0", headers=headers).json()["total"] == 0
        assert (
            client.post("/api/scan", headers=headers, json={"path": str(tmp_path / "missing")}).status_code
            == 400
        )
        assert (
            client.post(
                "/api/organize/preview",
                headers=headers,
                json={"destination": str(tmp_path), "filters": {"ids": ["bad-id"]}},
            ).status_code
            == 422
        )


def test_people_manual_corrections(tmp_path):
    app = create_app(tmp_path / "data")
    library = app.state.library
    source = tmp_path / "photos"
    source.mkdir()
    Image.new("RGB", (30, 30), "green").save(source / "photo.jpg")
    library.scan(library.add(str(source)), lambda *args: None, lambda: False)
    photo_id = library.photos({}, None)["photos"][0]["id"]
    with library.connect() as db:
        first = db.execute("INSERT INTO persons(name) VALUES ('First')").lastrowid
        second = db.execute("INSERT INTO persons(name) VALUES ('Second')").lastrowid
        db.execute(
            "INSERT INTO faces(photo_id,person_id,embedding) VALUES (?,?,?)", (photo_id, first, "[1,0]")
        )
        db.execute(
            "INSERT INTO faces(photo_id,person_id,embedding) VALUES (?,?,?)", (photo_id, second, "[0,1]")
        )
    people = People(library)
    people.rename(first, "Alex")
    people.merge(second, first)
    assert len(library.summary()["people"]) == 1
    separated = people.split(first, photo_id)
    assert separated["person_id"] != first
    assert library.summary()["people"][0]["count"] == 1
    app.state.jobs.close()
