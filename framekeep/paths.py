"""Stable per-user paths shared by packaged and ordinary Windows launches."""

import os
import shutil
from pathlib import Path


def user_directory() -> Path:
    # MSIX redirects AppData writes. A profile directory outside AppData is shared
    # by the desktop launcher and a server started from a packaged coding app.
    return Path.home() / ".framekeep"


def runtime_directory() -> Path:
    return Path(os.environ.get("FRAMEKEEP_RUNTIME_DIR", user_directory() / "runtime"))


def data_directory() -> Path:
    return Path(os.environ.get("FRAMEKEEP_DATA", user_directory() / "data"))


def protect_default_data(path: Path) -> None:
    if os.name == "nt" or "FRAMEKEEP_DATA" in os.environ:
        return
    for directory in (path.parent, path):
        directory.mkdir(exist_ok=True, mode=0o700)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(fd).st_uid != os.getuid():
                raise ValueError("Framekeep's data folder must belong to this user.")
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)


def recovery_directory() -> Path:
    return Path.home() / "Framekeep Recovered"


def ffmpeg_executable() -> str | None:
    if os.name == "nt":
        managed = user_directory() / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
        try:
            if managed.resolve(strict=True) == managed and managed.is_file():
                return str(managed)
        except (OSError, RuntimeError):
            pass
    return shutil.which("ffmpeg")
