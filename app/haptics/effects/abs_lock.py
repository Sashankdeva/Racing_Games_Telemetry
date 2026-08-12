"""ABS and wheel lock - fast, hard, and mechanically irregular.

An ABS system is a pump cycling at a fixed mechanical rate, so the signal
is a hard square gate rather than a smooth wave - a constant buzz here is
the single most common way to get ABS wrong.

Two sources feed it, in priority order:
  * an explicit ABS-active flag from the game (used as-is when present),
  * a negative slip ratio, meaning the wheel is turning slower than the car
    is travelling, which is a lock-up whether or not ABS is fitted.

The pump rate is jittered by a few percent every cycle. A perfectly
periodic gate reads as an electronic tone; real hydraulics never do, and
the small irregularity is what makes it feel like a machine.
"""

from __future__ import annotations

from app.core.models import TelemetryFrame, Wheels
from app.haptics.effects.base import Effect, EffectOutput, EffectSettings, wheels_to_lr
from app.haptics.signal import (
    MAX_USEFUL_MOD_HZ,
    NoiseSource,
    Oscillator,
    Waveform,
    clamp,
    lerp,
)


class AbsLockEffect(Effect):
    id = "abs_lock"
    name = "ABS / Wheel Lock"
    description = "Rapid mechanical pulsing under braking when wheels lock or ABS engages"
    priority = 60
    dominance = 0.6
    sharpness_label = "Pump hardness"

    PUMP_HZ = 16.0
    #: Magnitude of negative slip treated as a full lock.
    FULL_LOCK = 0.35
    #: Brake input below which a lock-up is ignored as noise.
    MIN_BRAKE = 0.05

    def __init__(self, settings: EffectSettings | None = None) -> None:
        super().__init__(settings)
        self._left_osc = Oscillator(Waveform.SQUARE, self.PUMP_HZ, sharpness=1.0, duty=0.5)
        self._right_osc = Oscillator(Waveform.SQUARE, self.PUMP_HZ, sharpness=1.0, duty=0.5)
        self._jitter = NoiseSource(rate_hz=9.0, smooth=True, seed=19)

    def reset(self) -> None:
        super().reset()
        self._left_osc.reset()
        self._right_osc.reset(0.5)

    def generate(
        self, dt: float, telemetry: TelemetryFrame, settings: EffectSettings
    ) -> EffectOutput:
        jitter = self._jitter.update(dt)

        lock = self._lock_amount(telemetry, settings)
        left_lock, right_lock = wheels_to_lr(lock)
        peak = max(left_lock, right_lock)

        if peak <= 0.0 or telemetry.brake < settings.get("min_brake", self.MIN_BRAKE):
            self._left_osc.update(dt)
            self._right_osc.update(dt)
            return EffectOutput()

        base_rate = clamp(settings.get("pump_hz", self.PUMP_HZ), 4.0, MAX_USEFUL_MOD_HZ)
        jitter_depth = clamp(settings.get("jitter", 0.12), 0.0, 0.5)
        rate = clamp(base_rate * (1.0 + (jitter - 0.5) * 2.0 * jitter_depth), 4.0, MAX_USEFUL_MOD_HZ)

        sharpness = clamp(settings.sharpness)
        self._left_osc.sharpness = sharpness
        self._right_osc.sharpness = sharpness

        left_gate = self._left_osc.update(dt, rate)
        right_gate = self._right_osc.update(dt, rate * 0.96)

        # Harder braking pushes the pulse harder.
        brake_weight = clamp(settings.get("brake_weight", 0.30), 0.0, 1.0)
        strength = (1.0 - brake_weight) + brake_weight * clamp(telemetry.brake)
        floor = lerp(0.25, 0.0, sharpness)  # softer setting keeps a little bed

        return EffectOutput(
            left=clamp(self.shape(left_lock, settings) * strength * (floor + (1.0 - floor) * left_gate)),
            right=clamp(self.shape(right_lock, settings) * strength * (floor + (1.0 - floor) * right_gate)),
        )

    def _lock_amount(self, telemetry: TelemetryFrame, settings: EffectSettings) -> Wheels:
        """Per-wheel lock severity, 0..1."""
        full = max(0.02, settings.get("full_lock", self.FULL_LOCK))
        raw = telemetry.wheel_slip_ratio

        def lock(value: float) -> float:
            return clamp(-value / full) if value < 0.0 else 0.0

        wheels = Wheels(lock(raw.fl), lock(raw.fr), lock(raw.rl), lock(raw.rr))

        # An explicit ABS flag guarantees a floor even if slip looks mild -
        # the pump is genuinely running, so it should be felt.
        if telemetry.abs_active and telemetry.brake > 0.2:
            floor = clamp(settings.get("abs_flag_floor", 0.45), 0.0, 1.0)
            wheels = Wheels(
                max(wheels.fl, floor),
                max(wheels.fr, floor),
                max(wheels.rl, floor),
                max(wheels.rr, floor),
            )
        return wheels
