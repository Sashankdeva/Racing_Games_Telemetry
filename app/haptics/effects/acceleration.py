"""Acceleration - body load under power, and lateral load in corners.

Reads measured g-force rather than throttle position, so it reflects what
the car is actually doing: full throttle in sixth produces far less shove
than full throttle in second, and this effect tells them apart.

Lateral g is routed to the motor on the *outside* of the corner, which is
where the load actually goes. That is derived from the sign of measured
lateral acceleration - not from steering input - so it stays correct
through oversteer and mid-corner corrections.
"""

from __future__ import annotations

from app.core.models import TelemetryFrame
from app.haptics.effects.base import Effect, EffectOutput, EffectSettings
from app.haptics.signal import Oscillator, Waveform, clamp, lerp


class AccelerationEffect(Effect):
    id = "acceleration"
    name = "Acceleration / G-Force"
    description = "Body load under power and cornering, from measured g-force"
    priority = 24
    dominance = 0.2
    sharpness_label = "Firmness"

    MAX_LEVEL = 0.40
    FULL_ACCEL_G = 2.0
    FULL_LATERAL_G = 4.0

    def __init__(self, settings: EffectSettings | None = None) -> None:
        super().__init__(settings)
        self._osc = Oscillator(Waveform.SINE, 11.0, sharpness=0.2)

    def generate(
        self, dt: float, telemetry: TelemetryFrame, settings: EffectSettings
    ) -> EffectOutput:
        if not telemetry.is_moving:
            self._osc.update(dt)
            return EffectOutput()

        full_long = max(0.2, settings.get("full_accel_g", self.FULL_ACCEL_G))
        longitudinal = clamp(max(0.0, telemetry.g_longitudinal) / full_long)

        lateral_weight = clamp(settings.get("lateral_weight", 0.45), 0.0, 1.0)
        full_lat = max(0.2, settings.get("full_lateral_g", self.FULL_LATERAL_G))
        lateral = clamp(abs(telemetry.g_lateral) / full_lat)

        combined = clamp(longitudinal + lateral * lateral_weight)
        level = self.shape(combined, settings)
        if level <= 0.0:
            self._osc.update(dt)
            return EffectOutput()

        rate = lerp(8.0, 18.0, clamp(settings.sharpness))
        modulation = self._osc.update(dt, rate)
        depth = clamp(settings.get("depth", 0.25), 0.0, 1.0)
        ceiling = clamp(settings.get("max_level", self.MAX_LEVEL), 0.05, 1.0)
        amplitude = level * ceiling * ((1.0 - depth) + depth * modulation)

        # Route cornering load to the outside of the turn. Positive lateral
        # g means the car is being pushed right, i.e. a left-hand corner.
        bias = clamp(settings.get("lateral_bias", 0.5), 0.0, 1.0)
        lateral_share = lateral * lateral_weight * bias
        if telemetry.g_lateral > 0.0:
            left_gain, right_gain = 1.0 - lateral_share, 1.0
        else:
            left_gain, right_gain = 1.0, 1.0 - lateral_share

        return EffectOutput(
            left=clamp(amplitude * left_gain),
            right=clamp(amplitude * right_gain),
        )
