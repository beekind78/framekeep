"""Windows desktop host. Keeps the existing authenticated loopback app and local library."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .access import RuntimeStore

ROOT = Path(__file__).resolve().parents[1]
ICON = ROOT / "framekeep" / "static" / "framekeep.ico"
APP_ID = "Framekeep.Desktop"


def allowed_navigation(url: str, origin: str) -> bool:
    """The host displays only the Framekeep root, never files or another web origin."""
    try:
        parsed, expected = urlsplit(url), urlsplit(origin)
        return (parsed.scheme, parsed.hostname, parsed.port, parsed.path) == (
            expected.scheme, expected.hostname, expected.port, "/"
        ) and not parsed.username and not parsed.password
    except ValueError:
        return False


class DesktopBackend:
    def __init__(self, port=47831, store=None, environment=None):
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.store = store or RuntimeStore()
        self.owner = secrets.token_hex(32)
        self.environment = {**os.environ, **(environment or {}), "FRAMEKEEP_RUNTIME_DIR": str(self.store.root),
                            "FRAMEKEEP_DESKTOP_OWNER": self.owner}
        self.child = None
        self.record = None
        self.token = None

    def _prove(self, stop=False):
        from run import reuse_instance

        args = argparse.Namespace(port=self.port, stop=stop, no_browser=True)
        reuse_instance(args, self.store, self.url)
        return self.store.read(self.port) if not stop else None

    def start(self):
        with socket.socket() as probe:
            if sys.platform == "win32":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind(("127.0.0.1", self.port))
                available = True
            except OSError:
                available = False
        if available:
            self.child = subprocess.Popen(
                [sys.executable, str(ROOT / "run.py"), "--port", str(self.port), "--no-browser"],
                cwd=ROOT, env=self.environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    self.record = self._prove()
                    break
                except (OSError, ValueError):
                    if self.child.poll() is not None:
                        raise ValueError("The local Framekeep service could not start. Check the launcher logs.") from None
                    time.sleep(0.05)
            else:
                raise ValueError("The local Framekeep service did not become ready in time.")
        else:
            # No capability is sent until the listener proves possession of our private control key.
            self.record = self._prove()
        from run import local_request

        ticket = local_request(self.url + "/api/launch", control=self.record["control"], post=True)["ticket"]
        return self.url + "/#launch=" + ticket

    def _read_json(self, path, body=None):
        request = urllib.request.Request(
            self.url + path, data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "X-Framekeep-Token": self.token or ""},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=3) as response:
            raw = response.read(65_537)
        if len(raw) > 65_536:
            raise ValueError("Unexpected response from the local service.")
        return json.loads(raw)

    def busy(self):
        if self.token is None:
            from run import local_request

            self._prove()
            ticket = local_request(self.url + "/api/launch", control=self.record["control"], post=True)["ticket"]
            self.token = self._read_json("/api/bootstrap", {"ticket": ticket})["token"]
        job = self._read_json("/api/job")["job"]
        return bool(job and job["state"] in ("queued", "running"))

    @property
    def owns_server(self):
        # Windows venv/uv redirectors may have a different PID from the interpreter.
        # The per-launch marker survives that redirect and stays in the protected runtime record.
        return bool(self.child and self.record and secrets.compare_digest(
            self.owner, self.record.get("desktop_owner", "")
        ))

    def stop(self):
        if not self.owns_server or self.child.poll() is not None:
            return
        try:
            current = self.store.read(self.port)
            if current["instance"] != self.record["instance"]:
                return
            self._prove(stop=True)
            self.child.wait(timeout=35)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            # Never kill an unrelated listener or force-terminate a copy in progress.
            pass


class WindowsInstance:
    """One window per user/runtime/port. Named events only activate or close that window."""

    def __init__(self, port, runtime):
        from ctypes import wintypes

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateEventW.restype = wintypes.HANDLE
        self.kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.OpenEventW.restype = wintypes.HANDLE
        self.kernel.SetEvent.argtypes = [wintypes.HANDLE]
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        digest = hashlib.sha256((str(Path(runtime).resolve()) + ":" + str(port)).encode()).hexdigest()[:24]
        self.name = "Local\\Framekeep.Desktop." + digest
        self.mutex = None
        self.events = {}

    def signal(self, action):
        handle = self.kernel.OpenEventW(0x0002, False, self.name + "." + action)
        if not handle:
            return False
        try:
            return bool(self.kernel.SetEvent(handle))
        finally:
            self.kernel.CloseHandle(handle)

    def acquire(self):
        self.mutex = self.kernel.CreateMutexW(None, False, self.name)
        if not self.mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            self.kernel.CloseHandle(self.mutex)
            self.mutex = None
            for _ in range(40):
                if self.signal("activate"):
                    return False
                time.sleep(0.05)
            raise ValueError("Framekeep is still opening. Try its shortcut again in a moment.")
        for action in ("activate", "stop"):
            handle = self.kernel.CreateEventW(None, False, False, self.name + "." + action)
            if not handle:
                self.close()
                raise ctypes.WinError(ctypes.get_last_error())
            self.events[action] = handle
        return True

    def watch(self, window, done):
        while not done.is_set():
            if self.kernel.WaitForSingleObject(self.events["activate"], 150) == 0:
                window.restore()
                window.show()
            if self.kernel.WaitForSingleObject(self.events["stop"], 0) == 0:
                window.destroy()

    def close(self):
        for handle in self.events.values():
            self.kernel.CloseHandle(handle)
        self.events.clear()
        if self.mutex:
            self.kernel.CloseHandle(self.mutex)
            self.mutex = None


def secure_webview(window, origin):
    """Install navigation restrictions before the initial WebView2 page is loaded."""
    control = window.native.browser.webview

    def navigation(_, args):
        if not allowed_navigation(str(args.Uri), origin):
            args.Cancel = True

    def ready(sender, args):
        if not args.IsSuccess:
            return
        core = sender.CoreWebView2
        core.Settings.AreHostObjectsAllowed = False
        core.Settings.IsPasswordAutosaveEnabled = False
        core.Settings.IsGeneralAutofillEnabled = False

        def no_permission(_, event):
            from Microsoft.Web.WebView2.Core import CoreWebView2PermissionState

            event.State = CoreWebView2PermissionState.Deny

        def no_new_window(_, event):
            event.Handled = True

        full_screen = False

        def media_fullscreen(_, event):
            nonlocal full_screen
            desired = bool(core.ContainsFullScreenElement)
            if desired != full_screen:
                full_screen = desired
                window.toggle_fullscreen()

        core.PermissionRequested += no_permission
        core.NewWindowRequested += no_new_window
        core.FrameNavigationStarting += navigation
        core.ContainsFullScreenElementChanged += media_fullscreen

    control.NavigationStarting += navigation
    control.CoreWebView2InitializationCompleted += ready


def launch(port=47831, title="Framekeep"):
    if sys.platform != "win32":
        raise ValueError("The Framekeep desktop launcher currently supports Windows. Use run.py for browser mode.")
    import webview

    store = RuntimeStore()
    instance = WindowsInstance(port, store.root)
    backend = DesktopBackend(port, store)
    done = threading.Event()
    watcher = None
    try:
        if not instance.acquire():
            return 0
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        url = backend.start()
        store.root.mkdir(parents=True, exist_ok=True)
        store._secure(store.root)
        profile_root = store.root / "webview"
        profile_root.mkdir(exist_ok=True)
        store._secure(profile_root)
        profile = Path(tempfile.mkdtemp(prefix="desktop-", dir=profile_root)).resolve()
        if not profile.is_relative_to(profile_root.resolve()):
            raise ValueError("Invalid desktop profile location.")
        store._secure(profile)
        webview.settings.update({"ALLOW_FILE_URLS": False, "ALLOW_DOWNLOADS": False,
            "OPEN_EXTERNAL_LINKS_IN_BROWSER": False, "OPEN_DEVTOOLS_IN_DEBUG": False,
            "REMOTE_DEBUGGING_PORT": None, "IGNORE_SSL_ERRORS": False, "SHOW_DEFAULT_MENUS": False})
        window = webview.create_window(title, url, width=1320, height=860, min_size=(940, 660),
            background_color="#faf9f6", text_select=True, zoomable=False, draggable=False)
        window.events.before_show += lambda: secure_webview(window, backend.url)

        def closing():
            try:
                if backend.owns_server and backend.busy():
                    return window.create_confirmation_dialog("Close Framekeep?",
                        "A task is still running. Stop it safely and close Framekeep? Completed copies will be kept.")
            except (OSError, ValueError):
                pass
            return True

        window.events.closing += closing
        window.events.closed += done.set

        def watch():
            nonlocal watcher
            watcher = threading.current_thread()
            instance.watch(window, done)

        webview.start(watch, gui="edgechromium", debug=False, private_mode=True,
                      storage_path=str(profile), icon=str(ICON))
        return 0
    finally:
        done.set()
        if watcher and watcher is not threading.current_thread():
            watcher.join(timeout=2)
        backend.stop()
        instance.close()


def main():
    parser = argparse.ArgumentParser(description="Framekeep Windows desktop app")
    parser.add_argument("--port", type=int, default=47831)
    parser.add_argument("--title", default="Framekeep")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Choose a TCP port between 1 and 65535.")
    try:
        if args.stop:
            instance = WindowsInstance(args.port, RuntimeStore().root)
            if instance.signal("stop"):
                return 0
            from run import run

            run(argparse.Namespace(port=args.port, stop=True, no_browser=True))
            return 0
        return launch(args.port, args.title)
    except Exception as error:  # noqa: BLE001 -- GUI launcher must present startup failures
        message = "Framekeep could not open securely. " + str(error)
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, message, "Framekeep", 0x30)
        return 1
