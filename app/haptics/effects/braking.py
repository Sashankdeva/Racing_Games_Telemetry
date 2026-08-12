"""Braking - responsive load feedback under the brake pedal.

Kept deliberately restrained. ABS and wheel lock are the dramatic events
under braking and they own priority 50; if this effect were loud it would
mask exactly the information the driver needs. So it provides the sense of
load and deceleration, and steps back once the wheels start protesting.

It responds to pedal input immediately - no attack smoothing - because
brake feel that lags the pedal is worse than no brake feel at all.
"""

from __future__ import annotations

from app.core.models import TelemetryFrame
from app.haptics.effects.base import Effect, EffectOutput, EffectSettings
from app.haptics.signal import Oscillator, Waveform, clamp, lerp


class BrakingEffect(Effect):
    id = "braking"
    name = "Braking"
    description = "Load feedback under braking, scaled by pedal and deceleration"
    priority = 26
    dominance = 0.2
    sharpness_label = "Bite"
    supports_balance = True

    MAX_LEVEL = 0.42
    #: Longitudinal g treated as maximum deceleration.
    FULL_DECEL_G = 4.0

    def __init__(self, settings: EffectSettings | None = None) -> None:
        super().__init__(settings)
        self._osc = Oscillator(Waveform.SINE, 14.0, sharpness=0.3)

    def generate(
        self, dt: float, telemetry: TelemetryFrame, settings: EffectSettings
    ) -> EffectOutput:
        brake = clamp(telemetry.brake)
        if brake <= 0.0 or not telemetry.is_moving:
            self._osc.update(dt)
            return EffectOutput()

        # Blend pedal position with actual deceleration so the effect
        # reflects grip, not just how hard the trigger is pulled.
        decel_weight = clamp(settings.get("decel_weight", 0.40), 0.0, 1.0)
        full_g = max(0.5, settings.get("full_decel_g", self.FULL_DECEL_G))
        decel = clamp(max(0.0, -telemetry.g_longitudinal) / full_g)
        drive = (1.0 - decel_weight) * brake + decel_weight * decel

        level = self.shape(drive, settings)
        if level <= 0.0:
            self._osc.update(dt)
            return EffectOutput()

        rate = lerp(10.0, 22.0, clamp(settings.sharpness))
        self._osc.sharpness = clamp(settings.sharpness * 0.7)
        modulation = self._osc.update(dt, rate)

        depth = clamp(settings.get("depth", 0.30), 0.0, 1.0)
        ceiling = clamp(settings.get("max_level", self.MAX_LEVEL), 0.05, 1.0)
        amplitude = level * ceiling * ((1.0 - depth) + depth * modulation)

        return self.both(clamp(amplitude))
