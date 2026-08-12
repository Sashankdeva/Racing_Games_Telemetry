"""Wheelspin - unstable, escalating, and never quite periodic.

Slip is read as a *positive* slip ratio (wheel turning faster than the car
is travelling). Negative slip is a wheel locking, which belongs to the
ABS/lock effect instead, so the two never fight over the same event.

Two things make this feel like losing traction rather than like a buzzer:
the modulation rate climbs steeply with slip, and a noise source is folded
in so the amplitude never settles into a clean tone. Traction breaking away
should feel nervous.

Where per-wheel slip exists it drives the two motors independently, so
lighting up the inside rear on corner exit is felt on that side.
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


class WheelspinEffect(Effect):
    id = "wheelspin"
    name = "Wheelspin"
    description = "Unstable rising vibration as driven wheels break traction"
    priority = 40
    dominance = 0.5
    sharpness_label = "Instability"

    MIN_HZ = 11.0
    MAX_HZ = 30.0
    #: Slip ratio treated as fully spinning.
    FULL_SLIP = 0.45

    def __init__(self, settings: EffectSettings | None = None) -> None:
        super().__init__(settings)
        self._left_osc = Oscillator(Waveform.SINE, self.MIN_HZ, sharpness=0.4)
        self._right_osc = Oscillator(Waveform.SINE, self.MIN_HZ, sharpness=0.4)
        self._noise = NoiseSource(rate_hz=22.0, smooth=True, seed=7)

    def reset(self) -> None:
        super().reset()
        self._left_osc.reset()
        self._right_osc.reset(0.37)

    def generate(
        self, dt: float, telemetry: TelemetryFrame, settings: EffectSettings
    ) -> EffectOutput:
        slip = self._positive_slip(telemetry, settings)
        left_slip, right_slip = wheels_to_lr(slip)
        peak = max(left_slip, right_slip)

        noise = self._noise.update(dt)

        if peak <= 0.0:
            self._left_osc.update(dt)
            self._right_osc.update(dt)
            return EffectOutput()

        rate = clamp(
            lerp(
                settings.get("min_hz", self.MIN_HZ),
                settings.get("max_hz", self.MAX_HZ),
                peak,
            ),
            1.0,
            MAX_USEFUL_MOD_HZ,
        )

        sharpness = clamp(settings.sharpness)
        self._left_osc.sharpness = sharpness
        self._right_osc.sharpness = sharpness
        self._noise.set_rate(lerp(14.0, 34.0, peak))

        left_mod = self._left_osc.update(dt, rate)
        right_mod = self._right_osc.update(dt, rate * 1.07)  # slight detune

        # Noise depth grows with sharpness: the knob controls how nervous it feels.
        noise_depth = lerp(0.15, 0.55, sharpness)
        texture_l = (1.0 - noise_depth) * left_mod + noise_depth * noise
        texture_r = (1.0 - noise_depth) * right_mod + noise_depth * (1.0 - noise)

        base = lerp(0.35, 1.0, peak)

        return EffectOutput(
            left=clamp(self.shape(left_slip, settings) * texture_l * base),
            right=clamp(self.shape(right_slip, settings) * texture_r * base),
        )

    def _positive_slip(self, telemetry: TelemetryFrame, settings: EffectSettings) -> Wheels:
        full = max(0.02, settings.get("full_slip", self.FULL_SLIP))
        raw = telemetry.wheel_slip_ratio

        def spin(value: float) -> float:
            return clamp(value / full) if value > 0.0 else 0.0

        return Wheels(spin(raw.fl), spin(raw.fr), spin(raw.rl), spin(raw.rr))
