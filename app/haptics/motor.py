"""Physical response model for one ERM vibration motor.

The job here is *not* to smooth the signal. A real ERM already low-passes
whatever it is given through rotor inertia, so adding a heavy software
filter on top just makes the controller feel late and mushy. What software
must fix is the part the motor gets wrong on its own:

  1. Dead zone. Below roughly 0.15 drive the rotor never breaks static
     friction, so a naive 0..1 scale wastes its bottom sixth on silence and
     makes every subtle effect vanish. `min_effective` maps the usable
     range onto real motion.
  2. Perceptual curve. Felt strength is not linear in drive, so a mid-range
     command feels weaker than it should without a corrective gamma.
  3. Genuinely impossible steps. A single-tick 0->1->0 spike is wasted
     energy the rotor cannot reproduce. A *light* slew removes those while
     leaving real transients intact.

Default slew rates let a full-scale step complete in ~17 ms, which is far
faster than the motor's own ~50 ms spin-up - so gear shifts and collisions
pass through essentially untouched.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.haptics.signal import clamp

#: Below this the motor is commanded to a hard zero rather than left humming.
SILENCE_EPSILON = 0.004


@dataclass(slots=True)
class MotorConfig:
    """Tunable physical characteristics. Exposed under Settings > Advanced."""

    #: Drive level at which the rotor actually starts turning.
    min_effective: float = 0.16
    #: Ceiling applied before the global output limit.
    max_output: float = 1.0
    #: Perceptual gamma; <1 lifts the low end where ERMs feel weakest.
    curve: float = 0.85
    #: Slew in drive-units per second. High by design - see module docstring.
    slew_rise: float = 60.0
    slew_fall: float = 35.0
    #: Escape hatch for users who want the rawest possible response.
    slew_enabled: bool = True

    def clamped(self) -> "MotorConfig":
        return MotorConfig(
            min_effective=clamp(self.min_effective, 0.0, 0.6),
            max_output=clamp(self.max_output, 0.1, 1.0),
            curve=clamp(self.curve, 0.3, 3.0),
            slew_rise=clamp(self.slew_rise, 1.0, 500.0),
            slew_fall=clamp(self.slew_fall, 1.0, 500.0),
            slew_enabled=self.slew_enabled,
        )


class Motor:
    """Converts a 0..1 haptic intent into a 0..1 motor drive level."""

    __slots__ = ("config", "_drive", "_intent")

    def __init__(self, config: MotorConfig | None = None) -> None:
        self.config = (config or MotorConfig()).clamped()
        self._drive = 0.0
        self._intent = 0.0

    @property
    def drive(self) -> float:
        """Current commanded drive level (what the hardware receives)."""
        return self._drive

    @property
    def intent(self) -> float:
        """Last requested intensity, before the physical model."""
        return self._intent

    def set_config(self, config: MotorConfig) -> None:
        self.config = config.clamped()

    def reset(self) -> None:
        self._drive = 0.0
        self._intent = 0.0

    def snap_to_zero(self) -> None:
        """Hard stop that bypasses slew - used by emergency stop/shutdown."""
        self._drive = 0.0
        self._intent = 0.0

    def update(self, intensity: float, dt: float) -> float:
        """Map `intensity` through the motor model and advance by `dt`."""
        cfg = self.config
        intensity = clamp(intensity)
        self._intent = intensity

        if intensity <= SILENCE_EPSILON:
            target = 0.0
        else:
            shaped = intensity ** cfg.curve
            # Lift onto the range where the rotor actually spins.
            target = cfg.min_effective + shaped * (cfg.max_output - cfg.min_effective)
            target = clamp(target, 0.0, cfg.max_output)

        if not cfg.slew_enabled or dt <= 0.0:
            self._drive = target
            return self._drive

        delta = target - self._drive
        if delta > 0.0:
            self._drive += min(delta, cfg.slew_rise * dt)
        elif delta < 0.0:
            self._drive -= min(-delta, cfg.slew_fall * dt)

        # Snap the last sliver so we always settle exactly on silence.
        if target == 0.0 and self._drive < SILENCE_EPSILON:
            self._drive = 0.0

        return self._drive
