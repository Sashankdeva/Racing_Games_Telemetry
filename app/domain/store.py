"""Editable JSON-backed record store.

Shared by the car and track databases. Built-in records live in code;
editing one writes a file that shadows it, and "reset" deletes that file so
the shipped values come back. That guarantees a known-good baseline is
always one click away no matter what the user has changed.

Writes go through a temporary file and an atomic replace, so a crash
mid-save cannot leave a truncated record behind. Loading is deliberately
tolerant: unknown keys are ignored and missing keys fall back to defaults,
so a file written by a newer build still loads.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Callable, Generic, TypeVar

from app.core.logging import get_logger

_log = get_logger(__name__)

T = TypeVar("T")


class RecordStore(Generic[T]):
    """A directory of JSON records keyed by a stable id."""

    def __init__(
        self,
        directory: Path,
        builtins: Callable[[], list[T]],
        key_of: Callable[[T], str],
        from_dict: Callable[[dict], T],
        to_dict: Callable[[T], dict],
    ) -> None:
        self._dir = Path(directory)
        self._builtins = builtins
        self._key_of = key_of
        self._from_dict = from_dict
        self._to_dict = to_dict
        self._records: dict[str, T] = {}
        self._lock = threading.RLock()
        self.reload()

    # ------------------------------------------------------------------
    def reload(self) -> None:
        with self._lock:
            self._records = {self._key_of(r): r for r in self._builtins()}

            try:
                self._dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                _log.warning("Cannot access %s: %s", self._dir, exc)
                return

            for path in sorted(self._dir.glob("*.json")):
                record = self._load_file(path)
                if record is not None:
                    # A saved file shadows the built-in of the same key.
                    self._records[self._key_of(record)] = record

    def _load_file(self, path: Path) -> T | None:
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            return self._from_dict(data)
        except (OSError, json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
            _log.warning("Skipping unreadable record %s: %s", path.name, exc)
            return None

    # ------------------------------------------------------------------
    @property
    def all(self) -> list[T]:
        with self._lock:
            return list(self._records.values())

    def get(self, key: str) -> T | None:
        with self._lock:
            return self._records.get(key)

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._records)

    def save(self, record: T) -> bool:
        key = self._key_of(record)
        path = self._dir / f"{key}.json"
        temp = path.with_suffix(".json.tmp")
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(self._to_dict(record), handle, indent=2)
            os.replace(temp, path)  # atomic on Windows and POSIX
        except OSError as exc:
            _log.error("Failed to save %s: %s", key, exc)
            temp.unlink(missing_ok=True)
            return False

        with self._lock:
            self._records[key] = record
        return True

    def reset(self, key: str) -> bool:
        """Delete the user's override so the shipped record returns."""
        try:
            (self._dir / f"{key}.json").unlink(missing_ok=True)
        except OSError as exc:
            _log.error("Failed to reset %s: %s", key, exc)
            return False

        with self._lock:
            for original in self._builtins():
                if self._key_of(original) == key:
                    self._records[key] = original
                    return True
            self._records.pop(key, None)
        return True

    def is_customised(self, key: str) -> bool:
        return (self._dir / f"{key}.json").exists()


def dataclass_from_dict(cls, data: dict):
    """Build a dataclass from a dict, ignoring unknown keys and coercing
    types where it can. Anything unusable falls back to the default."""
    if not isinstance(data, dict):
        raise ValueError("record must be an object")
    if not is_dataclass(cls):
        raise TypeError(f"{cls} is not a dataclass")

    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        try:
            if f.type in ("float", float):
                kwargs[f.name] = float(value)
            elif f.type in ("int", int):
                kwargs[f.name] = int(value)
            elif f.type in ("bool", bool):
                kwargs[f.name] = bool(value)
            elif f.type in ("str", str):
                kwargs[f.name] = str(value)
            else:
                kwargs[f.name] = value
        except (TypeError, ValueError):
            continue  # keep the default
    return cls(**kwargs)
