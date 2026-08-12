"""Application settings and their persistence.

Distinct from profiles: this is *how the app runs* (ports, indices, update
rates, window behaviour), while a profile is *how the haptics feel*. Users
switch profiles constantly and settings almost never, so they are stored
separately.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from app.core.logging import get_logger
from app.core.paths import ensure_dirs, settings_file

_log = get_logger(__name__)

# XInput exposes exactly four controller slots.
MAX_CONTROLLER_INDEX = 3
DEFAULT_UDP_PORT = 20777


@dataclass(slots=True)
class AppSettings:
    # --- general ----------------------------------------------------------
    start_minimized: bool = False
    minimize_to_tray: bool = True
    start_engine_on_launch: bool = True
    active_profile: str = "default"

    # --- controller -------------------------------------------------------
    controller_index: int = 0
    auto_detect_controller: bool = True
    #: Hard ceiling on motor output, applied last in the chain.
    master_output_limit: float = 1.0

    # --- telemetry --------------------------------------------------------
    game_id: str = "f1"
    udp_port: int = DEFAULT_UDP_PORT
    #: Seconds of silence before telemetry is considered lost.
    telemetry_timeout: float = 0.5
    #: Seconds without packets before the UI reports "no data".
    connection_timeout: float = 2.0
    auto_start_telemetry: bool = True
    packet_diagnostics: bool = False

    # --- haptics ----------------------------------------------------------
    update_rate_hz: float = 120.0

    # --- advanced ---------------------------------------------------------
    verbose_logging: bool = False
    show_advanced_effect_controls: bool = False

    def clamped(self) -> "AppSettings":
        self.controller_index = int(_clamp(self.controller_index, 0, MAX_CONTROLLER_INDEX))
        self.master_output_limit = _clamp(self.master_output_limit, 0.1, 1.0)
        self.udp_port = int(_clamp(self.udp_port, 1024, 65535))
        self.telemetry_timeout = _clamp(self.telemetry_timeout, 0.1, 10.0)
        self.connection_timeout = _clamp(self.connection_timeout, 0.5, 30.0)
        self.update_rate_hz = _clamp(self.update_rate_hz, 30.0, 250.0)
        return self

    # --- persistence ------------------------------------------------------
    def save(self, path: Path | None = None) -> bool:
        target = Path(path) if path else settings_file()
        temp = target.with_suffix(".json.tmp")
        try:
            ensure_dirs()
            target.parent.mkdir(parents=True, exist_ok=True)
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(asdict(self), handle, indent=2)
            os.replace(temp, target)
        except OSError as exc:
            _log.error("Could not save settings: %s", exc)
            temp.unlink(missing_ok=True)
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

        valid = {f.name: f.type for f in fields(cls)}
        for key, value in data.items():
            if key not in valid:
                continue  # forward-compatible: ignore unknown keys
            current = getattr(settings, key)
            try:
                if isinstance(current, bool):
                    setattr(settings, key, bool(value))
                elif isinstance(current, int):
                    setattr(settings, key, int(value))
                elif isinstance(current, float):
                    setattr(settings, key, float(value))
                else:
                    setattr(settings, key, str(value))
            except (TypeError, ValueError):
                _log.debug("Ignoring bad settings value for %s", key)

        return settings.clamped()


def _clamp(value, low, high):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return low
    if value != value:
        return low
    return low if value < low else high if value > high else value
