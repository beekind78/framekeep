import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

import pytest
from PIL import Image

from framekeep.access import RuntimeStore
from framekeep.desktop import DesktopBackend, WindowsInstance, allowed_navigation, secure_webview
from framekeep.library import Library, file_hash


def request(url, token=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Framekeep-Token"] = token
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as response:
        return json.load(response)


def exchange(url):
    parts = urllib.parse.urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    ticket = urllib.parse.parse_qs(parts.fragment)["launch"][0]
    return request(origin + "/api/bootstrap", body={"ticket": ticket})["token"]


def test_desktop_backend_owns_and_stops_its_server_preserves_data_and_borrowed_server(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    Image.new("RGB", (100, 70), "teal").save(source / "photo.jpg")
    original_hash = file_hash(source / "photo.jpg")
    library = Library(tmp_path / "data")
    library.scan(library.add(str(source)), lambda *args: None, lambda: False)
    photo = library.photos({}, None)["photos"][0]
    library.assign_kind([photo["id"]], "Illustrations")
    library.update_photo(photo["id"], True, "desktop test")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    store = RuntimeStore(tmp_path / "runtime")
    backend = DesktopBackend(port, store, {"FRAMEKEEP_DATA": str(library.data)})
    restarted = None
    try:
        token = exchange(backend.start())
        record = store.read(port)
        assert record["desktop_owner"] == backend.owner and backend.owns_server
        assert not backend.busy()
        # A Windows redirector PID need not equal the actual interpreter PID.
        backend.record["pid"] = -1
        assert backend.owns_server
        with pytest.raises(urllib.error.HTTPError) as denied:
            request(backend.url + "/api/session")
        assert denied.value.code == 401
        summary = request(backend.url + "/api/summary", token)
        assert summary["total"] == 1 and summary["favorites"] == 1
        second = DesktopBackend(port, store, {"FRAMEKEEP_DATA": str(tmp_path / "wrong-data")})
        second_token = exchange(second.start())
        assert not second.owns_server
        assert request(second.url + "/api/summary", second_token)["total"] == 1
        second.stop()
        assert request(backend.url + "/api/summary", token)["total"] == 1
        backend.stop()
        assert backend.child.poll() == 0 and not store.path(port).exists()
        restarted = DesktopBackend(port, store, {"FRAMEKEEP_DATA": str(library.data)})
        fresh_token = exchange(restarted.start())
        assert store.read(port)["instance"] != record["instance"]
        assert request(restarted.url + "/api/summary", fresh_token)["total"] == 1
        preserved = request(restarted.url + "/api/photos", fresh_token)["photos"][0]
        assert preserved["kind_override"] == "Illustrations" and preserved["tags"] == "desktop test"
        assert file_hash(source / "photo.jpg") == original_hash
    finally:
        backend.stop()
        if restarted:
            restarted.stop()


@pytest.mark.parametrize("url,allowed", [
    ("http://127.0.0.1:47831/", True),
    ("http://127.0.0.1:47831/#launch=one-use-ticket", True),
    ("https://example.com/", False),
    ("http://127.0.0.1:47832/", False),
    ("http://localhost:47831/", False),
    ("http://127.0.0.1:47831/api/photos", False),
    ("file:///C:/Users/example/Pictures/photo.jpg", False),
    ("javascript:alert(1)", False),
    ("http://127.0.0.1:47831@example.com/", False),
    ("http://user:password@127.0.0.1:47831/", False),
    ("http://127.0.0.1:invalid/", False),
])
def test_desktop_navigation_stays_in_its_exact_local_app(url, allowed):
    assert allowed_navigation(url, "http://127.0.0.1:47831") is allowed


def test_native_navigation_gate_cancels_external_and_file_navigation_before_loading():
    from types import SimpleNamespace

    class Event:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

    control = SimpleNamespace(NavigationStarting=Event(), CoreWebView2InitializationCompleted=Event())
    window = SimpleNamespace(native=SimpleNamespace(browser=SimpleNamespace(webview=control)))
    secure_webview(window, "http://127.0.0.1:47831")
    for uri in ("https://outside.example/", "file:///C:/private.txt"):
        args = SimpleNamespace(Uri=uri, Cancel=False)
        control.NavigationStarting.handlers[0](None, args)
        assert args.Cancel
    args = SimpleNamespace(Uri="http://127.0.0.1:47831/#launch=ticket", Cancel=False)
    control.NavigationStarting.handlers[0](None, args)
    assert not args.Cancel
    toggles = []
    window.toggle_fullscreen = lambda: toggles.append(True)
    core = SimpleNamespace(Settings=SimpleNamespace(), PermissionRequested=Event(), NewWindowRequested=Event(),
        FrameNavigationStarting=Event(), ContainsFullScreenElementChanged=Event(), ContainsFullScreenElement=False)
    control.CoreWebView2 = core
    control.CoreWebView2InitializationCompleted.handlers[0](control, SimpleNamespace(IsSuccess=True))
    core.ContainsFullScreenElement = True
    core.ContainsFullScreenElementChanged.handlers[0](None, None)
    core.ContainsFullScreenElementChanged.handlers[0](None, None)
    core.ContainsFullScreenElement = False
    core.ContainsFullScreenElementChanged.handlers[0](None, None)
    assert len(toggles) == 2


@pytest.mark.skipif(sys.platform != "win32", reason="Windows desktop single-instance events")
def test_repeated_windows_launch_signals_existing_instance_without_another_window(tmp_path):
    first = WindowsInstance(47997, tmp_path / "runtime")
    second = WindowsInstance(47997, tmp_path / "runtime")
    try:
        assert first.acquire()
        assert not second.acquire()
        assert first.kernel.WaitForSingleObject(first.events["activate"], 0) == 0
        assert second.signal("stop")
        assert first.kernel.WaitForSingleObject(first.events["stop"], 0) == 0
    finally:
        second.close()
        first.close()
    third = WindowsInstance(47997, tmp_path / "runtime")
    try:
        assert third.acquire()
    finally:
        third.close()
