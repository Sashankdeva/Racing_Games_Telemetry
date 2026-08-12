"""Kerbs - a sharp, speed-linked rhythm, split left/right.

A kerb is a sequence of physical ribs, so the tactile rate is set by how
fast you cross them, not by an arbitrary constant. Rate is derived from
road speed and a rib-spacing figure, which is why clipping a kerb slowly in
a chicane feels like distinct thuds while running one flat out in a fast
corner feels like a hard buzz.

This effect uses real per-wheel surface data, so left and right motors are
driven independently - a front-left kerb is felt on the left only. Nothing
is invented: if a game reports no surface information the effect stays
silent rather than guessing from steering angle.
"""

from __future__ import annotations

from app.core.models import TelemetryFrame, Wheels
from app.haptics.effects.base import Effect, EffectOutput, EffectSettings, wheels_to_lr
from app.haptics.signal import (
    MAX_USEFUL_MOD_HZ,
    Oscillator,
    Waveform,
    clamp,
    lerp,
)


class KerbEffect(Effect):
    id = "kerb"
    name = "Kerbs"
    description = "Sharp rhythmic pulses when a wheel is on a rumble strip"
    priority = 50
    dominance = 0.62
    sharpness_label = "Edge hardness"

    #: Fraction of each rib cycle spent driving. A narrow duty gives big
    #: peaks but little felt energy - an ERM integrates, so a kerb with a
    #: low mean sits *underneath* the engine bed no matter how tall its
    #: spikes are. Wide enough here to carry real weight, still gapped
    #: enough to read as discrete strikes rather than a buzz.
    DUTY = 0.62

    #: Rib crossing rate at walking pace and at full speed.
    MIN_HZ = 6.0
    MAX_HZ = 30.0
    #: Speed (kph) at which the rate reaches MAX_HZ.
    FULL_RATE_SPEED = 220.0

    def __init__(self, settings: EffectSettings | None = None) -> None:
        super().__init__(settings)
        self._left_osc = Oscillator(Waveform.PULSE, self.MIN_HZ, sharpness=0.85, duty=self.DUTY)
        self._right_osc = Oscillator(Waveform.PULSE, self.MIN_HZ, sharpness=0.85, duty=self.DUTY)

    def reset(self) -> None:
        super().reset()
        self._left_osc.reset()
        # Offset the right side so both kerbs at once do not read as one
        # single fat pulse across the whole controller.
        self._right_osc.reset(0.5)

    def generate(
        self, dt: float, telemetry: TelemetryFrame, settings: EffectSettings
    ) -> EffectOutput:
        contact = self._kerb_contact(telemetry)
        if contact.max <= 0.0 or not telemetry.is_moving:
            # Keep phase running so re-contact does not always start at 0.
            self._left_osc.update(dt)
            self._right_osc.update(dt)
            return EffectOutput()

        left_contact, right_contact = wheels_to_lr(contact)

        rate = self._rib_rate(telemetry, settings)
        sharpness = clamp(settings.sharpness)
        self._left_osc.sharpness = sharpness
        self._right_osc.sharpness = sharpness

        left_pulse = self._left_osc.update(dt, rate)
        right_pulse = self._right_osc.update(dt, rate)

        # Faster crossings hit harder, and hard compressions harder still.
        speed_weight = clamp(telemetry.speed_kph / self.FULL_RATE_SPEED)
        strength = lerp(0.55, 1.0, speed_weight) * self._compression_boost(telemetry, settings)
        level = self.shape(clamp(strength), settings)

        return EffectOutput(
            left=clamp(left_contact * left_pulse * level),
            right=clamp(right_contact * right_pulse * level),
        )

    def _kerb_contact(self, telemetry: TelemetryFrame) -> Wheels:
        surfaces = telemetry.surfaces
        return Wheels(
            fl=1.0 if surfaces.fl.is_kerb else 0.0,
            fr=1.0 if surfaces.fr.is_kerb else 0.0,
            rl=1.0 if surfaces.rl.is_kerb else 0.0,
            rr=1.0 if surfaces.rr.is_kerb else 0.0,
        )

    def _rib_rate(self, telemetry: TelemetryFrame, settings: EffectSettings) -> float:
        min_hz = settings.get("min_hz", self.MIN_HZ)
        max_hz = settings.get("max_hz", self.MAX_HZ)
        full_speed = max(20.0, settings.get("full_rate_speed", self.FULL_RATE_SPEED))
        weight = clamp(telemetry.speed_kph / full_speed)
        return clamp(lerp(min_hz, max_hz, weight), 1.0, MAX_USEFUL_MOD_HZ)

    def _compression_boost(
        self, telemetry: TelemetryFrame, settings: EffectSettings
    ) -> float:
        """Use suspension travel to tell a light brush from a real hit.

        Only applies where the game supplies suspension data; otherwise the
        effect runs on surface contact alone.
        """
        weight = clamp(settings.get("suspension_weight", 0.35), 0.0, 1.0)
        if weight <= 0.0:
            return 1.0
        scale = max(1.0, settings.get("suspension_scale", 12.0))
        travel = clamp(telemetry.suspension_velocity.max / scale)
        if travel <= 0.0:
            return 1.0
        return (1.0 - weight) + weight * (1.0 + travel)
