"""Explicit, write-only Windows clipboard operations for registered originals."""

import ctypes
import struct
import sys
import threading
import time

_lock = threading.Lock()


def file_payload(paths):
    if not paths or any(not path or "\0" in path for path in paths):
        raise ValueError("Invalid clipboard file list.")
    return struct.pack("<IiiII", 20, 0, 0, 0, 1) + ("\0".join(paths) + "\0\0").encode("utf-16-le")


def copy_files(paths):
    _publish([(15, file_payload([str(path) for path in paths])), ("Preferred DropEffect", struct.pack("<I", 1))])


def copy_text(text):
    if "\0" in text:
        raise ValueError("Invalid clipboard text.")
    _publish([(13, (text + "\0").encode("utf-16-le"))])


def _publish(formats):
    if sys.platform != "win32":
        raise ValueError("Copy to clipboard is supported in the Windows desktop app.")
    from ctypes import wintypes

    user, kernel = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
    user.CreateWindowExW.restype = wintypes.HWND
    user.DestroyWindow.argtypes = [wintypes.HWND]
    user.OpenClipboard.argtypes = [wintypes.HWND]
    user.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user.RegisterClipboardFormatW.restype = wintypes.UINT
    user.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user.SetClipboardData.restype = wintypes.HANDLE
    kernel.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel.GlobalAlloc.restype = wintypes.HANDLE
    kernel.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel.GlobalLock.restype = ctypes.c_void_p
    kernel.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel.GlobalFree.argtypes = [wintypes.HANDLE]
    with _lock:
        window = user.CreateWindowExW(0, "STATIC", "Framekeep clipboard", 0, 0, 0, 0, 0, None, None, None, None)
        if not window:
            raise ValueError("The Windows clipboard is unavailable.")
        allocated, opened = [], False
        try:
            for format_id, data in formats:
                format_id = user.RegisterClipboardFormatW(format_id) if isinstance(format_id, str) else format_id
                handle = kernel.GlobalAlloc(2, len(data))  # GMEM_MOVEABLE; Windows owns successful transfers.
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                allocated.append([format_id, handle])
                pointer = kernel.GlobalLock(handle)
                if not pointer:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    ctypes.memmove(pointer, data, len(data))
                finally:
                    kernel.GlobalUnlock(handle)
            for _ in range(10):
                if user.OpenClipboard(window):
                    opened = True
                    break
                time.sleep(.03)
            if not opened or not user.EmptyClipboard():
                raise ValueError("The clipboard is busy. Try Copy again.")
            for entry in allocated:
                if not entry[0] or not user.SetClipboardData(entry[0], entry[1]):
                    raise ValueError("Copy to clipboard failed. Try Copy to folder instead.")
                entry[1] = None
        finally:
            if opened:
                user.CloseClipboard()
            for _, handle in allocated:
                if handle:
                    kernel.GlobalFree(handle)
            user.DestroyWindow(window)
