"""Profile storage and CRUD.

One JSON file per profile under %APPDATA%\\RacingHapticEngine\\profiles.
Built-in profiles live in code; saving an edited built-in writes a file
that shadows it, and resetting deletes that file to restore the original.

Writes go through a temporary file and an atomic replace, so a crash or
power loss mid-save cannot leave a truncated profile behind.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from app.core.events import Event, EventBus
from app.core.logging import get_logger
from app.core.paths import ensure_dirs, profiles_dir
from app.profiles.schema import Profile, builtin_profiles, slugify

_log = get_logger(__name__)


class ProfileManager:
    def __init__(self, bus: EventBus | None = None, directory: Path | None = None) -> None:
        self.bus = bus or EventBus()
        self._dir = directory or profiles_dir()
        self._lock = threading.RLock()
        self._profiles: dict[str, Profile] = {}
        self._active_slug: str = ""
        self.load_all()

    # ------------------------------------------------------------------
    # loading
    # ------------------------------------------------------------------
    def load_all(self) -> None:
        with self._lock:
            self._profiles = {}

            for profile in builtin_profiles():
                self._profiles[profile.slug] = profile

            try:
                ensure_dirs()
                self._dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                _log.warning("Cannot access profile directory: %s", exc)
                self._ensure_active()
                return

            for path in sorted(self._dir.glob("*.json")):
                profile = self._load_file(path)
                if profile is None:
                    continue
                # A saved file shadows the built-in of the same slug.
                existing = self._profiles.get(profile.slug)
                profile.builtin = bool(existing and existing.builtin)
                self._profiles[profile.slug] = profile

            self._ensure_active()

    def _load_file(self, path: Path) -> Profile | None:
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("Skipping unreadable profile %s: %s", path.name, exc)
            return None
        try:
            return Profile.from_dict(data)
        except (ValueError, TypeError) as exc:
            _log.warning("Skipping invalid profile %s: %s", path.name, exc)
            return None

    def _ensure_active(self) -> None:
        if self._active_slug in self._profiles:
            return
        self._active_slug = next(iter(self._profiles), "")

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    @property
    def profiles(self) -> list[Profile]:
        with self._lock:
            return list(self._profiles.values())

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.profiles]

    @property
    def active(self) -> Profile:
        with self._lock:
            profile = self._profiles.get(self._active_slug)
            if profile is None:
                profile = next(iter(self._profiles.values()))
                self._active_slug = profile.slug
            return profile

    @property
    def active_slug(self) -> str:
        return self._active_slug

    def get(self, slug: str) -> Profile | None:
        with self._lock:
            return self._profiles.get(slug)

    def by_name(self, name: str) -> Profile | None:
        return self.get(slugify(name))

    def exists(self, name: str) -> bool:
        return slugify(name) in self._profiles

    # ------------------------------------------------------------------
    # mutations
    # ------------------------------------------------------------------
    def set_active(self, slug: str) -> Profile:
        with self._lock:
            if slug in self._profiles:
                self._active_slug = slug
            profile = self.active
        self.bus.emit(Event.PROFILE_CHANGED, profile=profile)
        return profile

    def save(self, profile: Profile) -> bool:
        """Persist a profile, replacing any existing file for its slug."""
        profile.normalize()
        path = self._dir / f"{profile.slug}.json"
        temp = path.with_suffix(".json.tmp")

        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(profile.to_dict(), handle, indent=2)
            os.replace(temp, path)  # atomic on Windows and POSIX
        except OSError as exc:
            _log.error("Failed to save profile %s: %s", profile.name, exc)
            temp.unlink(missing_ok=True)
            return False

        with self._lock:
            self._profiles[profile.slug] = profile
        _log.info("Saved profile '%s'", profile.name)
        self.bus.emit(Event.PROFILE_SAVED, profile=profile)
        return True

    def create(self, name: str, based_on: Profile | None = None) -> Profile | None:
        name = name.strip()
        if not name or self.exists(name):
            return None
        source = based_on or self.active
        profile = source.copy(new_name=name)
        profile.description = f"Copy of {source.name}" if based_on else "Custom profile"
        return profile if self.save(profile) else None

    def duplicate(self, slug: str) -> Profile | None:
        source = self.get(slug)
        if source is None:
            return None
        name = self._unique_name(f"{source.name} Copy")
        return self.create(name, based_on=source)

    def rename(self, slug: str, new_name: str) -> Profile | None:
        new_name = new_name.strip()
        source = self.get(slug)
        if source is None or not new_name or self.exists(new_name):
            return None

        renamed = source.copy(new_name=new_name)
        renamed.description = source.description
        if not self.save(renamed):
            return None

        was_active = self._active_slug == slug
        # Built-ins keep existing under their own slug; only user profiles move.
        if not source.builtin:
            self.delete(slug, emit=False)
        if was_active:
            self.set_active(renamed.slug)
        return renamed

    def delete(self, slug: str, emit: bool = True) -> bool:
        """Remove a user profile, or reset a built-in to its shipped values."""
        with self._lock:
            profile = self._profiles.get(slug)
            if profile is None:
                return False
            builtin = profile.builtin

        path = self._dir / f"{slug}.json"
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            _log.error("Failed to delete profile file %s: %s", path.name, exc)
            return False

        with self._lock:
            if builtin:
                # Restore the code-defined version rather than removing it.
                for original in builtin_profiles():
                    if original.slug == slug:
                        self._profiles[slug] = original
                        break
            else:
                self._profiles.pop(slug, None)
            self._ensure_active()

        _log.info("%s profile '%s'", "Reset" if builtin else "Deleted", slug)
        if emit:
            self.bus.emit(Event.PROFILE_CHANGED, profile=self.active)
        return True

    def reset(self, slug: str) -> bool:
        return self.delete(slug)

    # ------------------------------------------------------------------
    # import / export
    # ------------------------------------------------------------------
    def export(self, slug: str, destination: Path) -> bool:
        profile = self.get(slug)
        if profile is None:
            return False
        try:
            with Path(destination).open("w", encoding="utf-8") as handle:
                json.dump(profile.to_dict(), handle, indent=2)
        except OSError as exc:
            _log.error("Export failed: %s", exc)
            return False
        _log.info("Exported profile '%s' to %s", profile.name, destination)
        return True

    def import_file(self, source: Path) -> Profile | None:
        try:
            with Path(source).open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            profile = Profile.from_dict(data)
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            _log.error("Import failed: %s", exc)
            return None

        if self.exists(profile.name):
            profile.name = self._unique_name(profile.name)
        profile.builtin = False
        return profile if self.save(profile) else None

    def _unique_name(self, base: str) -> str:
        name = base
        index = 2
        while self.exists(name):
            name = f"{base} {index}"
            index += 1
        return name
