"""Filesystem locations for user data.

Everything the user can change (settings, profiles, logs) lives under
%APPDATA%\\F1RaceEngineer so the install directory stays read-only.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path

APP_NAME = "F1RaceEngineer"


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


#: Serialises this process's access to the user's data files.
#:
#: Sessions and observed profiles are written by the telemetry thread on
#: lap completion and read by the UI thread as it renders. On Windows an
#: `os.replace` over a file another handle has open fails outright with
#: PermissionError, so an unguarded read during a save does not merely
#: interleave - it loses the save. The files are a few KB and written
#: about once a lap, so one lock over all of it costs nothing measurable
#: and removes the whole class of race.
_FILE_LOCK = threading.RLock()

#: Windows also lets a virus scanner or indexer hold a just-written file
#: briefly. That is transient, so retry a couple of times before failing.
_REPLACE_ATTEMPTS = 3
_REPLACE_BACKOFF_S = 0.02


def write_atomic(path: Path, text: str) -> None:
    """Write `text` to `path` so a reader never sees a half-written file.

    The temp file carries a unique suffix: a shared `<name>.tmp` looks
    atomic but is not, since two writers would share one scratch file and
    race to rename it.

    Raises OSError; callers decide whether a failed save is fatal.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        with _FILE_LOCK:
            for attempt in range(_REPLACE_ATTEMPTS):
                try:
                    os.replace(temp, path)
                    return
                except PermissionError:
                    if attempt == _REPLACE_ATTEMPTS - 1:
                        raise
                    time.sleep(_REPLACE_BACKOFF_S)
    except OSError:
        # Never leave the scratch file behind to be mistaken for data.
        temp.unlink(missing_ok=True)
        raise


def read_text(path: Path) -> str:
    """Read a data file under the same guard `write_atomic` writes under.

    Readers must take the lock too, or a save landing mid-read fails.
    """
    with _FILE_LOCK:
        return path.read_text(encoding="utf-8")
