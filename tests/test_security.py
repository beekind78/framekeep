import argparse
import asyncio
import io
import json
import os
import secrets
import struct
import subprocess
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import framekeep.access as access_module
import framekeep.image_processing as processing
import run
from framekeep import source_files
from framekeep.access import LocalAccess, RuntimeStore, listener_proof
from framekeep.app import BOOTSTRAP_MAX_BYTES, create_app
from framekeep.library import Library, file_hash, scan_hash
from framekeep.paths import data_directory
from framekeep.source_files import open_source


def unlock(client, app):
    ticket = app.state.access.ticket()
    response = client.post("/api/bootstrap", json={"ticket": ticket})
    assert response.status_code == 200
    assert not response.headers.get("set-cookie")
    return {"X-Framekeep-Token": response.json()["token"]}


def image_file(path, *, format=None, orientation=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    exif = Image.Exif()
    if orientation:
        exif[274] = orientation
    Image.new("RGB", (80, 50), "teal").save(path, format=format, exif=exif)
    return path


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize(
    "path",
    [
        "/api/session",
        "/api/summary",
        "/api/photos",
        "/api/job",
        "/api/photos/1/thumbnail",
        "/api/photos/1/preview",
        "/api/people/1/faces",
        "/api/faces/1",
        "/api/%73ession",
        "/api/photos/",
    ],
)
def test_all_private_reads_require_nonambient_credential(tmp_path, method, path):
    app = create_app(tmp_path / "data")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.request(method, path).status_code == 401
        assert client.request(method, path, headers={"X-Framekeep-Token": "wrong"}).status_code == 401
        assert (
            client.request(method, path, headers={"Cookie": "framekeep_session=anything"}).status_code == 401
        )


def test_bootstrap_roles_origins_replays_and_private_mutations(tmp_path):
    app = create_app(tmp_path / "data")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/api/bootstrap", json={"ticket": secrets.token_urlsafe(48)}).status_code == 401
        assert client.post("/api/launch").status_code == 401
        ticket = app.state.access.ticket()
        response = client.post("/api/bootstrap", json={"ticket": ticket})
        assert response.status_code == 200
        headers = {"X-Framekeep-Token": response.json()["token"]}
        assert client.post("/api/bootstrap", json={"ticket": ticket}).status_code == 401
        assert client.get("/api/session", headers=headers).status_code == 200
        assert "token" not in client.get("/api/session", headers=headers).json()
        assert client.get("/api/summary", headers=headers).json()["total"] == 0
        for extra in (
            {"Origin": "https://attacker.example"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
            {"Origin": "http://127.0.0.1:9999"},
        ):
            assert client.get("/api/summary", headers={**headers, **extra}).status_code == 403
            assert client.post("/api/demo", headers={**headers, **extra}).status_code == 403
        assert client.post("/api/launch", headers=headers).status_code == 401
        assert client.post("/api/shutdown", headers=headers).status_code == 401
        assert client.post("/api/scan", json={"path": str(tmp_path)}).status_code == 401
        assert client.post("/api/organize/preview", json={}).status_code == 401
        control = {"X-Framekeep-Control": app.state.access.control}
        assert client.get("/api/photos", headers=control).status_code == 401
        assert client.post("/api/launch", headers=control).status_code == 200
        assert client.get("/api/session", headers={"X-Framekeep-Token": ticket}).status_code == 401
        health = client.get("/api/health", params={"challenge": "a" * 64}).json()
        assert health["proof"] == listener_proof(
            app.state.access.control, app.state.access.instance, "a" * 64
        )
        assert "token" not in health and "control" not in health and "data_dir" not in health


def test_bootstrap_bounds_declared_and_streamed_bodies_before_spending_ticket(tmp_path):
    app = create_app(tmp_path / "data")
    ticket = app.state.access.ticket()
    payload = json.dumps({"ticket": ticket, "padding": " " * BOOTSTRAP_MAX_BYTES}).encode()

    async def send_chunked():
        chunks = iter((payload[:512], payload[512:]))
        responses = []

        async def receive():
            body = next(chunks, b"")
            return {"type": "http.request", "body": body, "more_body": bool(body)}

        async def send(message):
            responses.append(message)

        await app({
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "scheme": "http", "method": "POST", "path": "/api/bootstrap",
            "headers": [(b"host", b"127.0.0.1"), (b"content-type", b"application/json")],
            "query_string": b"", "server": ("127.0.0.1", 80),
        }, receive, send)
        assert responses[0]["status"] == 413

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/api/bootstrap", content=payload, headers={"Content-Type": "application/json"})
        assert response.status_code == 413
        asyncio.run(send_chunked())
        assert client.post("/api/bootstrap", json={"ticket": ticket}).status_code == 200


def test_scan_worker_rejects_replaced_photo_even_with_matching_size_and_mtime(tmp_path):
    original = image_file(tmp_path / "source" / "photo.jpg")
    replacement = original.with_name("replacement.jpg")
    replacement.write_bytes(original.read_bytes())
    info = original.stat()
    os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns))
    expected = source_files.file_identity(info)
    replacement.replace(original)
    with pytest.raises(ValueError, match="changed during indexing"):
        processing.process_image(original, original.parent, 480, expected=expected)


def test_scan_hash_stops_before_reading_next_chunk_when_cancelled(tmp_path):
    path = tmp_path / "synthetic"
    path.write_bytes(b"x" * (2 * 1024**2))
    with path.open("rb") as stream:
        assert scan_hash(stream, os.fstat(stream.fileno()), lambda: stream.tell() >= 1024**2) is None
        assert stream.tell() == 1024**2


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX named pipes")
def test_source_fifo_is_rejected_before_a_blocking_read(tmp_path):
    pipe = tmp_path / "pipe.jpg"
    os.mkfifo(pipe)
    root = Path(__file__).resolve().parents[1]
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; "
        "from framekeep.source_files import open_source; path = Path(sys.argv[2]); "
        "open_source(path, path.parent).__enter__()"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(root), str(pipe)],
        capture_output=True, text=True, timeout=5, check=False,
    )
    assert result.returncode != 0 and "Only regular" in result.stderr


def test_ticket_expiry_session_expiry_and_restart_rotation(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(access_module.time, "monotonic", lambda: clock[0])
    access = LocalAccess()
    ticket = access.ticket()
    clock[0] += 61
    with pytest.raises(ValueError):
        access.exchange(ticket)
    token = access.exchange(access.ticket())
    assert access.authenticates(token)
    replacement = LocalAccess()
    assert not replacement.authenticates(token)
    assert not replacement.controls(access.control)
    clock[0] += access_module.SESSION_TTL + 1
    assert not access.authenticates(token)
    assert not access.controls("é" * 64)


def test_runtime_credential_is_private_and_round_trips(tmp_path):
    access = LocalAccess()
    store = RuntimeStore(tmp_path / "runtime")
    store.write(47839, access)
    raw = store.path(47839).read_bytes()
    if sys.platform == "win32":
        assert raw.startswith(b"DPAPI1\n")
        assert access.control.encode() not in raw
    else:
        assert store.root.stat().st_mode & 0o077 == 0
        assert store.path(47839).stat().st_mode & 0o077 == 0
    record = store.read(47839)
    assert record["control"] == access.control
    assert record["instance"] == access.instance
    store.remove(47839, "another-instance")
    assert store.path(47839).exists()
    store.remove(47839, access.instance)
    assert not store.path(47839).exists()


def test_default_storage_is_shared_across_appdata_contexts(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: profile))
    monkeypatch.delenv("FRAMEKEEP_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("FRAMEKEEP_DATA", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "packaged-appdata"))
    packaged = RuntimeStore()
    packaged_data = data_directory()
    access = LocalAccess()
    packaged.write(47839, access)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "native-appdata"))
    native = RuntimeStore()
    assert native.root == packaged.root == profile / ".framekeep" / "runtime"
    assert data_directory() == packaged_data == profile / ".framekeep" / "data"
    assert native.read(47839)["control"] == access.control
    native.remove(47839, access.instance)


def test_missing_runtime_cannot_authorize_an_existing_listener(tmp_path, monkeypatch):
    contacted = []
    monkeypatch.setattr(run, "local_request", lambda *args, **kwargs: contacted.append(args))
    with pytest.raises(ValueError, match="no matching private session"):
        run.reuse_instance(
            argparse.Namespace(port=47839, stop=False, no_browser=True),
            RuntimeStore(tmp_path / "missing"),
            "http://127.0.0.1:47839",
        )
    assert not contacted


def test_spoofed_listener_does_not_receive_control_credential(tmp_path):
    access = LocalAccess()
    observed = []

    class Impostor(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append(dict(self.headers))
            payload = json.dumps(
                {"app": "framekeep", "instance": access.instance, "proof": "0" * 64}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Impostor)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    port = server.server_address[1]
    store = RuntimeStore(tmp_path / "runtime")
    store.write(port, access)
    try:
        with pytest.raises(ValueError, match="prove"):
            run.reuse_instance(
                argparse.Namespace(port=port, stop=True, no_browser=True), store, f"http://127.0.0.1:{port}"
            )
        assert len(observed) == 1
        assert all("X-Framekeep-Control" not in headers for headers in observed)
        assert access.control not in json.dumps(observed)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_authenticated_thumbnail_preview_and_tags_remain_working(tmp_path):
    source = tmp_path / "source"
    original = image_file(source / "rotated.jpg", orientation=6)
    before = file_hash(original)
    app = create_app(tmp_path / "data")
    app.state.library.scan(app.state.library.add(str(source)), lambda *args: None, lambda: False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = unlock(client, app)
        photo = client.get("/api/photos", headers=headers).json()["photos"][0]
        for suffix in ("thumbnail", "preview"):
            response = client.get(f"/api/photos/{photo['id']}/{suffix}", headers=headers)
            assert response.status_code == 200
            with Image.open(io.BytesIO(response.content)) as result:
                assert result.height > result.width
                assert result.format == "JPEG"
                assert not result.getexif()
        response = client.patch(
            f"/api/photos/{photo['id']}", headers=headers, json={"favorite": True, "tags": "synthetic test"}
        )
        assert response.status_code == 200
        assert client.get("/api/photos?favorite=1", headers=headers).json()["total"] == 1
    assert file_hash(original) == before


@pytest.mark.parametrize(
    "name,format",
    [
        ("normal.jpg", "JPEG"),
        ("normal.png", "PNG"),
        ("iphone.heic", "HEIF"),
        ("modern.avif", "AVIF"),
        ("animated.gif", "GIF"),
        ("normal.tiff", "TIFF"),
    ],
)
def test_normal_formats_use_bounded_decoder(tmp_path, name, format):
    image_file(tmp_path / name, format=format)
    result = processing.process_image(tmp_path / name, tmp_path, 480)
    assert result["metadata"]["width"] == 80
    assert result["metadata"]["height"] == 50
    assert result["jpeg"].startswith(b"\xff\xd8")


@pytest.mark.parametrize(
    "content",
    [
        b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 100 100\n%%EndComments\nshowpage\n",
        b"#define x_width 1\n#define x_height 1\nstatic char x_bits[] = { 0x00 };\n",
        b"not an image",
    ],
)
def test_renamed_unsupported_content_is_rejected(tmp_path, content):
    path = tmp_path / "looks-like-a-photo.jpg"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        processing.process_image(path, tmp_path, 480)


def png_header(width, height):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0\0\0\0"))
        + chunk(b"IEND", b"")
    )


@pytest.mark.parametrize("width,height", [(10001, 5000), (30001, 1)])
def test_oversized_headers_rejected_before_full_decode(tmp_path, width, height):
    path = tmp_path / "huge.png"
    path.write_bytes(png_header(width, height))
    with pytest.raises(ValueError):
        processing.process_image(path, tmp_path, 480)


def test_encoded_size_and_decode_timeout(tmp_path, monkeypatch):
    path = image_file(tmp_path / "normal.jpg")
    monkeypatch.setattr(processing, "MAX_FILE_BYTES", 10)
    with pytest.raises(ValueError, match="encoded-file limit"):
        processing.process_image(path, tmp_path, 480)
    monkeypatch.setattr(processing, "MAX_FILE_BYTES", 256 * 1024 * 1024)
    monkeypatch.setattr(processing, "TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(
        processing, "_worker_command", lambda: [sys.executable, "-I", "-c", "import time; time.sleep(30)"]
    )
    started = time.monotonic()
    with pytest.raises(ValueError, match="exceeded"):
        processing.process_image(path, tmp_path, 480)
    assert time.monotonic() - started < 5


def test_worker_memory_limit_is_enforced():
    root = Path(__file__).resolve().parents[1]
    script = (
        "import sys; sys.path.insert(0, " + repr(str(root)) + "); "
        "from framekeep.image_worker import set_resource_limits, MEMORY_BYTES; set_resource_limits(); "
        "\ntry:\n bytearray(MEMORY_BYTES + 1)\nexcept MemoryError:\n print('limited')\n"
    )
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    response = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        creationflags=flags,
    )
    assert response.returncode == 0, response.stderr
    assert response.stdout.strip() == "limited"


def test_junction_and_indexed_path_replacement_do_not_escape_source(tmp_path):
    source = tmp_path / "source"
    private = tmp_path / "outside"
    original = image_file(source / "nested" / "photo.jpg")
    image_file(private / "photo.jpg", format="JPEG")
    library = Library(tmp_path / "data")
    library.scan(library.add(str(source)), lambda *args: None, lambda: False)
    photo = library.photos({}, None)["photos"][0]
    original.unlink()
    original.parent.rmdir()
    link = source / "nested"
    if sys.platform == "win32":
        # Fixed paths in this disposable test directory; mklink is CMD's junction API.
        response = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(private)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert response.returncode == 0, response.stderr
    else:
        link.symlink_to(private, target_is_directory=True)
    try:
        with pytest.raises(ValueError), open_source(Path(photo["path"]), Path(photo["source_root"])):
            pass
        with pytest.raises(ValueError):
            processing.process_image(Path(photo["path"]), Path(photo["source_root"]), 480)
        output = tmp_path / "output"
        output.mkdir()
        preview = library.plan(str(output), "date", {}, False)
        result = library.export(library.consume_plan(preview["token"]), lambda *args: None, lambda: False)
        assert result["copied"] == 0
        assert result["error_count"] == 1
    finally:
        # Remove only the link itself, never the target tree.
        link.rmdir() if sys.platform == "win32" else link.unlink()


def test_old_index_entries_are_revalidated_under_image_policy(tmp_path):
    source = tmp_path / "source"
    image_file(source / "photo.jpg")
    library = Library(tmp_path / "data")
    library_id = library.add(str(source))
    library.scan(library_id, lambda *args: None, lambda: False)
    with library.connect() as db:
        db.execute("UPDATE photos SET policy_version=0")
    assert library.scan(library_id, lambda *args: None, lambda: False)["indexed"] == 1


def test_root_swap_between_validation_and_open_is_rejected(tmp_path, monkeypatch):
    source = tmp_path / "source"
    outside = tmp_path / "outside"
    photo = image_file(source / "photo.jpg")
    image_file(outside / "photo.jpg")
    moved = tmp_path / "source-before-swap"
    validate = source_files.source_path
    switched = False

    def swap_after_validation(path, root):
        nonlocal switched
        resolved = validate(path, root)
        assert source.resolve().is_relative_to(tmp_path.resolve())
        assert moved.absolute().is_relative_to(tmp_path.resolve())
        source.rename(moved)
        if sys.platform == "win32":
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(source), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, result.stderr
        else:
            source.symlink_to(outside, target_is_directory=True)
        switched = True
        return resolved

    monkeypatch.setattr(source_files, "source_path", swap_after_validation)
    try:
        with pytest.raises(ValueError, match="outside"), open_source(photo, source):
            raise AssertionError("An outside file was opened as an authorized source.")
    finally:
        if switched:
            source.rmdir() if sys.platform == "win32" else source.unlink()
        if moved.exists():
            moved.rename(source)
