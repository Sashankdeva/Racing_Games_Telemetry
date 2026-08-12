"""The normalized telemetry model.

This is the contract between game adapters and the haptic engine. Adapters
translate their game's native packets into a TelemetryFrame; effects read
only TelemetryFrame. Nothing below this line knows what F1 is.

Design rule: fields an adapter cannot source are left as None rather than
guessed. Effects check for None and degrade gracefully instead of reacting
to invented data.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import IntEnum


class SurfaceType(IntEnum):
    """Normalized surface classification under a given wheel."""

    UNKNOWN = -1
    TARMAC = 0
    RUMBLE_STRIP = 1
    CONCRETE = 2
    ROCK = 3
    GRAVEL = 4
    MUD = 5
    SAND = 6
    GRASS = 7
    WATER = 8
    COBBLESTONE = 9
    METAL = 10
    RIDGED = 11

    @property
    def is_kerb(self) -> bool:
        return self in (SurfaceType.RUMBLE_STRIP, SurfaceType.RIDGED)

    @property
    def is_loose(self) -> bool:
        """Loose/low-grip surfaces that produce irregular, noisy vibration."""
        return self in (
            SurfaceType.GRAVEL,
            SurfaceType.SAND,
            SurfaceType.MUD,
            SurfaceType.ROCK,
        )

    @property
    def is_rough_road(self) -> bool:
        """Hard but textured surfaces."""
        return self in (SurfaceType.COBBLESTONE, SurfaceType.METAL, SurfaceType.CONCRETE)


@dataclass(frozen=True, slots=True)
class Wheels:
    """Per-wheel scalar values.

    Always constructed in explicit fl/fr/rl/rr terms. Games use different
    native orderings (F1 uses RL,RR,FL,FR) - adapters must map into these
    named fields so no ordering assumption leaks into the effects.
    """

    fl: float = 0.0
    fr: float = 0.0
    rl: float = 0.0
    rr: float = 0.0

    @property
    def front_avg(self) -> float:
        return (self.fl + self.fr) * 0.5

    @property
    def rear_avg(self) -> float:
        return (self.rl + self.rr) * 0.5

    @property
    def left_avg(self) -> float:
        return (self.fl + self.rl) * 0.5

    @property
    def right_avg(self) -> float:
        return (self.fr + self.rr) * 0.5

    @property
    def avg(self) -> float:
        return (self.fl + self.fr + self.rl + self.rr) * 0.25

    @property
    def max(self) -> float:
        return max(self.fl, self.fr, self.rl, self.rr)

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.fl, self.fr, self.rl, self.rr)


@dataclass(frozen=True, slots=True)
class Surfaces:
    fl: SurfaceType = SurfaceType.UNKNOWN
    fr: SurfaceType = SurfaceType.UNKNOWN
    rl: SurfaceType = SurfaceType.UNKNOWN
    rr: SurfaceType = SurfaceType.UNKNOWN

    def as_tuple(self) -> tuple[SurfaceType, SurfaceType, SurfaceType, SurfaceType]:
        return (self.fl, self.fr, self.rl, self.rr)

    @property
    def left(self) -> tuple[SurfaceType, SurfaceType]:
        return (self.fl, self.rl)

    @property
    def right(self) -> tuple[SurfaceType, SurfaceType]:
        return (self.fr, self.rr)

    def count(self, predicate) -> int:
        return sum(1 for s in self.as_tuple() if predicate(s))


@dataclass(slots=True)
class TelemetryFrame:
    """One sampled instant of vehicle state, normalized across games."""

    # --- meta -------------------------------------------------------------
    timestamp: float = field(default_factory=time.perf_counter)
    game: str = "unknown"
    valid: bool = False
    paused: bool = False
    in_pits: bool = False

    # --- drivetrain -------------------------------------------------------
    speed_kph: float = 0.0
    rpm: float = 0.0
    max_rpm: float = 0.0
    idle_rpm: float = 0.0
    gear: int = 0  # -1 reverse, 0 neutral, 1..n
    throttle: float = 0.0  # 0..1
    brake: float = 0.0  # 0..1
    clutch: float = 0.0  # 0..1
    steering: float = 0.0  # -1 left .. +1 right
    drs_active: bool = False

    # --- per-wheel --------------------------------------------------------
    wheel_speed: Wheels = field(default_factory=Wheels)  # m/s
    wheel_slip_ratio: Wheels = field(default_factory=Wheels)  # ~0 = rolling
    suspension_position: Wheels = field(default_factory=Wheels)
    suspension_velocity: Wheels = field(default_factory=Wheels)
    suspension_acceleration: Wheels = field(default_factory=Wheels)
    surfaces: Surfaces = field(default_factory=Surfaces)

    # --- forces (g) -------------------------------------------------------
    g_lateral: float = 0.0
    g_longitudinal: float = 0.0
    g_vertical: float = 0.0

    # --- derived / assist state ------------------------------------------
    # None means "this game did not tell us", not "false".
    abs_active: bool | None = None
    tc_active: bool | None = None
    rev_limiter_active: bool = False
    # 0..1 impact magnitude for this frame; adapters derive or read directly.
    impact: float = 0.0

    @property
    def rpm_ratio(self) -> float:
        """RPM as a 0..1 fraction of redline. 0 when max_rpm is unknown."""
        if self.max_rpm <= 0.0:
            return 0.0
        return _clamp(self.rpm / self.max_rpm, 0.0, 1.0)

    @property
    def rpm_band(self) -> float:
        """RPM mapped across the *usable* band (idle..redline) as 0..1.

        More useful than rpm_ratio for haptics: an idling engine should sit
        near 0 rather than at whatever fraction idle happens to be.
        """
        if self.max_rpm <= 0.0:
            return 0.0
        low = self.idle_rpm if 0.0 < self.idle_rpm < self.max_rpm else 0.0
        span = self.max_rpm - low
        if span <= 0.0:
            return 0.0
        return _clamp((self.rpm - low) / span, 0.0, 1.0)

    @property
    def speed_ms(self) -> float:
        return self.speed_kph / 3.6

    @property
    def is_moving(self) -> bool:
        return self.speed_kph > 1.0

    def age(self, now: float | None = None) -> float:
        """Seconds since this frame was captured."""
        return (time.perf_counter() if now is None else now) - self.timestamp

    def copy_with(self, **changes) -> "TelemetryFrame":
        return replace(self, **changes)


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


#: A frame representing "no game data". Effects treat this as silence.
NO_TELEMETRY = TelemetryFrame(valid=False)
