"""Real loopback/Windows-process workflow, using only generated test images."""

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

from framekeep.access import RuntimeStore, listener_proof

ROOT = Path(__file__).resolve().parents[1]


def request(url, path, *, token=None, control=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Framekeep-Token"] = token
    if control:
        headers["X-Framekeep-Control"] = control
    request_object = urllib.request.Request(
        url + path, data=json.dumps(body).encode() if body is not None else None, headers=headers
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request_object, timeout=5) as response:
            data = response.read()
            return response.status, json.loads(
                data
            ) if response.headers.get_content_type() == "application/json" else data
    except urllib.error.HTTPError as error:
        return error.code, None


def await_health(url, process):
    challenge = secrets.token_hex(32)
    for _ in range(100):
        assert process.poll() is None, "The test server exited during startup."
        try:
            status, data = request(url, "/api/health?challenge=" + challenge)
            if status == 200:
                return challenge, data
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError("The isolated test server did not start.")


def await_job(url, token):
    for _ in range(200):
        status, data = request(url, "/api/job", token=token)
        assert status == 200
        if data["job"]["state"] in ("complete", "failed", "cancelled"):
            assert data["job"]["state"] == "complete", data["job"]
            return data["job"]["result"]
        time.sleep(0.05)
    raise AssertionError("The test job did not finish.")


def test_real_server_auth_import_copy_reuse_stop_and_restart(tmp_path):
    source, output = tmp_path / "source", tmp_path / "output"
    source.mkdir()
    output.mkdir()
    original = source / "IMG_20240102_030405.jpg"
    Image.new("RGB", (100, 70), "goldenrod").save(original)
    shutil.copy2(original, source / "copy.jpg")
    original_bytes = original.read_bytes()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    runtime = tmp_path / "runtime"
    store = RuntimeStore(runtime)
    env = {**os.environ, "FRAMEKEEP_DATA": str(tmp_path / "data"), "FRAMEKEEP_RUNTIME_DIR": str(runtime)}
    command = [sys.executable, "run.py", "--port", str(port), "--no-browser"]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    url = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        command, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags
    )
    try:
        challenge, health = await_health(url, process)
        record = store.read(port)
        assert health["proof"] == listener_proof(record["control"], record["instance"], challenge)
        assert request(url, "/api/session")[0] == 401
        assert request(url, "/api/photos")[0] == 401
        assert request(url, "/api/shutdown", body={})[0] == 401
        _, launched = request(url, "/api/launch", control=record["control"], body={})
        status, browser = request(url, "/api/bootstrap", body={"ticket": launched["ticket"]})
        assert status == 200
        token = browser["token"]
        assert request(url, "/api/bootstrap", body={"ticket": launched["ticket"]})[0] == 401
        assert request(url, "/api/scan", token=token, body={"path": str(source)})[0] == 200
        assert await_job(url, token)["indexed"] == 2
        status, photos = request(url, "/api/photos", token=token)
        assert status == 200 and photos["total"] == 2
        photo_id = photos["photos"][0]["id"]
        assert request(url, f"/api/photos/{photo_id}/thumbnail")[0] == 401
        assert request(url, f"/api/photos/{photo_id}/thumbnail", token=token)[0] == 200
        assert request(url, f"/api/photos/{photo_id}/preview", token=token)[0] == 200
        status, plan = request(
            url,
            "/api/organize/preview",
            token=token,
            body={"destination": str(output), "layout": "date_type", "filters": {}, "skip_duplicates": True},
        )
        assert status == 200 and plan["count"] == 1 and plan["skipped"] == 1
        assert request(url, "/api/organize/copy", token=token, body={"token": plan["token"]})[0] == 200
        result = await_job(url, token)
        assert result["copied"] == 1 and result["error_count"] == 0
        assert len(list(output.rglob("*.jpg"))) == 1
        assert original.read_bytes() == original_bytes
        assert request(url, "/api/shutdown", token=token, body={})[0] == 401
        reused = subprocess.run(
            command, cwd=ROOT, env=env, capture_output=True, timeout=10, check=False, creationflags=flags
        )
        assert reused.returncode == 0
        assert store.read(port)["instance"] == record["instance"]
        stopped = subprocess.run(
            command + ["--stop"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            timeout=10,
            check=False,
            creationflags=flags,
        )
        assert stopped.returncode == 0
        assert process.wait(timeout=10) == 0
        assert not store.path(port).exists()
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        await_health(url, process)
        fresh = store.read(port)
        assert fresh["instance"] != record["instance"]
        assert fresh["control"] != record["control"]
        assert request(url, "/api/session", token=token)[0] == 401
        assert request(url, "/api/launch", control=record["control"], body={})[0] == 401
    finally:
        if process.poll() is None:
            subprocess.run(
                command + ["--stop"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                timeout=10,
                check=False,
                creationflags=flags,
            )
            process.wait(timeout=10)


def test_launcher_and_server_share_state_with_different_appdata_environments(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    base = {
        key: value
        for key, value in os.environ.items()
        if key not in ("FRAMEKEEP_DATA", "FRAMEKEEP_RUNTIME_DIR")
    }
    base.update({"USERPROFILE": str(profile), "HOME": str(profile)})
    server_env = {**base, "LOCALAPPDATA": str(tmp_path / "packaged" / "LocalCache" / "Local")}
    desktop_env = {**base, "LOCALAPPDATA": str(tmp_path / "ordinary" / "AppData" / "Local")}
    command = [sys.executable, "run.py", "--port", str(port), "--no-browser"]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=server_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    store = RuntimeStore(profile / ".framekeep" / "runtime")
    try:
        await_health(f"http://127.0.0.1:{port}", process)
        assert store.path(port).is_file()
        assert (profile / ".framekeep" / "data" / "library.sqlite3").is_file()
        record = store.read(port)
        reused = subprocess.run(
            command,
            cwd=ROOT,
            env=desktop_env,
            capture_output=True,
            timeout=10,
            check=False,
            creationflags=flags,
        )
        assert reused.returncode == 0, reused.stderr.decode(errors="replace")
        assert store.read(port)["instance"] == record["instance"]
        stopped = subprocess.run(
            command + ["--stop"],
            cwd=ROOT,
            env=desktop_env,
            capture_output=True,
            timeout=10,
            check=False,
            creationflags=flags,
        )
        assert stopped.returncode == 0, stopped.stderr.decode(errors="replace")
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            subprocess.run(
                command + ["--stop"],
                cwd=ROOT,
                env=desktop_env,
                capture_output=True,
                timeout=10,
                check=False,
                creationflags=flags,
            )
            process.wait(timeout=10)
