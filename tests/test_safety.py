"""Safety mechanisms.

The single guarantee this application makes is that the motors stop. These
tests cover every path to that outcome: emergency stop, stale telemetry,
controller disconnect, exceptions inside the loop, and shutdown.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.controller.base import ControllerBackend, DeviceInfo, NullController
from app.core.events import Event, EventBus
from app.core.models import TelemetryFrame
from app.haptics.engine import HapticEngine
from app.haptics.patterns import MAX_TEST_DURATION, PatternKind, PatternSpec

DT = 1 / 120


class RecordingController(NullController):
    """Records every write so a test can assert the final command was zero."""

    def __init__(self):
        super().__init__()
        self.writes: list[tuple[float, float]] = []

    def set_motors(self, left: float, right: float) -> bool:
        self.writes.append((left, right))
        return super().set_motors(left, right)

    def stop(self) -> bool:
        self.writes.append((0.0, 0.0))
        return super().stop()


def loud_frame(**overrides) -> TelemetryFrame:
    defaults = dict(
        valid=True, rpm=11000, max_rpm=12000, idle_rpm=4000,
        speed_kph=250, gear=6, throttle=1.0,
    )
    defaults.update(overrides)
    return TelemetryFrame(**defaults)


@pytest.fixture
def engine():
    controller = RecordingController()
    instance = HapticEngine(controller, EventBus(), tick_rate=120)
    yield instance
    instance.stop()


class TestEmergencyStop:
    def test_latches_output_off(self, engine):
        engine.submit_telemetry(loud_frame())
        for _ in range(30):
            engine._tick(DT)
        assert engine.snapshot().left > 0.0

        engine.emergency_stop()
        engine.submit_telemetry(loud_frame())
        for _ in range(30):
            engine._tick(DT)

        assert engine.snapshot().left == 0.0
        assert engine.snapshot().right == 0.0
        assert engine.controller.last_left == 0.0

    def test_stays_engaged_until_explicitly_cleared(self, engine):
        engine.emergency_stop()
        for _ in range(120):
            engine.submit_telemetry(loud_frame())
            engine._tick(DT)
        assert engine.snapshot().left == 0.0

        engine.clear_emergency_stop()
        engine.submit_telemetry(loud_frame())
        for _ in range(30):
            engine._tick(DT)
        assert engine.snapshot().left > 0.0

    def test_blocks_test_patterns(self, engine):
        engine.emergency_stop()
        engine.play_test_pattern(PatternSpec(PatternKind.CONSTANT, 1.0, 2.0))
        assert engine.scheduler.active_count == 0

    def test_emits_an_event(self):
        bus = EventBus()
        seen = []
        bus.subscribe(Event.EMERGENCY_STOP, lambda **kw: seen.append(kw))
        instance = HapticEngine(NullController(), bus)
        instance.emergency_stop()
        assert len(seen) == 1


class TestStaleTelemetry:
    def test_old_frames_are_discarded(self, engine):
        engine.set_telemetry_timeout(0.2)
        engine.submit_telemetry(loud_frame())
        for _ in range(30):
            engine._tick(DT)
        assert engine.snapshot().left > 0.0

        # Do not submit anything further; let the frame age out.
        time.sleep(0.35)
        for _ in range(60):
            engine._tick(DT)

        assert engine.snapshot().left == 0.0
        assert engine.snapshot().telemetry_valid is False

    def test_a_frame_with_a_stale_timestamp_never_produces_output(self, engine):
        engine.set_telemetry_timeout(0.2)
        stale = loud_frame()
        stale.timestamp = time.perf_counter() - 5.0
        engine.submit_telemetry(stale)
        for _ in range(30):
            engine._tick(DT)
        assert engine.snapshot().left == 0.0

    def test_clear_telemetry_silences_immediately(self, engine):
        engine.submit_telemetry(loud_frame())
        for _ in range(30):
            engine._tick(DT)
        engine.clear_telemetry()
        for _ in range(30):
            engine._tick(DT)
        assert engine.snapshot().left == 0.0

    def test_invalid_frames_are_ignored(self, engine):
        engine.submit_telemetry(TelemetryFrame(valid=False, rpm=11000, max_rpm=12000))
        for _ in range(30):
            engine._tick(DT)
        assert engine.snapshot().left == 0.0


class TestControllerDisconnect:
    def test_disconnect_event_cuts_all_output(self):
        bus = EventBus()
        controller = RecordingController()
        instance = HapticEngine(controller, bus, tick_rate=120)

        instance.submit_telemetry(loud_frame())
        for _ in range(30):
            instance._tick(DT)
        assert instance.snapshot().left > 0.0

        bus.emit(Event.CONTROLLER_DISCONNECTED, index=0)

        assert controller.writes[-1] == (0.0, 0.0)
        instance.stop()

    def test_disconnect_resets_effect_state(self):
        bus = EventBus()
        instance = HapticEngine(RecordingController(), bus, tick_rate=120)
        instance.submit_telemetry(loud_frame())
        for _ in range(30):
            instance._tick(DT)

        bus.emit(Event.CONTROLLER_DISCONNECTED, index=0)
        for effect in instance.effects:
            assert effect.last_output.peak == 0.0
        instance.stop()


class TestExceptionSafety:
    def test_a_failing_controller_does_not_kill_the_loop(self):
        class BrokenController(ControllerBackend):
            def __init__(self):
                self.calls = 0

            def is_connected(self):
                return True

            def set_motors(self, left, right):
                self.calls += 1
                raise RuntimeError("hardware exploded")

            def stop(self):
                return True

            def info(self):
                return DeviceInfo("broken", 0, "test", True)

        controller = BrokenController()
        instance = HapticEngine(controller, EventBus(), tick_rate=200)
        instance.start()
        instance.submit_telemetry(loud_frame())
        time.sleep(0.3)

        assert instance.running  # survived
        assert controller.calls > 1  # kept trying
        instance.stop()

    def test_stop_always_leaves_the_hardware_silent(self):
        controller = RecordingController()
        instance = HapticEngine(controller, EventBus(), tick_rate=120)
        instance.start()
        instance.submit_telemetry(loud_frame())
        time.sleep(0.2)
        instance.stop()

        assert controller.writes[-1] == (0.0, 0.0)

    def test_context_manager_silences_on_exception(self):
        controller = RecordingController()
        with pytest.raises(ValueError):
            with HapticEngine(controller, EventBus(), tick_rate=120) as instance:
                instance.submit_telemetry(loud_frame())
                time.sleep(0.15)
                raise ValueError("boom")
        assert controller.writes[-1] == (0.0, 0.0)


class TestOutputBounds:
    def test_output_never_leaves_the_valid_range(self, engine):
        engine.master.intensity = 1.5
        for effect in engine.effects:
            effect.settings.intensity = 2.0

        engine.submit_telemetry(
            loud_frame(impact=1.0, brake=1.0, g_lateral=6.0, g_longitudinal=-6.0)
        )
        for _ in range(200):
            engine._tick(DT)
            snapshot = engine.snapshot()
            assert 0.0 <= snapshot.left <= 1.0
            assert 0.0 <= snapshot.right <= 1.0

    def test_output_limit_is_respected(self, engine):
        engine.master.output_limit = 0.3
        engine.submit_telemetry(loud_frame())
        for _ in range(120):
            engine._tick(DT)
            assert engine.snapshot().left_intent <= 0.3 + 1e-9


class TestTestPatternLimits:
    def test_duration_is_hard_capped(self):
        spec = PatternSpec(PatternKind.CONSTANT, 1.0, duration=9999.0).clamped()
        assert spec.duration <= MAX_TEST_DURATION

    def test_a_pattern_finishes_on_its_own(self, engine):
        engine.play_test_pattern(PatternSpec(PatternKind.CONSTANT, 0.8, duration=0.2))
        for _ in range(120):  # one second
            engine._tick(DT)
        assert engine.scheduler.active_count == 0
        assert engine.snapshot().left == 0.0

    def test_stop_clears_every_cue(self, engine):
        engine.play_test_pattern(PatternSpec(PatternKind.CONSTANT, 0.8, duration=10.0))
        engine.stop_test_patterns()
        assert engine.scheduler.active_count == 0


class TestWatchdog:
    def test_forces_silence_when_the_loop_stalls(self):
        """The one failure the loop cannot catch itself: a hang with the
        motors already commanded on."""
        controller = RecordingController()
        instance = HapticEngine(controller, EventBus(), tick_rate=120)
        instance.submit_telemetry(loud_frame())
        for _ in range(30):
            instance._tick(DT)
        assert instance.snapshot().left > 0.0

        # Simulate a stalled loop: running, but _last_tick far in the past.
        instance._running.set()
        instance._last_tick = time.perf_counter() - 5.0
        watchdog = threading.Thread(target=instance._run_watchdog, daemon=True)
        watchdog.start()
        time.sleep(0.5)
        instance._running.clear()
        watchdog.join(timeout=1.0)

        assert controller.writes[-1] == (0.0, 0.0)


class TestApplicationShutdown:
    def test_shutdown_is_idempotent_and_silences(self):
        from app.config.settings import AppSettings
        from app.core.application import Application

        app = Application(AppSettings(auto_start_telemetry=False))
        app.startup()
        app.engine.submit_telemetry(loud_frame())
        time.sleep(0.15)

        app.shutdown()
        app.shutdown()  # must not raise

        assert not app.engine.running
        assert app.controller.last_intensities == (0.0, 0.0)
