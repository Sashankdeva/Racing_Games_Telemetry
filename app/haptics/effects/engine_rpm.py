"""Engine / RPM - the base layer the rest of the experience sits on.

This effect is deliberately NOT allowed to dominate. It is the bed: always
present, always informative, but it must leave room for the events that
actually carry information (shifts, kerbs, locking, contact). Three design
rules follow from that, and all three were learned from driving it:

1. Rate and intensity are separate curves.
   Tying them together means the top of the rev range arrives as one
   undifferentiated wall. They are computed independently here: intensity
   uses a slow-rising exponent so the sensation does NOT max out early,
   while rate rises closer to linearly so the rhythm tracks the engine.

2. The modulation range is SLOW - 3 Hz at idle to 13 Hz at the redline.
   This is the single biggest correction from real driving. An ERM rotor
   integrates amplitude modulation as it gets faster, so a high rate stops
   reading as "engine" and starts reading as a continuous electric buzz.
   Earlier versions ran to 32 Hz and then 21 Hz; both felt like a tone.
   Intensity at the top of the range comes from AMPLITUDE, not from
   spinning the rate up, which is what lets the redline feel intense while
   still being recognisably an engine turning over.

3. Modulation depth stays high all the way up.
   Narrowing the depth at high revs turns the signal into near-DC. Depth
   is held at ~0.5 even at the redline so the engine remains recognisably
   an engine, and the peak level is capped well below full scale so events
   have somewhere to go.

The ceiling matters most. At redline this effect asks for ~0.55, not 1.0.
That single change is what stops it masking everything else.

Independence: this effect reads RPM only. Speed is never consulted, and
throttle only colours it slightly - a stationary car with the engine idling
and no throttle must still pulse.
"""

from __future__ import annotations

from app.core.models import TelemetryFrame
from app.haptics.effects.base import Effect, EffectOutput, EffectSettings
from app.haptics.signal import (
    MAX_USEFUL_MOD_HZ,
    OnePole,
    Oscillator,
    Waveform,
    clamp,
    lerp,
)


class EngineRpmEffect(Effect):
    id = "engine_rpm"
    name = "Engine / RPM"
    description = "Continuous engine bed - rises in rate and strength, but stays under events"
    # Above road texture, below every event effect. It is a layer, not a star.
    priority = 20
    dominance = 0.15  # ducks almost nothing; everything punches through it
    sharpness_label = "Pulse hardness"

    # --- rate: what the rhythm does -------------------------------------
    #: Idle pulse rate. Slow enough to count individual thuds - this is the
    #: difference between feeling an engine turning over and hearing a tone.
    MIN_HZ = 3.0
    #: Redline rate. An ERM rotor can still articulate individual pulses at
    #: this rate; past roughly 15-18 Hz it starts integrating them into a
    #: continuous electric buzz, which is what "irritating" meant. Intensity
    #: at the top comes from AMPLITUDE, not from spinning the rate up.
    MAX_HZ = 13.0
    #: Linear: the rhythm should track revs honestly, with no sudden jumps.
    RATE_EXPONENT = 1.0

    # --- intensity: how hard it pushes ----------------------------------
    #: Peak level at the redline. The engine is a bed - it never asks for
    #: the whole output range, which is what leaves headroom for events.
    CEILING = 0.55
    #: >1 so the sensation builds late instead of maxing out early.
    INTENSITY_EXPONENT = 1.55
    #: Level at idle. Must be high enough that a stationary car with no
    #: throttle still has a clearly noticeable pulse - the engine is running,
    #: so it must be felt.
    FLOOR = 0.14

    # --- modulation depth -----------------------------------------------
    #: Deep at idle (distinct thuds), still substantial at the top so the
    #: redline never flattens into DC.
    DEPTH_LOW = 0.92
    DEPTH_HIGH = 0.50

    #: Above this fraction of redline we treat the engine as on the limiter.
    LIMITER_RATIO = 0.995
    LIMITER_HZ = 12.0

    def __init__(self, settings: EffectSettings | None = None) -> None:
        super().__init__(settings)
        self._osc = Oscillator(Waveform.SINE, self.MIN_HZ)
        self._limiter_osc = Oscillator(Waveform.SQUARE, self.LIMITER_HZ, sharpness=1.0)
        # Light de-stepping only: 60 Hz telemetry read by a 120 Hz loop.
        self._band_filter = OnePole(30.0)

    def reset(self) -> None:
        super().reset()
        self._osc.reset()
        self._limiter_osc.reset()
        self._band_filter.reset(0.0)

    def generate(
        self, dt: float, telemetry: TelemetryFrame, settings: EffectSettings
    ) -> EffectOutput:
        if telemetry.max_rpm <= 0.0 or telemetry.rpm <= 0.0:
            return EffectOutput()

        cutoff = settings.get("smoothing_hz", 30.0)
        self._band_filter.cutoff_hz = clamp(cutoff, 5.0, 60.0)
        band = self._band_filter.update(telemetry.rpm_band, dt)

        # NOTE: no early return on a zero band. At idle rpm_band is exactly
        # 0 by definition (idle is the bottom of the band), and returning
        # here made a stationary car completely silent - the engine is
        # running, so it must still be felt. The FLOOR below is what carries
        # idle, and it only applies if we get past this point.
        gated = self.shape(band, settings)

        if self._on_limiter(telemetry, settings):
            return self._limiter_signal(dt, settings)

        return self._engine_signal(dt, telemetry, settings, band, gated)

    # --- normal running ---------------------------------------------------
    def _engine_signal(
        self,
        dt: float,
        telemetry: TelemetryFrame,
        settings: EffectSettings,
        band: float,
        gated: float,
    ) -> EffectOutput:
        # --- rate: independent curve --------------------------------------
        min_hz = settings.get("min_hz", self.MIN_HZ)
        max_hz = settings.get("max_hz", self.MAX_HZ)
        rate_exp = max(0.2, settings.get("rate_exponent", self.RATE_EXPONENT))
        rate = clamp(lerp(min_hz, max_hz, band ** rate_exp), 1.0, MAX_USEFUL_MOD_HZ)

        # --- intensity: separate curve, capped well below full scale ------
        ceiling = clamp(settings.get("ceiling", self.CEILING), 0.05, 1.0)
        intensity_exp = max(0.2, settings.get("intensity_exponent", self.INTENSITY_EXPONENT))
        floor = clamp(settings.get("floor", self.FLOOR), 0.0, 0.5)
        level = lerp(floor, 1.0, gated ** intensity_exp)
        base = ceiling * level

        # Keep the pulse articulated: soften the hard edges only slightly
        # as rate climbs, never enough to flatten it.
        self._osc.sharpness = clamp(settings.sharpness * (1.0 - 0.2 * band))
        modulation = self._osc.update(dt, rate)

        depth = clamp(
            lerp(
                settings.get("depth_low", self.DEPTH_LOW),
                settings.get("depth_high", self.DEPTH_HIGH),
                band,
            )
        )
        amplitude = base * ((1.0 - depth) + depth * modulation)

        # Engine load: on-throttle pulls slightly harder. Deliberately a
        # small influence - the engine is running whether or not the driver
        # is on the throttle, so this colours the feel without ever gating
        # it. Speed is not consulted at all: a stationary car with the
        # engine running must still pulse.
        influence = clamp(settings.get("throttle_influence", 0.12), 0.0, 1.0)
        load = (1.0 - influence) + influence * clamp(telemetry.throttle)
        amplitude *= load

        return self.both(clamp(amplitude))

    # --- rev limiter ------------------------------------------------------
    def _on_limiter(self, telemetry: TelemetryFrame, settings: EffectSettings) -> bool:
        if not settings.get("limiter_enabled", 1.0):
            return False
        if telemetry.rev_limiter_active:
            return True
        ratio = clamp(settings.get("limiter_ratio", self.LIMITER_RATIO), 0.5, 1.0)
        return telemetry.rpm_ratio >= ratio and telemetry.throttle > 0.5

    def _limiter_signal(self, dt: float, settings: EffectSettings) -> EffectOutput:
        """A hard on/off stutter - categorically unlike the smooth high-rev
        pulse, and still capped so it cannot swamp the mix."""
        rate = clamp(settings.get("limiter_hz", self.LIMITER_HZ), 5.0, MAX_USEFUL_MOD_HZ)
        gate = self._limiter_osc.update(dt, rate)
        self._osc.update(dt, self.MAX_HZ)  # keep phase running
        ceiling = clamp(settings.get("ceiling", self.CEILING), 0.05, 1.0)
        # A little above the normal ceiling - being on the limiter should be
        # the strongest the engine ever feels - but still short of the event
        # effects, and gated hard rather than continuous.
        peak = min(1.0, ceiling * clamp(settings.get("limiter_boost", 1.25), 1.0, 2.0))
        return self.both(clamp(peak * (0.35 + 0.65 * gate)))
