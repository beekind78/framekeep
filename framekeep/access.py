"""Per-instance local authority. Public HTTP never discloses the launcher credential."""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

from .paths import runtime_directory

VERSION = "1.14.0"
TICKET_TTL = 60
SESSION_TTL = 12 * 60 * 60


class LocalAccess:
    def __init__(self):
        self.instance = secrets.token_hex(16)
        self.control = secrets.token_urlsafe(48)
        self.lock = threading.Lock()
        self.tickets: dict[str, float] = {}
        self.sessions: dict[str, float] = {}

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def proof(self, challenge: str) -> str:
        return listener_proof(self.control, self.instance, challenge)

    def controls(self, value: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9_-]{64}", value)) and secrets.compare_digest(value, self.control)

    def ticket(self) -> str:
        with self.lock:
            now = time.monotonic()
            self.tickets = {key: until for key, until in self.tickets.items() if until > now}
            while len(self.tickets) >= 8:
                self.tickets.pop(next(iter(self.tickets)))
            ticket = secrets.token_urlsafe(48)
            self.tickets[self._digest(ticket)] = now + TICKET_TTL
            return ticket

    def exchange(self, ticket: str) -> str:
        if not 32 <= len(ticket) <= 128:
            raise ValueError("Launch ticket is invalid or expired. Open the launcher again.")
        with self.lock:
            now = time.monotonic()
            until = self.tickets.pop(self._digest(ticket), 0)
            if until <= now:
                raise ValueError("Launch ticket is invalid or expired. Open the launcher again.")
            self.sessions = {key: expiry for key, expiry in self.sessions.items() if expiry > now}
            while len(self.sessions) >= 32:
                self.sessions.pop(next(iter(self.sessions)))
            token = secrets.token_urlsafe(48)
            self.sessions[self._digest(token)] = now + SESSION_TTL
            return token

    def authenticates(self, token: str) -> bool:
        if not 32 <= len(token) <= 128:
            return False
        with self.lock:
            return self.sessions.get(self._digest(token), 0) > time.monotonic()


def listener_proof(control: str, instance: str, challenge: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", challenge):
        raise ValueError("Invalid listener challenge.")
    message = ("framekeep-listener-v1:" + instance + ":" + challenge).encode("ascii")
    return hmac.new(control.encode("ascii"), message, hashlib.sha256).hexdigest()


def _windows_protect(data: bytes, decrypt: bool = False) -> bytes:
    """DPAPI current-user scope, never CRYPTPROTECT_LOCAL_MACHINE."""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    if decrypt:
        function = crypt.CryptUnprotectData
        function.argtypes = [
            ctypes.POINTER(Blob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(Blob),
        ]
        args = (ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing))
    else:
        function = crypt.CryptProtectData
        function.argtypes = [
            ctypes.POINTER(Blob),
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(Blob),
        ]
        args = (
            ctypes.byref(incoming),
            "Framekeep local runtime",
            None,
            None,
            None,
            1,
            ctypes.byref(outgoing),
        )
    function.restype = wintypes.BOOL
    if not function(*args):
        raise OSError(ctypes.get_last_error(), "Windows could not protect/unprotect the local credential.")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(outgoing.data)


def _windows_owner_acl(path: Path):
    """A protected DACL grants only this process user and SYSTEM access."""
    from ctypes import wintypes

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    adv.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    token = wintypes.HANDLE()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    sid_text, descriptor = wintypes.LPWSTR(), ctypes.c_void_p()
    try:
        needed = wintypes.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not adv.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if not adv.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        sddl = "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;" + sid_text.value + ")"
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not adv.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(token)
        if sid_text:
            kernel.LocalFree(sid_text)
        if descriptor:
            kernel.LocalFree(descriptor)


class RuntimeStore:
    def __init__(self, root: Path | None = None):
        self.root = root if root is not None else runtime_directory()

    def path(self, port: int) -> Path:
        return self.root / f"instance-{port}.bin"

    def _secure(self, path: Path):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Runtime storage must not be a link or reparse point.")
        if sys.platform == "win32":
            _windows_owner_acl(path)
        else:
            if info.st_uid != os.getuid():
                raise ValueError("Runtime storage belongs to another user.")
            path.chmod(0o700 if path.is_dir() else 0o600)

    def write(self, port: int, access: LocalAccess):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._secure(self.root)
        record = {"instance": access.instance, "control": access.control, "port": port, "pid": os.getpid()}
        desktop_owner = os.environ.get("FRAMEKEEP_DESKTOP_OWNER", "")
        if re.fullmatch(r"[0-9a-f]{64}", desktop_owner):
            record["desktop_owner"] = desktop_owner
        payload = json.dumps(record).encode()
        payload = (
            b"DPAPI1\n" + _windows_protect(payload) if sys.platform == "win32" else b"PRIVATE1\n" + payload
        )
        handle, name = tempfile.mkstemp(prefix="runtime-", dir=self.root)
        temporary = Path(name)
        try:
            with os.fdopen(handle, "wb") as output:
                self._secure(temporary)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.path(port))
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, port: int) -> dict:
        path = self.path(port)
        self._secure(self.root)
        self._secure(path)
        payload = path.read_bytes()
        if len(payload) > 16_384:
            raise ValueError("Invalid runtime credential.")
        if sys.platform == "win32":
            if not payload.startswith(b"DPAPI1\n"):
                raise ValueError("Runtime credential is not protected by Windows.")
            payload = _windows_protect(payload[7:], decrypt=True)
        else:
            if not payload.startswith(b"PRIVATE1\n"):
                raise ValueError("Invalid runtime credential.")
            payload = payload[9:]
        record = json.loads(payload)
        if record.get("port") != port or not re.fullmatch(r"[0-9a-f]{32}", record.get("instance", "")):
            raise ValueError("Invalid runtime credential.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{64}", record.get("control", "")):
            raise ValueError("Invalid runtime credential.")
        return record

    def remove(self, port: int, instance: str):
        try:
            if self.read(port)["instance"] == instance:
                self.path(port).unlink()
        except (OSError, ValueError):
            pass
