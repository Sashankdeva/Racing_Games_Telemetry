"""Filesystem locations for user data.

Everything the user can change (settings, profiles, logs) lives under
%APPDATA%\\RacingHapticEngine so the install directory stays read-only.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "RacingHapticEngine"


def data_dir() -> Path:
    """Root directory for user data. Honors RHE_DATA_DIR for tests."""
    override = os.environ.get("RHE_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(base) / APP_NAME


def profiles_dir() -> Path:
    return data_dir() / "profiles"


def logs_dir() -> Path:
    return data_dir() / "logs"


def settings_file() -> Path:
    return data_dir() / "settings.json"


def ensure_dirs() -> None:
    for path in (data_dir(), profiles_dir(), logs_dir()):
        path.mkdir(parents=True, exist_ok=True)
