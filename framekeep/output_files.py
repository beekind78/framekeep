"""Validate output components and pin Windows directories while copying."""

import ctypes
import os
import stat
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace

from .source_files import _opened_path, is_link


@contextmanager
def _pin_directory(path: Path):
    if is_link(path) or not path.is_dir():
        raise ValueError("Output folders cannot be links, junctions or non-folder files.")
    if sys.platform == "win32":
        import msvcrt
        from ctypes import wintypes

        class Attributes(ctypes.Structure):
            _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

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
        kernel.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        # Omit FILE_SHARE_DELETE to prevent renaming pinned output directories.
        handle = kernel.CreateFileW(str(path), 0, 3, None, 3, 0x02000000 | 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            attributes = Attributes()
            if not kernel.GetFileInformationByHandleEx(
                handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if attributes.attributes & 0x400 or not attributes.attributes & 0x10:
                raise ValueError("Output folder changed into a link or another file type.")
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
    else:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ValueError("Output is not a directory.")
    try:
        stream = SimpleNamespace(fileno=lambda: descriptor)
        if _opened_path(stream, path) != path:
            raise ValueError("Output folder resolves outside the expected destination.")
        yield path
    finally:
        os.close(descriptor)


@contextmanager
def output_directory(root: Path, relative: Path = Path()):
    if relative.is_absolute() or any(part in ("..", ".") for part in relative.parts):
        raise ValueError("Invalid output subfolder.")
    canonical = root.resolve(strict=True)
    with ExitStack() as stack:
        stack.enter_context(_pin_directory(canonical))
        current = canonical
        for part in relative.parts:
            current /= part
            current.mkdir(exist_ok=True)
            stack.enter_context(_pin_directory(current))
        yield current


def check_output_stream(stream, expected: Path):
    if _opened_path(stream, expected) != expected:
        raise ValueError("Output file is outside its expected destination.")
