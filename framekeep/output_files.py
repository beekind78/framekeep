"""Validate output components and pin Windows directories while copying."""

from __future__ import annotations

import ctypes
import os
import stat
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from .source_files import _opened_path, is_link


@contextmanager
def _pin_directory(path: Path, parent_fd=None):
    if parent_fd is None and (is_link(path) or not path.is_dir()):
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
        descriptor = os.open(
            path if parent_fd is None else path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
        )
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ValueError("Output is not a directory.")
    try:
        stream = SimpleNamespace(fileno=lambda: descriptor)
        if _opened_path(stream, path) != path:
            raise ValueError("Output folder resolves outside the expected destination.")
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def pinned_directory(root: Path | ApprovedOutput, relative: Path = Path()):
    if relative.anchor or any(part in ("..", ".") for part in relative.parts):
        raise ValueError("Invalid output subfolder.")
    approved = root if isinstance(root, ApprovedOutput) else None
    root = approved.path if approved else root.absolute()
    with ExitStack() as stack:
        current = Path(root.anchor)
        fd = stack.enter_context(_pin_directory(current))
        for part in root.parts[1:]:
            parent_fd = None if sys.platform == "win32" else fd
            current /= part
            fd = stack.enter_context(_pin_directory(current, parent_fd))
        if approved and not approved.matches(os.fstat(fd)):
            raise ValueError("The output folder changed. Choose it again.")
        for part in relative.parts:
            parent_fd = None if sys.platform == "win32" else fd
            current /= part
            try:
                os.mkdir(current if parent_fd is None else part, dir_fd=parent_fd)
            except FileExistsError:
                pass
            if parent_fd is not None and stat.S_ISLNK(os.stat(part, dir_fd=parent_fd, follow_symlinks=False).st_mode):
                raise ValueError("Output folders cannot be links, junctions or non-folder files.")
            fd = stack.enter_context(_pin_directory(current, parent_fd))
        yield current, fd


@contextmanager
def output_directory(root: Path | ApprovedOutput, relative: Path = Path()):
    with pinned_directory(root, relative) as (current, _):
        yield current


@dataclass(frozen=True)
class ApprovedOutput:
    path: Path
    device: int
    inode: int

    @classmethod
    def inspect(cls, path: Path):
        with pinned_directory(path) as (current, fd):
            info = os.fstat(fd)
            return cls(current, info.st_dev, info.st_ino)

    def matches(self, info):
        return (info.st_dev, info.st_ino) == (self.device, self.inode)


def new_output_folder(root: ApprovedOutput, name: str) -> ApprovedOutput:
    with pinned_directory(root) as (parent, fd):
        target = parent / name
        dir_fd = None if sys.platform == "win32" else fd
        os.mkdir(target if dir_fd is None else name, dir_fd=dir_fd)
        with _pin_directory(target, dir_fd) as child_fd:
            info = os.fstat(child_fd)
            return ApprovedOutput(target, info.st_dev, info.st_ino)


def remove_empty_output_folder(root: ApprovedOutput, folder: ApprovedOutput):
    with pinned_directory(root) as (parent, fd):
        if folder.path.parent != parent:
            raise ValueError("The output folder changed. Choose it again.")
        dir_fd = None if sys.platform == "win32" else fd
        name = folder.path if dir_fd is None else folder.path.name
        if folder.matches(os.stat(name, dir_fd=dir_fd, follow_symlinks=False)):
            os.rmdir(name, dir_fd=dir_fd)


@contextmanager
def new_output_file(root: Path | ApprovedOutput, relative: Path, *, first_suffix=None, csv_text=False):
    with pinned_directory(root, relative.parent) as (parent, fd), ExitStack() as stack:
        target = original = parent / relative.name
        dir_fd = None if sys.platform == "win32" else fd
        mode = "x" if csv_text else "x+b"
        options = {"newline": "", "encoding": "utf-8-sig"} if csv_text else {}
        number = first_suffix
        while True:
            name = target if dir_fd is None else target.name
            try:
                stream = stack.enter_context(
                    open(name, mode, opener=lambda path, flags: os.open(path, flags, dir_fd=dir_fd), **options)
                )
                break
            except FileExistsError:
                if number is None:
                    raise
                target = original.with_name(f"{original.stem} ({number}){original.suffix}")
                number += 1
        opened = os.fstat(stream.fileno())
        try:
            check_output_stream(stream, target)
            yield target, stream
            stream.flush()
            check_output_stream(stream, target)
        except BaseException:
            try:
                if os.path.samestat(opened, os.stat(name, dir_fd=dir_fd, follow_symlinks=False)):
                    # Windows permits deletion only after the write handle closes.
                    try:
                        stream.close()
                    finally:
                        os.unlink(name, dir_fd=dir_fd)
            except OSError:
                pass
            raise


def check_output_stream(stream, expected: Path):
    if _opened_path(stream, expected) != expected:
        raise ValueError("Output file is outside its expected destination.")
