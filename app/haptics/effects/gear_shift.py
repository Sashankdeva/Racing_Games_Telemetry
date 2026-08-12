"""Gear shift - a mechanical event with a shape, not a single buzz.

An upshift and a downshift are physically different things and must feel
different. Both start with the same sharp transient, but what follows is
what distinguishes them, and the effect models that sequence explicitly:

  UPSHIFT     hit -> revs FALL -> engine settles lower
              A clean single strike. The engine bed drops on its own
              because the revs genuinely drop, so the shift is followed by
              a noticeable easing-off. Short duck, no second event.

  DOWNSHIFT   hit -> revs RISE -> brief engine surge -> settles higher
              The strike is followed by a *blip*: a second, softer swell
              that tracks the rev rise as the engine catches up. Its
              amplitude comes from the real measured RPM delta, so a big
              multi-gear downshift under braking surges harder than a
              gentle one.

Two hardware realities shape the timings:

  * An ERM needs roughly 50 ms at full drive to reach speed. A transient
    that holds peak for only ~15 ms leaves the rotor still accelerating
    when the command is already decaying - the hand feels a vague bump
    rather than a hit. The peak is held long enough to physically arrive.
  * Amplitude alone cannot punch through a loud bed. The sidechain duck
    outlasts the hit, dropping the engine away underneath it, and that
    momentary hole is what makes the shift register as an event.
"""

from __future__ import annotations

from app.core.models import TelemetryFrame
from app.haptics.effects.base import Effect, EffectOutput, EffectSettings
from app.haptics.signal import Envelope, clamp, lerp


class GearShiftEffect(Effect):
    id = "gear_shift"
    name = "Gear Shift"
    description = "Sharp impact on every shift, with a rev-matched surge on downshifts"
    priority = 70  # second only to collision
    dominance = 0.70
    sharpness_label = "Impact snap"
    _holds_state = True

    #: Time the rotor needs at full drive before it is actually up to speed.
    ROTOR_SPINUP = 0.05
    #: How long after a downshift we watch for the rev rise before firing
    #: the surge. Long enough for the revs to actually climb, short enough
    #: that the surge still reads as part of the same shift event.
    BLIP_WINDOW = 0.22
    #: RPM rise (as a fraction of redline) treated as a full-strength surge.
    FULL_BLIP_RISE = 0.22

    def __init__(self, settings: EffectSettings | None = None) -> None:
        super().__init__(settings)
        self._envelope = Envelope(attack=0.003, hold=0.055, decay=0.11)
        # Longer than the output envelope: the tail is what creates the
        # hole in the engine bed after the hit.
        self._duck_envelope = Envelope(attack=0.002, hold=0.07, decay=0.16)
        # The downshift surge - a swell, not a strike, so it has a slow
        # attack and no hold.
        self._blip = Envelope(attack=0.06, hold=0.04, decay=0.22)

        self._last_gear: int | None = None
        self._watching_blip = False
        self._blip_elapsed = 0.0
        self._rpm_at_shift = 0.0
        self._peak_rise = 0.0

    def reset(self) -> None:
        super().reset()
        self._envelope.reset()
        self._duck_envelope.reset()
        self._blip.reset()
        self._last_gear = None
        self._watching_blip = False
        self._blip_elapsed = 0.0
        self._peak_rise = 0.0

    def duck(self) -> float:
        if not self.settings.enabled:
            return 0.0
        depth = clamp(self.settings.get("duck", 0.80), 0.0, 1.0)
        return clamp(self._duck_envelope.level * depth)

    def generate(
        self, dt: float, telemetry: TelemetryFrame, settings: EffectSettings
    ) -> EffectOutput:
        self._detect_shift(telemetry, settings)
        self._track_downshift_rise(dt, telemetry, settings)

        strike = self._envelope.update(dt)
        surge = self._blip.update(dt)
        self._duck_envelope.update(dt)

        # The strike and the surge never overlap meaningfully (the surge
        # starts as the strike decays), so taking the louder of the two
        # keeps the sequence readable rather than muddying it.
        level = max(strike, surge)
        if level <= 0.0:
            return EffectOutput()
        return self.both(clamp(level))

    # ------------------------------------------------------------------
    def _detect_shift(self, telemetry: TelemetryFrame, settings: EffectSettings) -> None:
        gear = telemetry.gear
        previous = self._last_gear

        if previous is None:
            self._last_gear = gear
            return
        if gear == previous:
            return

        self._last_gear = gear

        # Ignore shuffling through neutral/reverse in the pits or on the grid.
        if gear <= 0 or previous <= 0:
            return

        upshift = gear > previous
        if not upshift and not settings.get("downshift_enabled", 1.0):
            return

        snap = clamp(settings.sharpness)
        self._envelope.attack = lerp(0.010, 0.001, snap)
        # Never shorter than the rotor can respond to.
        self._envelope.hold = max(self.ROTOR_SPINUP, lerp(0.075, 0.050, snap))
        base_decay = lerp(0.16, 0.09, snap)
        # A downshift strike is a touch longer and softer than an upshift's
        # clean snap - part of what makes the two distinguishable.
        self._envelope.decay = base_decay if upshift else base_decay * 1.35

        rev_weight = clamp(settings.get("rev_weight", 0.30), 0.0, 1.0)
        amplitude = (1.0 - rev_weight) + rev_weight * telemetry.rpm_ratio
        if not upshift:
            amplitude *= clamp(settings.get("downshift_scale", 0.88), 0.1, 1.5)

        shaped = self.shape(clamp(amplitude), settings)
        self._envelope.trigger(shaped)

        # Upshifts duck briefly; downshifts hold the bed down a little
        # longer so the surge that follows has room to be felt.
        self._duck_envelope.hold = self._envelope.hold + (0.02 if upshift else 0.05)
        self._duck_envelope.decay = self._envelope.decay * (1.5 if upshift else 2.0)
        self._duck_envelope.trigger(shaped)

        if upshift:
            self._watching_blip = False
        else:
            # Start watching for the rev rise that follows a downshift.
            self._watching_blip = True
            self._blip_elapsed = 0.0
            self._rpm_at_shift = telemetry.rpm
            self._peak_rise = 0.0

    def _track_downshift_rise(
        self, dt: float, telemetry: TelemetryFrame, settings: EffectSettings
    ) -> None:
        """Fire the surge from the real RPM change after a downshift."""
        if not self._watching_blip:
            return

        self._blip_elapsed += dt
        if telemetry.max_rpm > 0.0:
            rise = (telemetry.rpm - self._rpm_at_shift) / telemetry.max_rpm
            self._peak_rise = max(self._peak_rise, rise)

        window = settings.get("blip_window", self.BLIP_WINDOW)
        if self._blip_elapsed < window:
            return

        self._watching_blip = False

        full_rise = max(0.02, settings.get("full_blip_rise", self.FULL_BLIP_RISE))
        strength = clamp(self._peak_rise / full_rise)
        if strength <= 0.05:
            return  # revs did not actually rise; nothing to represent

        scale = clamp(settings.get("blip_scale", 0.55), 0.0, 1.0)
        self._blip.trigger(clamp(strength * scale))
