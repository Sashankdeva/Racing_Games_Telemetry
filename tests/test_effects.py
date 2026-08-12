"""Effect behaviour.

These tests assert the *character* each effect is supposed to have, not
just that it produces a number: that the engine gets faster and stronger
with revs, that a gear shift is short and sharp, that gravel is irregular
rather than periodic, and that per-wheel events reach the correct motor.
"""

from __future__ import annotations

import pytest

from app.core.models import Surfaces, SurfaceType, TelemetryFrame, Wheels
from app.haptics.effects import EFFECT_CLASSES, create_all
from app.haptics.effects.abs_lock import AbsLockEffect
from app.haptics.effects.base import EffectSettings
from app.haptics.effects.collision import CollisionEffect
from app.haptics.effects.engine_rpm import EngineRpmEffect
from app.haptics.effects.gear_shift import GearShiftEffect
from app.haptics.effects.kerb import KerbEffect
from app.haptics.effects.surface import SurfaceEffect
from app.haptics.effects.wheelspin import WheelspinEffect

DT = 1 / 120


def run(effect, frame, ticks: int = 240, skip: int = 60):
    """Run an effect and return the settled (left, right) sample lists."""
    left, right = [], []
    for index in range(ticks):
        output = effect.update(DT, frame)
        if index >= skip:
            left.append(output.left)
            right.append(output.right)
    return left, right


def rpm_frame(rpm: float, **overrides) -> TelemetryFrame:
    defaults = dict(
        valid=True, rpm=rpm, max_rpm=12000.0, idle_rpm=4000.0,
        speed_kph=200.0, gear=5, throttle=1.0,
    )
    defaults.update(overrides)
    return TelemetryFrame(**defaults)


# --------------------------------------------------------------------------
class TestContract:
    @pytest.mark.parametrize("effect_class", EFFECT_CLASSES, ids=lambda c: c.id)
    def test_output_always_in_range(self, effect_class):
        effect = effect_class()
        frame = rpm_frame(9000, brake=0.5, g_lateral=3.0, g_longitudinal=-2.0,
                          wheel_slip_ratio=Wheels(0.3, -0.3, 0.2, -0.2),
                          suspension_acceleration=Wheels(40, 40, 40, 40),
                          impact=0.9)
        for _ in range(300):
            output = effect.update(DT, frame)
            assert 0.0 <= output.left <= 1.0
            assert 0.0 <= output.right <= 1.0

    @pytest.mark.parametrize("effect_class", EFFECT_CLASSES, ids=lambda c: c.id)
    def test_invalid_telemetry_produces_silence(self, effect_class):
        effect = effect_class()
        for _ in range(20):
            output = effect.update(DT, TelemetryFrame(valid=False))
            assert output.left == 0.0 and output.right == 0.0

    @pytest.mark.parametrize("effect_class", EFFECT_CLASSES, ids=lambda c: c.id)
    def test_paused_produces_silence(self, effect_class):
        effect = effect_class()
        frame = rpm_frame(9000)
        frame.paused = True
        for _ in range(20):
            output = effect.update(DT, frame)
            assert output.left == 0.0 and output.right == 0.0

    @pytest.mark.parametrize("effect_class", EFFECT_CLASSES, ids=lambda c: c.id)
    def test_disabled_produces_silence(self, effect_class):
        effect = effect_class(EffectSettings(enabled=False))
        for _ in range(20):
            output = effect.update(DT, rpm_frame(9000))
            assert output.left == 0.0 and output.right == 0.0

    def test_registry_ids_are_unique(self):
        ids = [cls.id for cls in EFFECT_CLASSES]
        assert len(ids) == len(set(ids))

    def test_create_all_builds_every_effect(self):
        assert len(create_all()) == len(EFFECT_CLASSES)


# --------------------------------------------------------------------------
class TestEngineRpm:
    def test_modulation_rate_rises_with_revs(self):
        """Rate must climb across the band - this is what makes it feel
        like an engine rather than a level control."""
        rates = []
        for rpm in (4500, 6000, 8000, 10000, 11500):
            effect = EngineRpmEffect()
            run(effect, rpm_frame(rpm), ticks=300, skip=299)
            rates.append(effect._osc.frequency)

        assert rates == sorted(rates)
        assert rates[0] < 5.0    # idle: slow enough to count individual thuds
        assert rates[-1] > 8.0   # redline: clearly faster
        assert rates[-1] <= 15.0  # ...but never fast enough to become a buzz

    def test_idle_is_present_when_stationary_with_no_throttle(self):
        """A running engine must be felt even parked with the throttle shut.

        rpm_band is exactly 0 at idle by definition, so an early return on a
        zero band made a stationary car completely silent. The floor is what
        carries idle.
        """
        frame = rpm_frame(4000, throttle=0.0, speed_kph=0.0)
        left, _ = run(EngineRpmEffect(), frame, ticks=600, skip=300)
        assert max(left) > 0.02, "engine silent at idle"
        assert max(left) - min(left) > 0.01, "idle must pulse, not sit flat"

    def test_speed_does_not_affect_the_engine(self):
        """Engine character comes from revs alone."""
        slow, _ = run(EngineRpmEffect(), rpm_frame(9000, speed_kph=5.0))
        fast, _ = run(EngineRpmEffect(), rpm_frame(9000, speed_kph=300.0))
        assert max(slow) == pytest.approx(max(fast), abs=1e-6)

    def test_throttle_is_not_required(self):
        """Off-throttle must still produce most of the engine feel."""
        on, _ = run(EngineRpmEffect(), rpm_frame(9000, throttle=1.0))
        off, _ = run(EngineRpmEffect(), rpm_frame(9000, throttle=0.0))
        assert max(off) > max(on) * 0.8

    def test_rate_stays_inside_the_articulable_band(self):
        """The redline must NOT run so fast that the rotor smears it.

        An ERM cannot articulate amplitude modulation much above ~25 Hz; it
        integrates it into a flat buzz. Driving to 32 Hz produced a bigger
        number and a worse feel - the "irritating continuous vibration at
        10-12k rpm" was exactly this. The ceiling is a feel requirement,
        not an arbitrary limit.
        """
        effect = EngineRpmEffect()
        run(effect, rpm_frame(11950), ticks=300, skip=299)
        assert effect._osc.frequency <= 25.0

    def test_engine_stays_a_bed_and_leaves_headroom(self):
        """The engine must never ask for the whole output range, or events
        have nowhere to go and it masks everything.

        Uses 11500 rather than the very top of the range: above ~98.5% of
        redline the effect switches to the limiter stutter, which is a
        different signal with its own (still capped) ceiling.
        """
        left, _ = run(EngineRpmEffect(), rpm_frame(11500))
        assert max(left) <= 0.65

    def test_intensity_builds_late_rather_than_maxing_out_early(self):
        """Complaint: 'reaches maximum sensation far too quickly'. Half
        revs must be well under half the peak level."""
        mid, _ = run(EngineRpmEffect(), rpm_frame(8000))    # band ~0.5
        top, _ = run(EngineRpmEffect(), rpm_frame(11500))   # below the limiter
        assert max(mid) < max(top) * 0.6

    def test_rate_progresses_evenly_with_no_sudden_jumps(self):
        """Gradual frequency progression across the whole range.

        Deliberately linear now. An accelerating rate curve made the top of
        the range rush away and read as a buzz; an even progression tracks
        the engine honestly and has no step changes anywhere.
        """
        def rate_at(rpm):
            effect = EngineRpmEffect()
            run(effect, rpm_frame(rpm), ticks=300, skip=299)
            return effect._osc.frequency

        rates = [rate_at(r) for r in range(4000, 12001, 1000)]
        deltas = [b - a for a, b in zip(rates, rates[1:])]
        assert all(d > 0 for d in deltas)          # always rising
        assert max(deltas) - min(deltas) < 0.5     # evenly, no jumps

    def test_amplitude_rises_with_revs(self):
        peaks = []
        for rpm in (5000, 8000, 11000):
            left, _ = run(EngineRpmEffect(), rpm_frame(rpm))
            peaks.append(max(left))
        assert peaks == sorted(peaks)

    def test_signal_actually_modulates_rather_than_sitting_at_dc(self):
        left, _ = run(EngineRpmEffect(), rpm_frame(8000))
        assert max(left) - min(left) > 0.1

    def test_low_revs_modulate_more_deeply_than_high_revs(self):
        """Distinct pulses low down; a tighter, denser buzz up top."""
        low, _ = run(EngineRpmEffect(), rpm_frame(5000))
        high, _ = run(EngineRpmEffect(), rpm_frame(11000))
        low_depth = (max(low) - min(low)) / max(low)
        high_depth = (max(high) - min(high)) / max(high)
        assert low_depth > high_depth

    def test_rev_limiter_is_categorically_different(self):
        """A hard stutter, not just 'more of the same' high-rev buzz."""
        normal, _ = run(EngineRpmEffect(), rpm_frame(11000))
        limiter, _ = run(
            EngineRpmEffect(),
            rpm_frame(12000, rev_limiter_active=True),
        )
        # Categorically different = deeper modulation (a stutter), not
        # merely louder. It is still capped below full scale so even the
        # limiter cannot swamp the events layered above it.
        assert (max(limiter) - min(limiter)) > (max(normal) - min(normal))
        assert max(limiter) > max(normal)
        assert max(limiter) <= 0.85

    def test_silent_without_rpm_data(self):
        left, right = run(EngineRpmEffect(), TelemetryFrame(valid=True, rpm=0, max_rpm=0))
        assert max(left) == 0.0 and max(right) == 0.0

    def test_throttle_influences_load(self):
        on, _ = run(EngineRpmEffect(), rpm_frame(9000, throttle=1.0))
        off, _ = run(EngineRpmEffect(), rpm_frame(9000, throttle=0.0))
        assert max(on) > max(off)


# --------------------------------------------------------------------------
class TestGearShift:
    def test_fires_on_gear_change(self):
        effect = GearShiftEffect()
        effect.update(DT, rpm_frame(9000, gear=4))
        peak = max(effect.update(DT, rpm_frame(9000, gear=5)).left for _ in range(1))
        # Attack is a single tick, so the very next sample is already loud.
        assert peak > 0.5

    def test_is_short_lived(self):
        """A shift is an impact. If it rings for half a second it reads as
        a rumble instead."""
        effect = GearShiftEffect()
        effect.update(DT, rpm_frame(9000, gear=4))
        frame = rpm_frame(9000, gear=5)

        duration = 0.0
        for _ in range(240):
            if effect.update(DT, frame).left <= 0.001:
                break
            duration += DT
        assert duration < 0.35

    def test_no_output_without_a_shift(self):
        effect = GearShiftEffect()
        left, _ = run(effect, rpm_frame(9000, gear=5))
        assert max(left) == 0.0

    def test_neutral_and_reverse_transitions_are_ignored(self):
        effect = GearShiftEffect()
        effect.update(DT, rpm_frame(1000, gear=0))
        assert effect.update(DT, rpm_frame(1000, gear=1)).left == 0.0

    def _shift_trace(self, from_gear, to_gear, rpm_before, rpm_after, ticks=90):
        """Run a shift with the revs actually moving, as they do in game."""
        effect = GearShiftEffect()
        for _ in range(30):
            effect.update(DT, rpm_frame(rpm_before, gear=from_gear))
        levels = []
        for i in range(ticks):
            progress = min(1.0, i / 18)
            rpm = rpm_before + (rpm_after - rpm_before) * progress
            levels.append(effect.update(DT, rpm_frame(rpm, gear=to_gear)).left)
        return levels

    def test_downshift_produces_a_second_surge_after_the_strike(self):
        """The requested sequence: impact -> revs rise -> engine surge.

        The surge is driven by the real measured RPM delta, so it only
        appears when the revs actually climb.
        """
        levels = self._shift_trace(6, 5, 8000, 11200)
        strike = max(levels[:20])
        surge = max(levels[35:])
        assert strike > 0.5
        assert surge > 0.15, "no rev-matched surge after the downshift"

    def test_upshift_has_no_second_surge(self):
        """Revs fall on an upshift, so there is nothing to surge about -
        this is what makes the two shifts feel different."""
        levels = self._shift_trace(5, 6, 11500, 9200)
        assert max(levels[:20]) > 0.5   # the strike
        assert max(levels[35:]) < 0.05  # then nothing

    def test_downshift_surge_scales_with_the_rev_rise(self):
        small = max(self._shift_trace(6, 5, 10000, 10600)[35:])
        large = max(self._shift_trace(6, 4, 8000, 11800)[35:])
        assert large > small

    def test_downshift_without_a_rev_rise_produces_no_surge(self):
        """Guards against firing the surge on gear number alone."""
        levels = self._shift_trace(6, 5, 9000, 9000)
        assert max(levels[35:]) < 0.05

    def test_shift_ducks_the_engine_bed(self):
        effect = GearShiftEffect()
        effect.update(DT, rpm_frame(11000, gear=4))
        effect.update(DT, rpm_frame(11000, gear=5))
        assert effect.duck() > 0.3

    def test_higher_revs_hit_harder(self):
        def shift_peak(rpm):
            effect = GearShiftEffect()
            effect.update(DT, rpm_frame(rpm, gear=4))
            return max(effect.update(DT, rpm_frame(rpm, gear=5)).left for _ in range(1))

        assert shift_peak(11500) > shift_peak(5000)


# --------------------------------------------------------------------------
class TestKerb:
    def _kerb_frame(self, left_on=True, right_on=False, speed=150.0):
        return TelemetryFrame(
            valid=True, speed_kph=speed, rpm=9000, max_rpm=12000,
            surfaces=Surfaces(
                fl=SurfaceType.RUMBLE_STRIP if left_on else SurfaceType.TARMAC,
                fr=SurfaceType.RUMBLE_STRIP if right_on else SurfaceType.TARMAC,
                rl=SurfaceType.RUMBLE_STRIP if left_on else SurfaceType.TARMAC,
                rr=SurfaceType.RUMBLE_STRIP if right_on else SurfaceType.TARMAC,
            ),
        )

    def test_left_kerb_drives_only_the_left_motor(self):
        left, right = run(KerbEffect(), self._kerb_frame(left_on=True, right_on=False))
        assert max(left) > 0.1
        assert max(right) == 0.0

    def test_right_kerb_drives_only_the_right_motor(self):
        left, right = run(KerbEffect(), self._kerb_frame(left_on=False, right_on=True))
        assert max(right) > 0.1
        assert max(left) == 0.0

    def test_silent_on_tarmac(self):
        left, right = run(KerbEffect(), self._kerb_frame(False, False))
        assert max(left) == 0.0 and max(right) == 0.0

    def test_rhythm_is_faster_at_higher_speed(self):
        slow = KerbEffect()
        run(slow, self._kerb_frame(speed=60.0), ticks=200, skip=199)
        fast = KerbEffect()
        run(fast, self._kerb_frame(speed=250.0), ticks=200, skip=199)
        assert fast._left_osc.frequency > slow._left_osc.frequency

    def test_pulses_return_to_silence_between_ribs(self):
        """Kerbs must feel like discrete strikes, not a constant buzz."""
        left, _ = run(KerbEffect(), self._kerb_frame(speed=80.0), ticks=400, skip=100)
        assert min(left) < 0.05
        assert max(left) > 0.3

    def test_stationary_car_produces_nothing(self):
        left, _ = run(KerbEffect(), self._kerb_frame(speed=0.0))
        assert max(left) == 0.0


# --------------------------------------------------------------------------
class TestAbsLock:
    def _lock_frame(self, slip=-0.3, brake=0.9, abs_flag=None):
        return TelemetryFrame(
            valid=True, speed_kph=180, rpm=8000, max_rpm=12000, brake=brake,
            wheel_slip_ratio=Wheels(slip, slip, slip, slip),
            abs_active=abs_flag,
        )

    def test_locking_under_braking_produces_output(self):
        left, _ = run(AbsLockEffect(), self._lock_frame())
        assert max(left) > 0.2

    def test_silent_without_brake_input(self):
        left, right = run(AbsLockEffect(), self._lock_frame(brake=0.0))
        assert max(left) == 0.0 and max(right) == 0.0

    def test_positive_slip_is_ignored(self):
        """Wheelspin belongs to the wheelspin effect, not to ABS."""
        left, _ = run(AbsLockEffect(), self._lock_frame(slip=0.4))
        assert max(left) == 0.0

    def test_gate_is_mechanical_not_a_constant_buzz(self):
        settings = EffectSettings(sharpness=1.0)
        left, _ = run(AbsLockEffect(settings), self._lock_frame(), ticks=400, skip=100)
        assert min(left) < 0.05   # fully off between pump strokes
        assert max(left) > 0.3

    def test_abs_flag_guarantees_a_floor(self):
        without = run(AbsLockEffect(), self._lock_frame(slip=-0.02, abs_flag=False))[0]
        with_flag = run(AbsLockEffect(), self._lock_frame(slip=-0.02, abs_flag=True))[0]
        assert max(with_flag) > max(without)


# --------------------------------------------------------------------------
class TestWheelspin:
    def _spin_frame(self, rl=0.0, rr=0.0):
        return TelemetryFrame(
            valid=True, speed_kph=120, rpm=10000, max_rpm=12000, throttle=1.0,
            wheel_slip_ratio=Wheels(fl=0.0, fr=0.0, rl=rl, rr=rr),
        )

    def test_rear_left_spin_biases_the_left_motor(self):
        left, right = run(WheelspinEffect(), self._spin_frame(rl=0.4, rr=0.0))
        assert max(left) > max(right)

    def test_silent_without_slip(self):
        left, right = run(WheelspinEffect(), self._spin_frame())
        assert max(left) == 0.0 and max(right) == 0.0

    def test_more_slip_means_more_output(self):
        mild = run(WheelspinEffect(), self._spin_frame(rl=0.1, rr=0.1))[0]
        severe = run(WheelspinEffect(), self._spin_frame(rl=0.45, rr=0.45))[0]
        assert max(severe) > max(mild)

    def test_rate_climbs_with_slip(self):
        mild = WheelspinEffect()
        run(mild, self._spin_frame(rl=0.08, rr=0.08), ticks=200, skip=199)
        severe = WheelspinEffect()
        run(severe, self._spin_frame(rl=0.45, rr=0.45), ticks=200, skip=199)
        assert severe._left_osc.frequency > mild._left_osc.frequency

    def test_negative_slip_is_ignored(self):
        left, right = run(WheelspinEffect(), self._spin_frame(rl=-0.4, rr=-0.4))
        assert max(left) == 0.0 and max(right) == 0.0


# --------------------------------------------------------------------------
class TestCollision:
    def _impact_frame(self, impact):
        return TelemetryFrame(
            valid=True, speed_kph=150, rpm=9000, max_rpm=12000, impact=impact
        )

    def test_impact_fires_immediately(self):
        effect = CollisionEffect()
        assert effect.update(DT, self._impact_frame(1.0)).left > 0.8

    def test_decays_to_silence(self):
        effect = CollisionEffect()
        effect.update(DT, self._impact_frame(1.0))
        quiet = self._impact_frame(0.0)
        for _ in range(400):
            effect.update(DT, quiet)
        assert effect.last_output.peak == 0.0

    def test_heavier_impacts_ring_longer(self):
        def ring_time(magnitude):
            effect = CollisionEffect()
            effect.update(DT, self._impact_frame(magnitude))
            quiet = self._impact_frame(0.0)
            elapsed = 0.0
            for _ in range(500):
                if effect.update(DT, quiet).left <= 0.001:
                    break
                elapsed += DT
            return elapsed

        assert ring_time(1.0) > ring_time(0.2)

    def test_below_threshold_is_ignored(self):
        effect = CollisionEffect()
        assert effect.update(DT, self._impact_frame(0.01)).left == 0.0

    def test_dominates_everything_else(self):
        assert CollisionEffect.priority > max(
            c.priority for c in EFFECT_CLASSES if c is not CollisionEffect
        )
        assert CollisionEffect.dominance == 1.0


# --------------------------------------------------------------------------
class TestSurface:
    def _surface_frame(self, surface, speed=120.0):
        return TelemetryFrame(
            valid=True, speed_kph=speed, rpm=8000, max_rpm=12000,
            surfaces=Surfaces(fl=surface, fr=surface, rl=surface, rr=surface),
        )

    def test_gravel_is_stronger_than_grass(self):
        gravel = run(SurfaceEffect(), self._surface_frame(SurfaceType.GRAVEL))[0]
        grass = run(SurfaceEffect(), self._surface_frame(SurfaceType.GRASS))[0]
        assert max(gravel) > max(grass)

    def test_tarmac_is_silent(self):
        left, right = run(SurfaceEffect(), self._surface_frame(SurfaceType.TARMAC))
        assert max(left) == 0.0 and max(right) == 0.0

    def test_gravel_is_irregular_rather_than_periodic(self):
        """A clean waveform here would read as a synthetic tone."""
        left, _ = run(SurfaceEffect(), self._surface_frame(SurfaceType.GRAVEL), ticks=600, skip=100)
        unique = len({round(value, 3) for value in left})
        assert unique > 50

    def test_only_the_wheels_off_track_are_felt(self):
        frame = TelemetryFrame(
            valid=True, speed_kph=120, rpm=8000, max_rpm=12000,
            surfaces=Surfaces(
                fl=SurfaceType.GRAVEL, rl=SurfaceType.GRAVEL,
                fr=SurfaceType.TARMAC, rr=SurfaceType.TARMAC,
            ),
        )
        left, right = run(SurfaceEffect(), frame)
        assert max(left) > 0.1
        assert max(right) == 0.0
