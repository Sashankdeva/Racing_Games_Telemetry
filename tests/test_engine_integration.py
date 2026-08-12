"""End-to-end engine behaviour with several effects live at once.

The brief's headline requirement is that "RPM + kerb + wheelspin + gear
shift must be mixed intelligently" and that the combined result feels like
a coherent car rather than random vibration. These tests exercise exactly
that path: telemetry in, mixed motor drive out.
"""

from __future__ import annotations

import time

import pytest

from app.controller.base import NullController
from app.core.events import EventBus
from app.core.models import Surfaces, SurfaceType, TelemetryFrame, Wheels
from app.haptics.engine import HapticEngine, MasterSettings

DT = 1 / 120


@pytest.fixture
def engine():
    instance = HapticEngine(NullController(), EventBus(), tick_rate=120)
    yield instance
    instance.stop()


def drive(engine, frame, ticks=180, skip=40):
    left, right = [], []
    for index in range(ticks):
        frame.timestamp = time.perf_counter()
        engine.submit_telemetry(frame)
        engine._tick(DT)
        if index >= skip:
            snapshot = engine.snapshot()
            left.append(snapshot.left)
            right.append(snapshot.right)
    return left, right


def racing_frame(**overrides) -> TelemetryFrame:
    defaults = dict(
        valid=True, rpm=10500, max_rpm=12000, idle_rpm=4000,
        speed_kph=230, gear=6, throttle=1.0,
    )
    defaults.update(overrides)
    return TelemetryFrame(**defaults)


class TestSimultaneousEffects:
    def test_everything_at_once_stays_in_range(self, engine):
        frame = racing_frame(
            brake=0.8,
            wheel_slip_ratio=Wheels(-0.3, 0.3, -0.2, 0.4),
            suspension_acceleration=Wheels(40, 40, 40, 40),
            surfaces=Surfaces(
                SurfaceType.RUMBLE_STRIP, SurfaceType.GRAVEL,
                SurfaceType.RUMBLE_STRIP, SurfaceType.GRAVEL,
            ),
            g_lateral=4.0, g_longitudinal=-5.0, impact=0.8,
        )
        left, right = drive(engine, frame, ticks=300)
        assert all(0.0 <= value <= 1.0 for value in left + right)

    def test_multiple_effects_reach_the_mixer_together(self, engine):
        frame = racing_frame(
            wheel_slip_ratio=Wheels(0.0, 0.0, 0.25, 0.30),
            surfaces=Surfaces(
                SurfaceType.RUMBLE_STRIP, SurfaceType.TARMAC,
                SurfaceType.RUMBLE_STRIP, SurfaceType.TARMAC,
            ),
        )
        drive(engine, frame, ticks=120, skip=119)
        active = engine.snapshot().active_effects
        assert "engine_rpm" in active
        assert "kerb" in active
        assert "wheelspin" in active

    def test_a_kerb_is_still_felt_over_a_loud_engine(self, engine):
        """The engine bed has low dominance precisely so it cannot mask
        the sharp events that carry the actual information."""
        engine_only = racing_frame()
        with_kerb = racing_frame(
            surfaces=Surfaces(
                SurfaceType.RUMBLE_STRIP, SurfaceType.TARMAC,
                SurfaceType.RUMBLE_STRIP, SurfaceType.TARMAC,
            )
        )
        baseline, _ = drive(engine, engine_only)
        engine.safe_stop_all()
        combined, _ = drive(engine, with_kerb)

        assert max(combined) > max(baseline)

    def test_a_collision_briefly_dominates_everything(self, engine):
        busy = racing_frame(
            wheel_slip_ratio=Wheels(0.0, 0.0, 0.3, 0.3),
            surfaces=Surfaces(
                SurfaceType.GRAVEL, SurfaceType.GRAVEL,
                SurfaceType.GRAVEL, SurfaceType.GRAVEL,
            ),
        )
        drive(engine, busy, ticks=60)

        impact = racing_frame(
            wheel_slip_ratio=busy.wheel_slip_ratio,
            surfaces=busy.surfaces,
            impact=1.0,
        )
        impact.timestamp = time.perf_counter()
        engine.submit_telemetry(impact)
        engine._tick(DT)

        assert engine.snapshot().active_effects[0] == "collision"

    def test_left_and_right_stay_independent_end_to_end(self, engine):
        frame = racing_frame(
            surfaces=Surfaces(
                fl=SurfaceType.GRAVEL, rl=SurfaceType.GRAVEL,
                fr=SurfaceType.TARMAC, rr=SurfaceType.TARMAC,
            ),
        )
        # Engine/road beds are symmetric, so isolate the per-wheel effects.
        for effect in engine.effects:
            if effect.id not in ("surface", "kerb", "wheelspin", "abs_lock"):
                effect.settings.enabled = False

        left, right = drive(engine, frame)
        assert max(left) > max(right)


class TestMasterControls:
    def test_master_intensity_scales_output(self, engine):
        frame = racing_frame()
        engine.set_master(MasterSettings(intensity=1.0))
        loud, _ = drive(engine, frame)

        engine.safe_stop_all()
        engine.set_master(MasterSettings(intensity=0.3))
        quiet, _ = drive(engine, frame)

        assert max(quiet) < max(loud)

    def test_zero_intensity_is_silence(self, engine):
        engine.set_master(MasterSettings(intensity=0.0))
        left, right = drive(engine, racing_frame())
        assert max(left) == 0.0 and max(right) == 0.0

    def test_global_smoothing_is_off_by_default(self, engine):
        assert engine.master.global_smoothing == 0.0

    def test_global_smoothing_reduces_variation_when_enabled(self, engine):
        frame = racing_frame()
        engine.set_master(MasterSettings(intensity=1.0, global_smoothing=0.0))
        raw, _ = drive(engine, frame)

        engine.safe_stop_all()
        engine.set_master(MasterSettings(intensity=1.0, global_smoothing=1.0))
        smoothed, _ = drive(engine, frame)

        def variation(samples):
            return sum(abs(b - a) for a, b in zip(samples, samples[1:]))

        assert variation(smoothed) < variation(raw)

    def test_response_control_changes_slew(self, engine):
        engine.set_master(MasterSettings(response=0.0))
        slow = engine._motor_left.config.slew_rise
        engine.set_master(MasterSettings(response=1.0))
        assert engine._motor_left.config.slew_rise > slow


class TestLoopHealth:
    def test_measured_rate_tracks_the_target(self):
        instance = HapticEngine(NullController(), EventBus(), tick_rate=120)
        instance.start()
        try:
            instance.submit_telemetry(racing_frame())
            time.sleep(1.0)
            rate = instance.snapshot().tick_rate
            # Generous bound: CI machines are noisy, but a wildly wrong rate
            # would mean the loop is broken rather than merely busy.
            assert 60 < rate < 160
        finally:
            instance.stop()

    def test_a_long_stall_does_not_slam_envelopes_through(self, engine):
        """A debugger pause or suspend must not integrate as one huge step."""
        engine.submit_telemetry(racing_frame())
        engine._tick(5.0)  # pathological dt
        assert 0.0 <= engine.snapshot().left <= 1.0

    def test_effects_reset_cleanly_between_sessions(self, engine):
        drive(engine, racing_frame(), ticks=60)
        engine.safe_stop_all()
        for effect in engine.effects:
            assert effect.last_output.peak == 0.0
