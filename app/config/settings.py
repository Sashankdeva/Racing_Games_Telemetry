"""Global application settings.

Deliberately small: only things that are genuinely not per-game live here.
Everything a game mode can differ on - ports, units, layout, coaching and
strategy preferences - belongs in ModeSettings so switching modes cannot
clobber the other one's configuration.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from app.core.logging import get_logger
from app.core.paths import ensure_dirs, settings_file, write_atomic
from app.games.modes import GameMode

_log = get_logger(__name__)


@dataclass(slots=True)
class AppSettings:
    #: Which game mode is active. Per-mode settings live beside this.
    game_mode: str = GameMode.F1_25.value
    #: Which telemetry adapter to use (F1, and later others).
    game_id: str = "f1"

    start_minimized: bool = False
    minimize_to_tray: bool = True
    verbose_logging: bool = False

    @property
    def mode(self) -> GameMode:
        return GameMode.parse(self.game_mode)

    def clamped(self) -> "AppSettings":
        self.game_mode = self.mode.value
        return self

    # --- persistence ------------------------------------------------------
    def save(self, path: Path | None = None) -> bool:
        target = Path(path) if path else settings_file()
        try:
            ensure_dirs()
            write_atomic(target, json.dumps(asdict(self), indent=2))
        except OSError as exc:
            _log.error("Could not save settings: %s", exc)
            return False
        return True

    @classmethod
    def load(cls, path: Path | None = None) -> "AppSettings":
        """Load settings, falling back to defaults for anything unusable."""
        source = Path(path) if path else settings_file()
        settings = cls()
        if not source.exists():
            return settings.clamped()

        try:
            with source.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("Settings unreadable (%s); using defaults", exc)
            return settings.clamped()

        if not isinstance(data, dict):
            return settings.clamped()

        valid = {f.name for f in fields(cls)}
        for key, value in data.items():
            if key not in valid:
                continue  # forward-compatible: ignore unknown keys
            current = getattr(settings, key)
            try:
                if isinstance(current, bool):
                    setattr(settings, key, bool(value))
                else:
                    setattr(settings, key, str(value))
            except (TypeError, ValueError):
                _log.debug("Ignoring bad settings value for %s", key)

        return settings.clamped()
