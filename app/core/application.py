"""Application composition root.

Owns the long-lived components and the wiring between them, with no Qt
dependency - the UI is one consumer of this object and the headless/CLI
modes are others.

Game modes are switched *hot*. Nothing here needs a restart: the telemetry
listener is stopped and restarted on the new port, the mode's settings and
car/track databases are reloaded, and the UI re-reads capabilities on its
next refresh. The shared infrastructure - event bus, telemetry state,
logging - keeps running throughout, which is the whole point of keeping
version-specific facts in GameProfile rather than forking the app.
"""

from __future__ import annotations

import threading
from pathlib import Path

from app.config.mode_settings import ModeSettings
from app.config.settings import AppSettings
from app.core.events import Event, EventBus
from app.core.logging import get_logger, setup_logging
from app.core.models import TelemetryFrame
from app.core.telemetry_state import TelemetryState
from app.diagnostics.metrics import DiagnosticsCollector, DiagnosticsReport
from app.domain.car_profiles import create_car_store
from app.domain.driver_coach import DriverCoach
from app.domain.driver_session import DriverSession
from app.domain.lap_analysis import LapAnalysis, analyse_laps
from app.domain.profile_intelligence import ProfileContext, ProfileIntelligence
from app.domain.session_history import HistoryAnalysis, SessionCollector
from app.domain.stints import Stint, TyreState, build_stints, current_tyre_state
from app.domain.strategy import StrategyContext, StrategyEngine, StrategyPlan
from app.domain.race_intelligence import RaceIntelligence
from app.domain.smart_suggestions import (
    NOMINAL_HZ,
    SmartSuggestionEngine,
    SuggestionContext,
)
from app.domain.track_profiles import create_track_store
from app.games.base import GameAdapter
from app.games.modes import Capability, GameMode, GameProfile, game_profile
from app.games.registry import create_adapters
from app.telemetry.inspector import TelemetryInspector
from app.telemetry.recording import Recorder, RecordingMeta, recordings_dir
from app.telemetry.replay import ReplayPlayer

_log = get_logger(__name__)


class Application:
    def __init__(self, settings: AppSettings | None = None) -> None:
        self.settings = settings or AppSettings.load()
        setup_logging(verbose=self.settings.verbose_logging)

        self.bus = EventBus()

        # --- shared infrastructure, survives mode switches ---------------
        self.mode: GameMode = self.settings.mode
        self.mode_settings: ModeSettings = ModeSettings.load(self.mode).validate_against(
            game_profile(self.mode)
        )
        self.telemetry = TelemetryState(timeout=self.mode_settings.telemetry_timeout)

        self.adapters: dict[str, GameAdapter] = {
            adapter.game_id: adapter for adapter in create_adapters()
        }
        self.diagnostics = DiagnosticsCollector(self.telemetry)

        # --- validation tooling ------------------------------------------
        # The inspector observes; it never sits in the decode path.
        self.inspector = TelemetryInspector()
        self._recorder: Recorder | None = None
        self._replay: ReplayPlayer | None = None

        # Driver session data - collection only, no inference yet.
        self.session = DriverSession()
        #: Measured pace for the current session. Refreshed on lap
        #: completion; read freely by the UI between updates.
        self.lap_analysis = LapAnalysis()
        #: Stints for the current session, and the live tyre picture.
        #: Rebuilt with the lap analysis, on lap completion only.
        self.stints: list[Stint] = []
        self.tyres = TyreState()
        #: Smart Suggestions - the race-engineer layer over the analysis
        #: already computed. Evaluated on a controlled cadence, never per
        #: UDP packet.
        self.suggestions = SmartSuggestionEngine()
        #: Race Intelligence - the factual picture the suggestions read.
        #: Fed from the same frame callback as everything else; there is
        #: no second pipeline and none for replay.
        self.race = RaceIntelligence()
        #: Strategy. Evaluated on lap completion - a pit decision cannot
        #: change between frames, and the projection is not free.
        self.strategy = StrategyEngine()
        #: Driver Coach. Accumulates inputs per frame, analyses on lap
        #: completion - never per packet.
        self.coach = DriverCoach()
        #: Car & Track Intelligence - the context layer. Holds shipped
        #: profiles and what has been learned from real sessions, kept
        #: strictly apart.
        self.profiles = ProfileIntelligence(self.mode)
        #: Session history. Completed laps are written after every lap,
        #: so an unexpected loss costs one lap rather than a session.
        self.history = SessionCollector(self.mode)

        # --- version-specific, reloaded on mode switch -------------------
        self.cars = create_car_store(mode=self.mode)
        self.tracks = create_track_store(mode=self.mode)

        self._active_adapter: GameAdapter | None = None
        #: When False, exiting must not write settings back to disk. Set by
        #: CLI runs carrying --port/--mode overrides and by --selftest /
        #: --diagnose: a throwaway or diagnostic invocation must never
        #: silently become the user's permanent configuration.
        self.persist_on_exit = True
        self._started = False
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False

        self._select_adapter(self.settings.game_id)

    # ------------------------------------------------------------------
    # game mode
    # ------------------------------------------------------------------
    @property
    def game(self) -> GameProfile:
        """Version-specific configuration for the active mode."""
        return game_profile(self.mode)

    def supports(self, capability: Capability) -> bool:
        return self.game.supports(capability)

    def set_mode(self, mode: GameMode) -> None:
        """Switch game mode without a restart.

        Order matters: the current mode's settings are flushed *before*
        anything is swapped, so nothing it owns is lost.
        """
        if mode == self.mode:
            return

        _log.info("Switching game mode %s -> %s", self.mode.value, mode.value)

        was_running = bool(self._active_adapter and self._active_adapter.status().running)
        self.mode_settings.save()  # never lose the outgoing mode's config
        self.stop_telemetry()

        self.mode = mode
        self.settings.game_mode = mode.value
        self.settings.save()

        self.mode_settings = ModeSettings.load(mode).validate_against(game_profile(mode))
        self.telemetry.set_timeout(self.mode_settings.telemetry_timeout)
        self.telemetry.clear()
        self.reset_session()

        # Version-specific databases: the same team can be rated
        # differently between titles, so these are reloaded, not shared.
        self.cars = create_car_store(mode=mode)
        self.tracks = create_track_store(mode=mode)
        self.profiles.set_mode(mode)
        self.history.set_mode(mode)

        self._configure_adapter()
        if was_running and self.mode_settings.auto_start_telemetry:
            self.start_telemetry()

        self.bus.emit(Event.MODE_CHANGED, mode=mode)

    # ------------------------------------------------------------------
    # game adapters
    # ------------------------------------------------------------------
    @property
    def active_adapter(self) -> GameAdapter | None:
        return self._active_adapter

    def _select_adapter(self, game_id: str) -> None:
        adapter = self.adapters.get(game_id) or next(iter(self.adapters.values()), None)
        self._active_adapter = adapter
        self.diagnostics.set_adapter(adapter)
        if adapter is not None:
            adapter.set_frame_callback(self._on_telemetry_frame)
            if hasattr(adapter, "set_packet_observer"):
                adapter.set_packet_observer(self._on_raw_packet)
            self._configure_adapter()

    def _configure_adapter(self) -> None:
        adapter = self._active_adapter
        if adapter is None:
            return
        adapter.configure(
            port=self.mode_settings.udp_port,
            connection_timeout=self.mode_settings.connection_timeout,
            expected_formats=self.game.expected_formats,
        )

    def set_game(self, game_id: str) -> None:
        if self._active_adapter and self._active_adapter.game_id == game_id:
            return
        self.stop_telemetry()
        self.settings.game_id = game_id
        self.settings.save()
        self._select_adapter(game_id)
        if self._started and self.mode_settings.auto_start_telemetry:
            self.start_telemetry()

    def _on_telemetry_frame(self, frame: TelemetryFrame) -> None:
        self.telemetry.submit(frame)
        self.inspector.observe_frame(frame)
        # Cheap per-frame bookkeeping: position changes, pit phases,
        # neutralisation. Gap sampling happens per lap, below.
        self.race.observe_frame(frame, now=self._session_clock())
        self.coach.observe_frame(frame)
        self.profiles.observe_frame(frame)
        self.history.observe_frame(
            frame,
            car_id=self.mode_settings.selected_car,
            track_id=self.mode_settings.selected_track,
        )
        completed = self.session.observe(frame)
        # Lap analysis is recomputed when a lap completes, not per frame.
        # At 60 Hz the difference is ~60x, and nothing in it can change
        # mid-lap anyway.
        if completed is not None:
            laps = self.session.laps
            self.lap_analysis = analyse_laps(laps)
            self.stints = build_stints(laps)
            self.tyres = current_tyre_state(self.stints)
            # Gap samples are per lap, so this is the only place they can be
            # taken without oversampling a noisy per-frame figure.
            self.race.observe_lap(
                completed.lap_number, frame, now=self._session_clock()
            )
            self.coach.observe_lap(
                completed, self.lap_analysis, now=self._session_clock(),
                context=self.profile_context(),
            )
            self.strategy.evaluate(self.strategy_context(self._report_for_strategy()))
            # Learning is per lap and cheap; the profiles it writes are
            # what survive the session.
            self.profiles.select(
                self.mode_settings.selected_car, self.mode_settings.selected_track
            )
            self.profiles.learn(laps, self.stints)
            # History is written last, so it captures the analysis that
            # the other modules have just refreshed for this lap.
            self.history.observe_lap(completed)
            plan = self.strategy.plan
            self.history.update_context(
                self.stints,
                self.coach.problems,
                self.strategy.history,
                recommended=(
                    plan.recommended.summary() if plan.recommended else ""
                ),
            )
            self.bus.emit(Event.LAP_COMPLETED, lap=completed)

    def _on_raw_packet(self, data: bytes) -> None:
        """Every raw packet, live or replayed, before it is parsed."""
        self.inspector.observe_packet(data)
        recorder = self._recorder
        if recorder is not None and recorder.active:
            header = None
            try:
                from app.games.f1 import packets as f1_packets

                header = f1_packets.parse_header(data)
            except Exception:  # noqa: BLE001 - recording must not break
                header = None
            recorder.write(
                data,
                packet_format=header.packet_format if header else 0,
                packet_id=header.packet_id if header else -1,
            )

    # ------------------------------------------------------------------
    # recording
    # ------------------------------------------------------------------
    @property
    def recording(self) -> bool:
        return self._recorder is not None and self._recorder.active

    @property
    def recorder(self) -> Recorder | None:
        return self._recorder

    def start_recording(self, note: str = "") -> Path | None:
        """Begin capturing raw packets for later replay."""
        if self.recording:
            return self._recorder.path

        import time as _time

        stamp = _time.strftime("%Y%m%d-%H%M%S")
        path = recordings_dir() / f"{self.mode.value}-{stamp}.f1re"
        meta = RecordingMeta(
            game_mode=self.mode.value,
            game_label=self.game.display_name,
            note=note,
        )
        recorder = Recorder(path, meta)
        if not recorder.start():
            return None
        self._recorder = recorder
        return path

    def stop_recording(self) -> RecordingMeta | None:
        if self._recorder is None:
            return None
        meta = self._recorder.stop()
        self._recorder = None
        return meta

    # ------------------------------------------------------------------
    # replay
    # ------------------------------------------------------------------
    @property
    def replay(self) -> ReplayPlayer | None:
        return self._replay

    @property
    def replaying(self) -> bool:
        return self._replay is not None and self._replay.running

    def load_replay(self, path: Path) -> ReplayPlayer | None:
        """Load a recording, stopping live telemetry first.

        Live and replay never run together: two sources feeding one
        normalized state would make the result impossible to reason about.
        """
        self.stop_replay()
        self.stop_telemetry()

        adapter = self._active_adapter
        if adapter is None or not hasattr(adapter, "feed"):
            return None

        player = ReplayPlayer(Path(path), adapter.feed)
        if not player.load():
            return None

        self.inspector.reset()
        self.reset_session()
        self.telemetry.clear()
        self._replay = player
        return player

    def start_replay(self) -> bool:
        return bool(self._replay and self._replay.start())

    def stop_replay(self) -> None:
        if self._replay is not None:
            self._replay.stop()
            self._replay = None
        adapter = self._active_adapter
        if adapter is not None and hasattr(adapter, "end_replay"):
            adapter.end_replay()
        self.telemetry.clear()

    def _session_clock(self) -> float:
        """Seconds derived from frames observed, not from wall time.

        This is what makes replay deterministic: playback speed cannot
        change a cooldown or a state transition.
        """
        return self.telemetry.frames_received / NOMINAL_HZ

    def _report_for_strategy(self) -> DiagnosticsReport:
        return self.report()

    def profile_context(self) -> ProfileContext:
        """Car and track context, for strategy, coaching and suggestions.

        The profile data itself is not copied into those modules - they ask
        this for it, so there is one owner.
        """
        self.profiles.select(
            self.mode_settings.selected_car, self.mode_settings.selected_track
        )
        return self.profiles.context(
            self.cars.get(self.mode_settings.selected_car),
            self.tracks.get(self.mode_settings.selected_track),
        )

    def strategy_context(self, report: DiagnosticsReport) -> StrategyContext:
        """Assemble the strategy inputs from what is already measured."""
        return StrategyContext(
            frame=report.frame,
            race=self.race_state(report),
            tyres=self.tyres,
            stints=self.stints,
            game=self.game,
            car=self.cars.get(self.mode_settings.selected_car),
            track=self.tracks.get(self.mode_settings.selected_track),
            fuel_per_lap=self.session.summary().mean_fuel_per_lap,
            now=self._session_clock(),
            live=report.live,
            profiles=self.profile_context(),
        )

    def strategy_plan(self, report: DiagnosticsReport) -> StrategyPlan:
        """The current plan. Recomputed on lap completion, read freely."""
        if not report.live:
            return self.strategy.evaluate(self.strategy_context(report))
        return self.strategy.plan

    def race_state(self, report: DiagnosticsReport):
        """The current factual race picture."""
        return self.race.state(
            report.frame,
            self.lap_analysis,
            self.tyres,
            self.game,
            live=report.live,
            now=self._session_clock(),
        )

    def suggestion_context(self, report: DiagnosticsReport) -> SuggestionContext:
        """Assemble everything the rules may read.

        `now` comes from the telemetry stream - frames observed, not wall
        time - so a recording replays to identical suggestions whatever the
        playback speed.
        """
        return SuggestionContext(
            frame=report.frame,
            analysis=self.lap_analysis,
            tyres=self.tyres,
            stints=self.stints,
            game=self.game,
            car=self.cars.get(self.mode_settings.selected_car),
            track=self.tracks.get(self.mode_settings.selected_track),
            now=self._session_clock(),
            live=report.live,
            fuel_per_lap=self.session.summary().mean_fuel_per_lap,
            race=self.race_state(report),
            strategy=self.strategy.plan,
            coaching=self.coach.observations,
            profiles=self.profile_context(),
        )

    def reset_session(self) -> None:
        """Clear per-session driver data. A new session must never inherit
        the previous one's laps, or its pace and bests are meaningless."""
        self.session.reset()
        self.lap_analysis = LapAnalysis()
        self.stints = []
        self.tyres = TyreState()
        self.suggestions.reset()
        self.race.reset()
        self.strategy.reset()
        self.coach.reset()
        self.profiles.reset_session()
        self.history.finish()

    def start_telemetry(self) -> None:
        adapter = self._active_adapter
        if adapter is None or not adapter.supported:
            return
        self.stop_replay()  # live and replay are mutually exclusive
        self.inspector.reset()
        self.reset_session()
        adapter.start()

    def stop_telemetry(self) -> None:
        adapter = self._active_adapter
        if adapter is None:
            return
        adapter.stop()
        self.telemetry.clear()

    # ------------------------------------------------------------------
    # per-mode settings
    # ------------------------------------------------------------------
    def set_udp_port(self, port: int) -> None:
        self.mode_settings.udp_port = port
        self.mode_settings.save()
        self._configure_adapter()

    def set_telemetry_timeout(self, seconds: float) -> None:
        self.mode_settings.telemetry_timeout = seconds
        self.telemetry.set_timeout(seconds)
        self.mode_settings.save()

    def save_mode_settings(self) -> bool:
        return self.mode_settings.clamped().save()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @property
    def started(self) -> bool:
        return self._started

    def startup(self) -> None:
        if self._started:
            return
        _log.info("Starting F1 Race Engineer in %s mode", self.game.display_name)
        if self.mode_settings.auto_start_telemetry:
            self.start_telemetry()
        self._started = True

    def shutdown(self) -> None:
        """Stop everything. Safe to call twice."""
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True

        _log.info("Shutting down")
        try:
            # Save the session before anything else is torn down.
            self.history.finish()
        except Exception:  # noqa: BLE001
            _log.exception("Error saving the session on exit")
        try:
            self.stop_recording()
        except Exception:  # noqa: BLE001
            _log.exception("Error stopping recording")
        try:
            self.stop_replay()
        except Exception:  # noqa: BLE001
            _log.exception("Error stopping replay")
        try:
            self.stop_telemetry()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            _log.exception("Error stopping telemetry")

        if self.persist_on_exit:
            for label, save in (
                ("settings", self.settings.save),
                ("mode settings", self.mode_settings.save),
            ):
                try:
                    save()
                except Exception:  # noqa: BLE001
                    _log.exception("Could not save %s on exit", label)
        else:
            _log.info("Session overrides active - settings not written on exit")

        self._started = False
        _log.info("Shutdown complete")

    def report(self) -> DiagnosticsReport:
        report = self.diagnostics.collect()
        # Going stale changes the session STATE only. Nothing already
        # recorded is touched.
        self.history.tick(report.live)
        return report

    def session_history(self) -> HistoryAnalysis:
        """Stored sessions for this mode, plus the one in progress."""
        return self.history.history()

    def __enter__(self) -> "Application":
        self.startup()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()
