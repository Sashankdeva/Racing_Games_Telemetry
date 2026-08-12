"""Application composition root.

Owns every long-lived component and the wiring between them, with no Qt
dependency at all - the UI is one consumer of this object, and the headless
CLI in app.cli is another. Keeping the orchestration here is what lets the
whole engine be driven and tested without a display.

Shutdown is the important part: `shutdown()` is idempotent, safe to call
from an exception handler, and always ends with the motors silent.
"""

from __future__ import annotations

import threading

from app.config.settings import AppSettings
from app.controller.blitz import XInputController
from app.controller.device_manager import DeviceManager
from app.core.events import EventBus
from app.core.logging import get_logger, setup_logging
from app.core.models import TelemetryFrame
from app.diagnostics.metrics import DiagnosticsCollector, DiagnosticsReport
from app.games.base import GameAdapter
from app.games.registry import create_adapters
from app.haptics.engine import HapticEngine, MasterSettings
from app.haptics.patterns import PatternSpec
from app.profiles.manager import ProfileManager
from app.profiles.schema import Profile

_log = get_logger(__name__)


class Application:
    def __init__(self, settings: AppSettings | None = None) -> None:
        self.settings = settings or AppSettings.load()
        setup_logging(verbose=self.settings.verbose_logging)

        self.bus = EventBus()

        self.controller = XInputController(
            index=self.settings.controller_index,
            output_limit=self.settings.master_output_limit,
        )
        self.device_manager = DeviceManager(
            self.controller, self.bus, auto_detect=self.settings.auto_detect_controller
        )
        self.engine = HapticEngine(
            self.controller, self.bus, tick_rate=self.settings.update_rate_hz
        )
        self.engine.set_telemetry_timeout(self.settings.telemetry_timeout)

        self.profiles = ProfileManager(self.bus)
        self.adapters: dict[str, GameAdapter] = {
            adapter.game_id: adapter for adapter in create_adapters()
        }
        self.diagnostics = DiagnosticsCollector(self.controller, self.engine)

        self._active_adapter: GameAdapter | None = None
        self._started = False
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False

        self._restore_active_profile()
        self._select_adapter(self.settings.game_id)

    # ------------------------------------------------------------------
    # profiles
    # ------------------------------------------------------------------
    def _restore_active_profile(self) -> None:
        slug = self.settings.active_profile
        if self.profiles.get(slug) is not None:
            self.profiles.set_active(slug)
        self.apply_profile(self.profiles.active)

    def apply_profile(self, profile: Profile) -> None:
        """Push a profile into the live engine."""
        profile.normalize()

        self.engine.set_master(
            MasterSettings(
                intensity=profile.master.intensity,
                dynamic_range=profile.master.dynamic_range,
                feel=profile.master.feel,
                response=profile.master.response,
                global_smoothing=profile.master.global_smoothing,
                output_limit=profile.master.output_limit,
            )
        )
        self.engine.set_motor_config(profile.motor)
        self.engine.apply_effect_settings(profile.effects)
        _log.info("Applied profile '%s'", profile.name)

    def set_active_profile(self, slug: str) -> Profile:
        profile = self.profiles.set_active(slug)
        self.apply_profile(profile)
        self.settings.active_profile = profile.slug
        self.settings.save()
        return profile

    def save_active_profile(self) -> bool:
        return self.profiles.save(self.profiles.active)

    # ------------------------------------------------------------------
    # game adapters
    # ------------------------------------------------------------------
    @property
    def active_adapter(self) -> GameAdapter | None:
        return self._active_adapter

    def _select_adapter(self, game_id: str) -> None:
        adapter = self.adapters.get(game_id)
        if adapter is None:
            adapter = next(iter(self.adapters.values()), None)
        self._active_adapter = adapter
        self.diagnostics.set_adapter(adapter)
        if adapter is not None:
            adapter.set_frame_callback(self._on_telemetry_frame)
            adapter.configure(
                port=self.settings.udp_port,
                connection_timeout=self.settings.connection_timeout,
            )

    def set_game(self, game_id: str) -> None:
        if self._active_adapter and self._active_adapter.game_id == game_id:
            return
        self.stop_telemetry()
        self.settings.game_id = game_id
        self.settings.save()
        self._select_adapter(game_id)
        if self._started and self.settings.auto_start_telemetry:
            self.start_telemetry()

    def _on_telemetry_frame(self, frame: TelemetryFrame) -> None:
        self.engine.submit_telemetry(frame)

    def start_telemetry(self) -> None:
        adapter = self._active_adapter
        if adapter is None or not adapter.supported:
            return
        adapter.start()

    def stop_telemetry(self) -> None:
        adapter = self._active_adapter
        if adapter is None:
            return
        adapter.stop()
        self.engine.clear_telemetry()

    def set_udp_port(self, port: int) -> None:
        self.settings.udp_port = port
        self.settings.save()
        if self._active_adapter is not None:
            self._active_adapter.configure(port=port)

    # ------------------------------------------------------------------
    # controller
    # ------------------------------------------------------------------
    def set_controller_index(self, index: int) -> None:
        self.controller.set_index(index)
        self.settings.controller_index = index
        self.settings.save()
        self.device_manager.check_now()

    def set_output_limit(self, limit: float) -> None:
        self.controller.set_output_limit(limit)
        self.settings.master_output_limit = limit
        self.settings.save()

    def set_update_rate(self, hz: float) -> None:
        self.engine.set_tick_rate(hz)
        self.settings.update_rate_hz = self.engine.tick_rate
        self.settings.save()

    # ------------------------------------------------------------------
    # test lab
    # ------------------------------------------------------------------
    def play_test_pattern(self, spec: PatternSpec) -> None:
        self.engine.play_test_pattern(spec)

    def stop_test_patterns(self) -> None:
        self.engine.stop_test_patterns()

    # ------------------------------------------------------------------
    # safety
    # ------------------------------------------------------------------
    def emergency_stop(self) -> None:
        self.engine.emergency_stop()

    def clear_emergency_stop(self) -> None:
        self.engine.clear_emergency_stop()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @property
    def started(self) -> bool:
        return self._started

    def startup(self) -> None:
        if self._started:
            return
        _log.info("Starting Racing Haptic Engine")

        self.device_manager.start()
        self.device_manager.check_now()

        if self.settings.start_engine_on_launch:
            self.engine.start()
        if self.settings.auto_start_telemetry:
            self.start_telemetry()

        self._started = True

    def shutdown(self) -> None:
        """Stop everything and silence the motors. Safe to call twice."""
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True

        _log.info("Shutting down")

        # Order matters: silence first, then tear down the producers.
        try:
            self.engine.safe_stop_all()
        except Exception:  # noqa: BLE001
            _log.exception("Error during emergency silence")

        for step, action in (
            ("telemetry", self.stop_telemetry),
            ("engine", self.engine.stop),
            ("device manager", self.device_manager.stop),
        ):
            try:
                action()
            except Exception:  # noqa: BLE001 - one failure must not skip the rest
                _log.exception("Error stopping %s", step)

        # Belt and braces: hit the hardware directly one last time.
        try:
            self.controller.stop()
        except Exception:  # noqa: BLE001
            _log.exception("Final controller stop failed")

        try:
            self.settings.save()
        except Exception:  # noqa: BLE001
            _log.exception("Could not save settings on exit")

        self._started = False
        _log.info("Shutdown complete - motors stopped")

    def report(self) -> DiagnosticsReport:
        return self.diagnostics.collect()

    def __enter__(self) -> "Application":
        self.startup()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()
