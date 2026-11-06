"""Constrain every original-file read to its registered physical source tree."""

from __future__ import annotations

import ctypes
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path


def is_link(path: Path) -> bool:
    return _is_link(path.lstat())


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def file_identity(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def require_file_identity(stream, expected) -> None:
    if expected is not None and file_identity(os.fstat(stream.fileno())) != tuple(expected):
        raise ValueError("File changed during indexing; rescan to retry.")


def source_entries(root: Path, onerror, skipped, cancelled):
    from .output_files import pinned_directory

    pending = [root]
    while pending and not cancelled():
        directory = pending.pop()
        try:
            with pinned_directory(directory) as (physical, fd), os.scandir(
                physical if sys.platform == "win32" else fd
            ) as entries:
                for entry in entries:
                    if cancelled():
                        return
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError as error:
                        onerror(str(error))
                        continue
                    if _is_link(info):
                        skipped(f"Skipped linked file or folder: {entry.name}")
                    elif stat.S_ISDIR(info.st_mode):
                        pending.append(physical / entry.name)
                    elif stat.S_ISREG(info.st_mode):
                        yield physical / entry.name, info
                    else:
                        skipped(f"Skipped nonregular file: {entry.name}")
        except (OSError, ValueError) as error:
            if directory == root:
                raise
            onerror(str(error))


def source_path(path: Path, root: Path) -> Path:
    if is_link(root):
        raise ValueError("The registered source has become a link or reparse point.")
    root = root.resolve(strict=True)
    absolute = path.absolute()
    try:
        relative = absolute.relative_to(root)
    except ValueError:
        raise ValueError("Photo is outside its registered source folder.") from None
    current = root
    for part in relative.parts:
        current /= part
        if is_link(current):
            raise ValueError("Linked files, junctions, and reparse points are not processed.")
    resolved = absolute.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError("Photo resolves outside its registered source folder.")
    return resolved


def _opened_path(stream, fallback: Path) -> Path:
    if sys.platform == "win32":
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        handle = msvcrt.get_osfhandle(stream.fileno())
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise OSError("Cannot establish the opened photo's physical path.")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)
    descriptor = Path(f"/proc/self/fd/{stream.fileno()}")
    if descriptor.exists():
        return descriptor.resolve(strict=True)
    resolved = fallback.resolve(strict=True)
    if not os.path.samestat(os.fstat(stream.fileno()), resolved.stat()):
        raise ValueError("Photo changed while opening it.")
    return resolved


@contextmanager
def open_source(path: Path, root: Path):
    resolved = source_path(path, root)
    canonical_root = root.absolute()
    if sys.platform == "win32":
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        # Share read only: an original cannot be replaced or written while this handle is used.
        handle = kernel.CreateFileW(str(resolved), 0x80000000, 1, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
    else:
        if not stat.S_ISREG(resolved.lstat().st_mode):
            raise ValueError("Only regular, nonlinked photo files are processed.")
        descriptor = os.open(resolved, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Only regular, nonlinked photo files are processed.")
        actual = _opened_path(stream, resolved)
        if not actual.is_relative_to(canonical_root):
            raise ValueError("Opened photo is outside its registered source folder.")
        yield stream
