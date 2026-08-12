"""Collision - sudden, dominant, and short.

Highest priority in the engine with dominance 1.0, so a hard impact briefly
owns both motors: at full strength it consumes all mixer headroom and
everything else is silenced for the duration. That momentary exclusivity is
what makes a crash register as an event rather than as "the buzzing got
louder".

Attack is a single tick. Decay scales with severity - a light brush is gone
in ~120 ms, a heavy shunt rings for ~600 ms - and then it stops. A long
continuous vibration after contact feels like a bug, not like damage.

The `impact` field is supplied by the game adapter. Adapters that get no
collision events derive it from the longitudinal/lateral acceleration
spike; either way this effect just consumes a normalized 0..1 magnitude.
"""

from __future__ import annotations

from app.core.models import TelemetryFrame
from app.haptics.effects.base import Effect, EffectOutput, EffectSettings
from app.haptics.signal import Envelope, EnvelopeStage, NoiseSource, clamp, lerp


class CollisionEffect(Effect):
    id = "collision"
    name = "Collision"
    description = "Sudden dominant impact on contact, scaled by severity"
    priority = 100
    dominance = 1.0  # a full-strength hit takes over the controller
    sharpness_label = "Impact hardness"
    supports_balance = False
    _holds_state = True

    #: Below this the impact is treated as noise, not contact.
    MIN_IMPACT = 0.08
    #: Re-triggering inside this window just tops up the existing envelope.
    RETRIGGER_WINDOW = 0.05

    def __init__(self, settings: EffectSettings | None = None) -> None:
        super().__init__(settings)
        self._envelope = Envelope(attack=0.001, hold=0.02, decay=0.25)
        self._noise = NoiseSource(rate_hz=40.0, smooth=False, seed=31)
        self._since_trigger = 999.0
        self._severity = 0.0

    def reset(self) -> None:
        super().reset()
        self._envelope.reset()
        self._since_trigger = 999.0
        self._severity = 0.0

    def generate(
        self, dt: float, telemetry: TelemetryFrame, settings: EffectSettings
    ) -> EffectOutput:
        self._since_trigger += dt
        noise = self._noise.update(dt)

        impact = clamp(telemetry.impact)
        threshold = max(self.MIN_IMPACT, settings.threshold)

        if impact >= threshold and self._since_trigger >= self.RETRIGGER_WINDOW:
            self._trigger(impact, settings)

        level = self._envelope.update(dt)
        if level <= 0.0:
            return EffectOutput()

        # Grit is applied to the ringing tail only, never to the initial
        # slam. Modulating the attack would let the noise phase decide the
        # peak, so the same maximum-severity impact could land anywhere
        # between 0.70 and full - the hardest hit must always feel hardest.
        if self._envelope.stage in (EnvelopeStage.ATTACK, EnvelopeStage.HOLD):
            return self.both(clamp(level))

        grit = clamp(settings.get("grit", 0.35), 0.0, 1.0) * self._severity
        shaped = level * ((1.0 - grit) + grit * noise)

        return self.both(clamp(shaped))

    def _trigger(self, impact: float, settings: EffectSettings) -> None:
        severity = self.shape(impact, settings)
        if severity <= 0.0:
            return

        self._severity = severity
        self._since_trigger = 0.0

        snap = clamp(settings.sharpness)
        self._envelope.attack = lerp(0.006, 0.0, snap)
        self._envelope.hold = lerp(0.008, 0.030, severity)
        # Heavier impacts ring longer, but always terminate.
        self._envelope.decay = lerp(0.12, settings.get("max_decay", 0.60), severity)
        self._envelope.trigger(severity)
