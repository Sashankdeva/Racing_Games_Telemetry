"""The haptic engine: the fixed-rate loop that turns telemetry into rumble.

Runs on its own thread so neither the UI nor the UDP listener can stall
motor output, and so a slow repaint never turns into a stuck vibration.

Signal path per tick:

    telemetry snapshot (dropped if stale)
        -> effects, each with its own signal character
        -> scheduler cues (Test Lab)
        -> mixer: priority ducking + soft limit
        -> master gain, dynamic range
        -> optional global smoothing (OFF by default, see below)
        -> per-motor physical model
        -> controller

Global smoothing is deliberately disabled by default. Per-effect processing
is what makes a gear shift feel different from body float; a single filter
across the sum would flatten both into the same mush. It is kept only as an
opt-in comfort control.

Three independent safety mechanisms guard the motors:

  1. Stale telemetry cutoff - the engine substitutes an invalid frame once
     data stops arriving, so effects fall silent on their own.
  2. Controller disconnect cutoff - driven by DeviceManager events.
  3. Watchdog - a separate thread that force-stops the hardware if the loop
     itself stops ticking while output is live.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from app.controller.base import ControllerBackend
from app.core.events import Event, EventBus
from app.core.logging import RateLimitedLogger, get_logger
from app.core.models import NO_TELEMETRY, TelemetryFrame
from app.haptics.effects import Effect, EffectSettings, create_all
from app.haptics.mixer import Contribution, HapticMixer
from app.haptics.motor import Motor, MotorConfig
from app.haptics.patterns import PatternSpec
from app.haptics.scheduler import HapticScheduler
from app.haptics.signal import OnePole, apply_response, clamp, lerp

_log = get_logger(__name__)
_rate_log = RateLimitedLogger(_log)

DEFAULT_TICK_RATE = 120.0
#: Loop must tick at least this often or the watchdog cuts output.
WATCHDOG_TIMEOUT = 1.0
WATCHDOG_INTERVAL = 0.25


@dataclass(frozen=True, slots=True)
class EngineSnapshot:
    """Immutable view for the UI. Polled; never pushed."""

    left: float = 0.0
    right: float = 0.0
    left_intent: float = 0.0
    right_intent: float = 0.0
    active_effects: tuple[str, ...] = ()
    tick_rate: float = 0.0
    telemetry_valid: bool = False
    telemetry_age: float = 0.0
    emergency_stop: bool = False
    limited: bool = False
    running: bool = False
    controller_connected: bool = False
    rpm: float = 0.0
    max_rpm: float = 0.0
    speed_kph: float = 0.0
    gear: int = 0


@dataclass(slots=True)
class MasterSettings:
    """Global shaping applied after the mixer, before the motor model."""

    intensity: float = 1.0  # 0..1.5
    #: 1.0 keeps full contrast; lower compresses quiet effects upward.
    dynamic_range: float = 1.0
    #: 0 soft/rounded .. 1 firm/immediate. Maps onto the motor curve.
    feel: float = 0.55
    #: 0 sluggish .. 1 instant. Maps onto motor slew rates.
    response: float = 0.85
    #: Opt-in only; 0 disables the global filter entirely.
    global_smoothing: float = 0.0
    output_limit: float = 1.0


class HapticEngine:
    def __init__(
        self,
        controller: ControllerBackend,
        bus: EventBus | None = None,
        tick_rate: float = DEFAULT_TICK_RATE,
    ) -> None:
        self.controller = controller
        self.bus = bus or EventBus()
        self._tick_rate = clamp(tick_rate, 30.0, 250.0)

        self.mixer = HapticMixer()
        self.scheduler = HapticScheduler()
        self.effects: list[Effect] = create_all()
        self._effects_by_id = {e.id: e for e in self.effects}

        self.master = MasterSettings()
        self.motor_config = MotorConfig()
        self._motor_left = Motor(self.motor_config)
        self._motor_right = Motor(self.motor_config)
        self._smooth_left = OnePole(20.0)
        self._smooth_right = OnePole(20.0)

        self._telemetry: TelemetryFrame = NO_TELEMETRY
        self._telemetry_timeout = 0.5
        self._telemetry_lock = threading.Lock()

        self._emergency_stop = False
        self._controller_connected = False

        self._state_lock = threading.Lock()
        self._snapshot = EngineSnapshot()
        self._contributions: list[Contribution] = []

        self._running = threading.Event()
        self._thread: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self._last_tick = 0.0
        self._measured_rate = 0.0
        self._smoothed_dt = 0.0

        self.bus.subscribe(Event.CONTROLLER_DISCONNECTED, self._on_controller_lost)
        self.bus.subscribe(Event.CONTROLLER_CONNECTED, self._on_controller_found)

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------
    @property
    def tick_rate(self) -> float:
        return self._tick_rate

    def set_tick_rate(self, rate: float) -> None:
        self._tick_rate = clamp(rate, 30.0, 250.0)

    def set_telemetry_timeout(self, seconds: float) -> None:
        self._telemetry_timeout = max(0.05, seconds)

    def set_master(self, master: MasterSettings) -> None:
        self.master = master
        self._apply_feel_and_response()

    def set_motor_config(self, config: MotorConfig) -> None:
        self.motor_config = config
        self._apply_feel_and_response()

    def _apply_feel_and_response(self) -> None:
        """Fold the two friendly Haptics-page knobs into the motor model."""
        config = MotorConfig(
            min_effective=self.motor_config.min_effective,
            max_output=self.motor_config.max_output,
            # Firmer feel = lower gamma = more immediate mid-range punch.
            curve=lerp(1.15, 0.65, clamp(self.master.feel)),
            slew_rise=lerp(12.0, 200.0, clamp(self.master.response)),
            slew_fall=lerp(8.0, 120.0, clamp(self.master.response)),
            slew_enabled=self.motor_config.slew_enabled,
        )
        self._motor_left.set_config(config)
        self._motor_right.set_config(config)

    def apply_effect_settings(self, settings_by_id: dict[str, EffectSettings]) -> None:
        for effect_id, settings in settings_by_id.items():
            effect = self._effects_by_id.get(effect_id)
            if effect is not None:
                effect.apply_settings(settings.copy())

    def effect_by_id(self, effect_id: str) -> Effect | None:
        return self._effects_by_id.get(effect_id)

    # ------------------------------------------------------------------
    # telemetry input
    # ------------------------------------------------------------------
    def submit_telemetry(self, frame: TelemetryFrame) -> None:
        """Called from the telemetry thread. Latest frame wins."""
        with self._telemetry_lock:
            self._telemetry = frame

    def clear_telemetry(self) -> None:
        with self._telemetry_lock:
            self._telemetry = NO_TELEMETRY

    def _current_telemetry(self) -> tuple[TelemetryFrame, float]:
        with self._telemetry_lock:
            frame = self._telemetry
        if not frame.valid:
            return NO_TELEMETRY, 0.0
        age = frame.age()
        if age > self._telemetry_timeout:
            # Stale data must never keep the motors alive.
            return NO_TELEMETRY, age
        return frame, age

    # ------------------------------------------------------------------
    # test lab
    # ------------------------------------------------------------------
    def play_test_pattern(self, spec: PatternSpec, cue_id: str = "test_lab") -> None:
        if self._emergency_stop:
            return
        self.scheduler.play(cue_id, spec)

    def stop_test_patterns(self) -> None:
        self.scheduler.clear()

    # ------------------------------------------------------------------
    # safety
    # ------------------------------------------------------------------
    @property
    def emergency_stop_active(self) -> bool:
        return self._emergency_stop

    def emergency_stop(self) -> None:
        """Latch all output off until explicitly cleared."""
        self._emergency_stop = True
        self.scheduler.clear()
        self._silence_now()
        _log.warning("EMERGENCY STOP engaged")
        self.bus.emit(Event.EMERGENCY_STOP)

    def clear_emergency_stop(self) -> None:
        if not self._emergency_stop:
            return
        self._emergency_stop = False
        for effect in self.effects:
            effect.reset()
        _log.info("Emergency stop cleared")
        self.bus.emit(Event.EMERGENCY_STOP_CLEARED)

    def safe_stop_all(self) -> None:
        """Immediate, unconditional silence. Safe to call from anywhere."""
        self.scheduler.clear()
        for effect in self.effects:
            effect.reset()
        self._silence_now()

    def _silence_now(self) -> None:
        self._motor_left.snap_to_zero()
        self._motor_right.snap_to_zero()
        self._smooth_left.reset(0.0)
        self._smooth_right.reset(0.0)
        try:
            self.controller.stop()
        except Exception:  # noqa: BLE001 - stopping must never raise
            _log.exception("Failed to stop controller")

    def _on_controller_lost(self, **_) -> None:
        self._controller_connected = False
        self.safe_stop_all()
        self.bus.emit(Event.SAFETY_CUTOFF, reason="controller_disconnected")

    def _on_controller_found(self, **_) -> None:
        self._controller_connected = True

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running.is_set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running.set()
        self._last_tick = time.perf_counter()
        self._thread = threading.Thread(target=self._run, name="haptic-engine", daemon=True)
        self._thread.start()
        self._watchdog = threading.Thread(
            target=self._run_watchdog, name="haptic-watchdog", daemon=True
        )
        self._watchdog.start()
        _log.info("Haptic engine started at %.0f Hz", self._tick_rate)

    def stop(self) -> None:
        self._running.clear()
        for thread in (self._thread, self._watchdog):
            if thread is not None:
                thread.join(timeout=2.0)
        self._thread = None
        self._watchdog = None
        self._silence_now()
        _log.info("Haptic engine stopped")

    def __enter__(self) -> "HapticEngine":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.safe_stop_all()
        self.stop()

    # ------------------------------------------------------------------
    # the loop
    # ------------------------------------------------------------------
    def _run(self) -> None:
        previous = time.perf_counter()
        try:
            while self._running.is_set():
                now = time.perf_counter()
                dt = now - previous
                previous = now
                # A long stall (debugger, suspend) must not integrate as one
                # giant step and slam every envelope to completion.
                if dt > 0.25:
                    dt = 1.0 / self._tick_rate

                try:
                    self._tick(dt)
                except Exception:  # noqa: BLE001 - never let the loop die
                    _rate_log.error("tick", "Haptic tick failed; silencing motors")
                    _log.debug("Tick exception detail", exc_info=True)
                    self._silence_now()

                self._last_tick = time.perf_counter()
                # Average the tick *period*, then invert - never the other
                # way round. Averaging 1/dt over a jittery period (which
                # Windows sleep granularity guarantees) is biased high by
                # Jensen's inequality and over-reports the rate by tens of Hz.
                if dt > 0.0:
                    if self._smoothed_dt <= 0.0:
                        self._smoothed_dt = dt
                    else:
                        self._smoothed_dt += (dt - self._smoothed_dt) * 0.08
                    self._measured_rate = 1.0 / self._smoothed_dt

                interval = 1.0 / self._tick_rate
                remaining = interval - (time.perf_counter() - now)
                if remaining > 0:
                    time.sleep(remaining)
        finally:
            # Whatever happens - exception, stop request, interpreter exit -
            # the hardware ends up silent.
            self._silence_now()

    def _tick(self, dt: float) -> None:
        if self._emergency_stop:
            self._motor_left.snap_to_zero()
            self._motor_right.snap_to_zero()
            self.controller.set_motors(0.0, 0.0)
            self._publish(0.0, 0.0, 0.0, 0.0, (), False, NO_TELEMETRY, 0.0)
            return

        telemetry, age = self._current_telemetry()

        contributions = self._contributions
        contributions.clear()

        for effect in self.effects:
            output = effect.update(dt, telemetry)
            duck = effect.duck()
            # A duck outlives the sound that caused it, so an effect still
            # contributes while silent if it is holding the bed down.
            if output.left > 0.0 or output.right > 0.0 or duck > 0.0:
                contributions.append(
                    Contribution(
                        effect_id=effect.id,
                        left=output.left,
                        right=output.right,
                        priority=effect.priority,
                        dominance=effect.dominance,
                        duck=duck,
                    )
                )

        self.scheduler.update(dt, contributions)

        mixed = self.mixer.mix(contributions)

        left = self._apply_master(mixed.left)
        right = self._apply_master(mixed.right)

        if self.master.global_smoothing > 0.0:
            cutoff = lerp(30.0, 3.0, clamp(self.master.global_smoothing))
            self._smooth_left.cutoff_hz = cutoff
            self._smooth_right.cutoff_hz = cutoff
            left = self._smooth_left.update(left, dt)
            right = self._smooth_right.update(right, dt)

        drive_left = self._motor_left.update(left, dt)
        drive_right = self._motor_right.update(right, dt)

        self.controller.set_motors(drive_left, drive_right)

        self._publish(
            drive_left,
            drive_right,
            left,
            right,
            tuple(mixed.active),
            mixed.limited,
            telemetry,
            age,
        )

    def _apply_master(self, value: float) -> float:
        value = clamp(value * clamp(self.master.intensity, 0.0, 1.5))
        # Dynamic range: 1.0 untouched, lower lifts quiet effects toward loud
        # ones (less contrast), which some users prefer on weaker motors.
        dynamic = clamp(self.master.dynamic_range)
        if dynamic < 1.0:
            value = apply_response(value, lerp(0.40, 1.0, dynamic))
        return clamp(value * clamp(self.master.output_limit, 0.0, 1.0))

    def _publish(
        self,
        left: float,
        right: float,
        left_intent: float,
        right_intent: float,
        active: tuple[str, ...],
        limited: bool,
        telemetry: TelemetryFrame,
        age: float,
    ) -> None:
        snapshot = EngineSnapshot(
            left=left,
            right=right,
            left_intent=left_intent,
            right_intent=right_intent,
            active_effects=active,
            tick_rate=self._measured_rate,
            telemetry_valid=telemetry.valid,
            telemetry_age=age,
            emergency_stop=self._emergency_stop,
            limited=limited,
            running=self._running.is_set(),
            controller_connected=self._controller_connected,
            rpm=telemetry.rpm,
            max_rpm=telemetry.max_rpm,
            speed_kph=telemetry.speed_kph,
            gear=telemetry.gear,
        )
        with self._state_lock:
            self._snapshot = snapshot

    def snapshot(self) -> EngineSnapshot:
        with self._state_lock:
            return self._snapshot

    # ------------------------------------------------------------------
    # watchdog
    # ------------------------------------------------------------------
    def _run_watchdog(self) -> None:
        """Force-stops the hardware if the main loop stops ticking.

        Guards the one failure mode the loop cannot guard itself against: a
        hang while the motors are already commanded on.
        """
        while self._running.is_set():
            time.sleep(WATCHDOG_INTERVAL)
            if not self._running.is_set():
                break
            since = time.perf_counter() - self._last_tick
            if since > WATCHDOG_TIMEOUT:
                snapshot = self.snapshot()
                if snapshot.left > 0.0 or snapshot.right > 0.0:
                    _rate_log.error(
                        "watchdog",
                        "Haptic loop stalled for %.2fs with motors live - forcing stop",
                        since,
                    )
                    self._silence_now()
                    self.bus.emit(Event.SAFETY_CUTOFF, reason="loop_stalled")
