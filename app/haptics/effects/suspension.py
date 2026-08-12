"""Suspension - body movement, and the one effect that genuinely wants smoothing.

Everything else in this engine avoids filtering. This is the exception, and
for a physical reason: suspension events are body-frequency motion at a few
Hz - crests, compressions, kerb strikes settling out. Passing raw
suspension acceleration through would produce a fizzing high-frequency
signal that feels like electrical noise rather than mass moving on springs.

So this effect low-passes deliberately (default 9 Hz) and modulates slowly.
It is the "weight" underneath the sharper effects, not a texture.

Driven by suspension acceleration where a game provides it, falling back to
velocity, and staying silent when neither exists.
"""

from __future__ import annotations

from app.core.models import TelemetryFrame, Wheels
from app.haptics.effects.base import Effect, EffectOutput, EffectSettings, wheels_to_lr
from app.haptics.signal import OnePole, Oscillator, Waveform, clamp, lerp


class SuspensionEffect(Effect):
    id = "suspension"
    name = "Suspension"
    description = "Low-frequency body movement over bumps, crests and compressions"
    priority = 30
    dominance = 0.35
    sharpness_label = "Body firmness"

    #: Suspension acceleration magnitude treated as a full-scale event.
    FULL_ACCEL = 45.0
    FULL_VELOCITY = 18.0
    #: Body motion lives at a few Hz - filtering here is physically correct.
    CUTOFF_HZ = 9.0

    def __init__(self, settings: EffectSettings | None = None) -> None:
        super().__init__(settings)
        self._left_filter = OnePole(self.CUTOFF_HZ)
        self._right_filter = OnePole(self.CUTOFF_HZ)
        self._osc = Oscillator(Waveform.SINE, 7.0, sharpness=0.15)

    def reset(self) -> None:
        super().reset()
        self._left_filter.reset(0.0)
        self._right_filter.reset(0.0)
        self._osc.reset()

    def generate(
        self, dt: float, telemetry: TelemetryFrame, settings: EffectSettings
    ) -> EffectOutput:
        magnitude = self._magnitude(telemetry, settings)
        left_raw, right_raw = wheels_to_lr(magnitude)

        cutoff = clamp(settings.get("cutoff_hz", self.CUTOFF_HZ), 2.0, 30.0)
        self._left_filter.cutoff_hz = cutoff
        self._right_filter.cutoff_hz = cutoff

        left = self._left_filter.update(left_raw, dt)
        right = self._right_filter.update(right_raw, dt)

        # Firmer setting = faster body modulation and a tighter feel.
        rate = lerp(5.0, 12.0, clamp(settings.sharpness))
        modulation = self._osc.update(dt, rate)
        depth = clamp(settings.get("depth", 0.35), 0.0, 1.0)
        shaped = (1.0 - depth) + depth * modulation

        left_out = self.shape(clamp(left), settings) * shaped
        right_out = self.shape(clamp(right), settings) * shaped

        if max(left_out, right_out) <= 0.0:
            return EffectOutput()
        return EffectOutput(left=clamp(left_out), right=clamp(right_out))

    def _magnitude(self, telemetry: TelemetryFrame, settings: EffectSettings) -> Wheels:
        accel = telemetry.suspension_acceleration
        if accel.max > 0.0 or min(accel.as_tuple()) < 0.0:
            scale = max(1.0, settings.get("full_accel", self.FULL_ACCEL))
            return Wheels(*(clamp(abs(v) / scale) for v in accel.as_tuple()))

        velocity = telemetry.suspension_velocity
        scale = max(1.0, settings.get("full_velocity", self.FULL_VELOCITY))
        return Wheels(*(clamp(abs(v) / scale) for v in velocity.as_tuple()))
