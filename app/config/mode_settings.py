"""Per-game-mode settings.

Split deliberately in two:

  AppSettings   global - which mode is active, window behaviour, logging
  ModeSettings  per mode - telemetry transport, units, UI and future
                coaching/strategy preferences

Each mode's settings live in their own file, so switching F1 25 -> F1 26
cannot overwrite the F1 25 configuration and switching back restores it
exactly. That is a storage guarantee, not a convention.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from app.core.logging import get_logger
from app.core.paths import data_dir, ensure_dirs, write_atomic
from app.games.modes import GameMode

_log = get_logger(__name__)

DEFAULT_UDP_PORT = 20777


@dataclass(slots=True)
class ModeSettings:
    """Settings that belong to one game mode."""

    mode: str = GameMode.F1_25.value

    # --- telemetry transport ---
    udp_port: int = DEFAULT_UDP_PORT
    telemetry_timeout: float = 2.0
    connection_timeout: float = 2.0
    auto_start_telemetry: bool = True

    # --- presentation ---
    #: "metric" (kph, C, kg) or "imperial" (mph, F, lb).
    units: str = "metric"
    #: Dashboard layout preset; future layouts slot in here per mode.
    dashboard_layout: str = "default"
    show_unconfirmed_fields: bool = True

    # --- ERS, per mode ------------------------------------------------
    #: Preferred deploy mode name; validated against the game's ERS config
    #: so an F1 25 mode name cannot leak into F1 26.
    ers_default_mode: str = "Medium"
    #: Reserve kept in hand rather than deployed, 0-1.
    ers_reserve: float = 0.15
    ers_auto_manage: bool = True

    # --- DRS / straight-line aid, per mode -----------------------------
    #: Warn when the gap ahead drops under the activation threshold.
    drs_alert_enabled: bool = True
    #: Seconds of gap at which to start alerting.
    drs_alert_gap_s: float = 1.2
    #: 2026 active aero: preferred wing state when not overriding.
    aero_default_mode: str = ""

    # --- future engines, stored now so a mode switch preserves them ---
    coaching_sensitivity: float = 0.5
    strategy_aggression: float = 0.5
    #: Preferred car/track selection for this mode.
    selected_car: str = "generic"
    selected_track: str = "generic"

    def clamped(self) -> "ModeSettings":
        self.udp_port = int(_clamp(self.udp_port, 1024, 65535))
        self.telemetry_timeout = _clamp(self.telemetry_timeout, 0.1, 10.0)
        self.connection_timeout = _clamp(self.connection_timeout, 0.5, 30.0)
        self.coaching_sensitivity = _clamp(self.coaching_sensitivity, 0.0, 1.0)
        self.strategy_aggression = _clamp(self.strategy_aggression, 0.0, 1.0)
        if self.units not in ("metric", "imperial"):
            self.units = "metric"
        self.ers_reserve = _clamp(self.ers_reserve, 0.0, 0.9)
        self.drs_alert_gap_s = _clamp(self.drs_alert_gap_s, 0.1, 5.0)
        return self

    def validate_against(self, profile) -> "ModeSettings":
        """Reconcile stored choices with the active game's configuration.

        A deploy mode saved under one title may not exist in the other, and
        active aero only exists in 2026. Rather than silently keeping a
        value the game has no concept of, fall back to that game's own
        default.
        """
        if self.ers_default_mode not in profile.ers.modes:
            modes = profile.ers.modes
            self.ers_default_mode = modes[len(modes) // 2] if modes else ""

        if profile.drs.has_active_aero and profile.drs.aero_modes:
            if self.aero_default_mode not in profile.drs.aero_modes:
                self.aero_default_mode = profile.drs.aero_modes[0]
        else:
            self.aero_default_mode = ""  # no active aero in this title

        if not profile.drs.has_drs and not profile.drs.has_manual_override:
            self.drs_alert_enabled = False
        return self

    # --- persistence ------------------------------------------------------
    @staticmethod
    def path_for(mode: GameMode) -> Path:
        return data_dir() / "modes" / f"{mode.value}.json"

    def save(self, path: Path | None = None) -> bool:
        target = Path(path) if path else self.path_for(GameMode.parse(self.mode))
        try:
            ensure_dirs()
            write_atomic(target, json.dumps(asdict(self), indent=2))
        except OSError as exc:
            _log.error("Could not save %s settings: %s", self.mode, exc)
            return False
        return True

    @classmethod
    def load(cls, mode: GameMode, path: Path | None = None) -> "ModeSettings":
        """Load one mode's settings, defaulting anything unusable."""
        source = Path(path) if path else cls.path_for(mode)
        settings = cls(mode=mode.value)
        settings.udp_port = _default_port_for(mode)

        if not source.exists():
            return settings.clamped()

        try:
            with source.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("%s settings unreadable (%s); using defaults", mode.value, exc)
            return settings.clamped()

        if not isinstance(data, dict):
            return settings.clamped()

        valid = {f.name for f in fields(cls)}
        for key, value in data.items():
            if key not in valid:
                continue  # forward-compatible
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
                _log.debug("Ignoring bad %s value for %s", mode.value, key)

        # The file must never be able to claim a different mode than the
        # one it is stored under.
        settings.mode = mode.value
        return settings.clamped()


def _default_port_for(mode: GameMode) -> int:
    from app.games.modes import game_profile

    return game_profile(mode).default_port


def _clamp(value, low, high):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return low
    if math.isnan(value):  # NaN fails every comparison below
        return low
    return low if value < low else high if value > high else value
