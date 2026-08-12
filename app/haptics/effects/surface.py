"""Surface feel: off-track terrain, and the road texture bed.

Two effects live here because they share the same per-wheel surface data
but serve opposite ends of the intensity range.

SurfaceEffect covers leaving the track. The character comes almost entirely
from noise rather than an oscillator - gravel is not periodic, and the
moment you drive it with a clean waveform it reads as a synthetic tone
instead of loose stones. Gravel uses sample-and-hold noise (harsh, jumpy);
grass uses interpolated noise (rough but softer), which is what makes the
two distinguishable through the grip.

RoadTextureEffect is the quiet bed underneath everything: a subtle
modulation that grows with speed, so a fast straight has presence without
anything dramatic happening. It also carries the "high speed" sensation, so
there is one road-feel control rather than two overlapping ones.
"""

from __future__ import annotations

from app.core.models import SurfaceType, TelemetryFrame, Wheels
from app.haptics.effects.base import Effect, EffectOutput, EffectSettings, wheels_to_lr
from app.haptics.signal import NoiseSource, Oscillator, Waveform, clamp, lerp

#: Per-surface (severity, noise-rate Hz, harshness) tuning.
#: harshness 1.0 = sample-and-hold (jagged), 0.0 = fully interpolated.
_SURFACE_CHARACTER: dict[SurfaceType, tuple[float, float, float]] = {
    SurfaceType.GRAVEL: (1.00, 30.0, 1.0),
    SurfaceType.ROCK: (0.95, 28.0, 1.0),
    SurfaceType.SAND: (0.70, 24.0, 0.7),
    SurfaceType.MUD: (0.55, 18.0, 0.5),
    SurfaceType.GRASS: (0.50, 20.0, 0.35),
    SurfaceType.COBBLESTONE: (0.45, 22.0, 0.8),
    SurfaceType.METAL: (0.30, 26.0, 0.9),
    SurfaceType.RIDGED: (0.40, 24.0, 0.9),
}


class SurfaceEffect(Effect):
    id = "surface"
    name = "Surface / Off-track"
    description = "Irregular noisy vibration on gravel, grass and loose surfaces"
    priority = 35
    dominance = 0.45
    sharpness_label = "Coarseness"

    #: Speed at which off-track surfaces reach full intensity.
    FULL_SPEED = 140.0

    def __init__(self, settings: EffectSettings | None = None) -> None:
        super().__init__(settings)
        self._left_noise = NoiseSource(rate_hz=26.0, smooth=True, seed=101)
        self._right_noise = NoiseSource(rate_hz=26.0, smooth=True, seed=202)
        self._left_rough = NoiseSource(rate_hz=30.0, smooth=False, seed=303)
        self._right_rough = NoiseSource(rate_hz=30.0, smooth=False, seed=404)

    def generate(
        self, dt: float, telemetry: TelemetryFrame, settings: EffectSettings
    ) -> EffectOutput:
        severity, harshness, rate = self._surface_profile(telemetry)

        # Always advance the noise so re-entry is not phase-locked.
        smooth_l = self._left_noise.update(dt)
        smooth_r = self._right_noise.update(dt)
        rough_l = self._left_rough.update(dt)
        rough_r = self._right_rough.update(dt)

        left_sev, right_sev = wheels_to_lr(severity)
        if max(left_sev, right_sev) <= 0.0 or not telemetry.is_moving:
            return EffectOutput()

        for source in (self._left_noise, self._right_noise):
            source.set_rate(rate)
        for source in (self._left_rough, self._right_rough):
            source.set_rate(rate * 1.2)

        # Sharpness biases toward the jagged sample-and-hold source.
        blend = clamp(harshness * lerp(0.5, 1.3, clamp(settings.sharpness)))
        texture_l = lerp(smooth_l, rough_l, blend)
        texture_r = lerp(smooth_r, rough_r, blend)

        full_speed = max(20.0, settings.get("full_speed", self.FULL_SPEED))
        speed_scale = lerp(0.35, 1.0, clamp(telemetry.speed_kph / full_speed))

        # Keep a floor so the surface is a continuous presence, not a flicker.
        floor = clamp(settings.get("floor", 0.35), 0.0, 0.9)

        return EffectOutput(
            left=clamp(self.shape(left_sev, settings) * speed_scale * (floor + (1.0 - floor) * texture_l)),
            right=clamp(self.shape(right_sev, settings) * speed_scale * (floor + (1.0 - floor) * texture_r)),
        )

    def _surface_profile(self, telemetry: TelemetryFrame) -> tuple[Wheels, float, float]:
        """Per-wheel severity plus the dominant surface's noise character."""
        severities = []
        worst_harshness = 0.0
        worst_rate = 24.0
        worst_severity = 0.0

        for surface in telemetry.surfaces.as_tuple():
            character = _SURFACE_CHARACTER.get(surface)
            if character is None:
                severities.append(0.0)
                continue
            severity, rate, harshness = character
            severities.append(severity)
            if severity > worst_severity:
                worst_severity = severity
                worst_harshness = harshness
                worst_rate = rate

        return Wheels(*severities), worst_harshness, worst_rate


class RoadTextureEffect(Effect):
    id = "road_texture"
    name = "Road Texture / Speed"
    description = "Subtle continuous road feel that builds with speed"
    priority = 10
    dominance = 0.1  # the quietest bed - never masks anything
    sharpness_label = "Grain"
    supports_balance = True

    #: Speed at which the texture reaches its full (still modest) level.
    FULL_SPEED = 300.0
    MAX_LEVEL = 0.30

    def __init__(self, settings: EffectSettings | None = None) -> None:
        super().__init__(settings)
        self._noise = NoiseSource(rate_hz=16.0, smooth=True, seed=55)
        self._osc = Oscillator(Waveform.SINE, 12.0, sharpness=0.2)

    def generate(
        self, dt: float, telemetry: TelemetryFrame, settings: EffectSettings
    ) -> EffectOutput:
        noise = self._noise.update(dt)

        if not telemetry.is_moving:
            self._osc.update(dt)
            return EffectOutput()

        full_speed = max(30.0, settings.get("full_speed", self.FULL_SPEED))
        speed_ratio = clamp(telemetry.speed_kph / full_speed)
        level = self.shape(speed_ratio, settings)
        if level <= 0.0:
            self._osc.update(dt)
            return EffectOutput()

        # Rate rises with speed so a straight feels like it is building.
        rate = lerp(9.0, 26.0, speed_ratio)
        self._noise.set_rate(lerp(12.0, 28.0, speed_ratio))
        self._osc.sharpness = clamp(settings.sharpness * 0.6)
        modulation = self._osc.update(dt, rate)

        grain = clamp(settings.get("grain", 0.45), 0.0, 1.0)
        texture = (1.0 - grain) * modulation + grain * noise

        ceiling = clamp(settings.get("max_level", self.MAX_LEVEL), 0.02, 1.0)
        amplitude = level * ceiling * (0.45 + 0.55 * texture)

        return self.both(clamp(amplitude))
