"""Stable per-user paths shared by packaged and ordinary Windows launches."""

import os
from pathlib import Path


def user_directory() -> Path:
    # MSIX redirects AppData writes. A profile directory outside AppData is shared
    # by the desktop launcher and a server started from a packaged coding app.
    return Path.home() / ".framekeep"


def runtime_directory() -> Path:
    return Path(os.environ.get("FRAMEKEEP_RUNTIME_DIR", user_directory() / "runtime"))


def data_directory() -> Path:
    return Path(os.environ.get("FRAMEKEEP_DATA", user_directory() / "data"))
