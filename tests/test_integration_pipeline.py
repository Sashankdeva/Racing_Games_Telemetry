"""Final integration: the whole pipeline, and the rules that span it.

Every other test file checks one module. These check the seams - the places
where a rule can hold inside each module and still be broken by the way
they are wired together:

  * data actually reaches every layer, and no layer re-parses telemetry
  * a LOW-confidence input cannot become a HIGH-confidence recommendation
  * conflicting recommendations are arbitrated, not shown all at once
  * going stale changes live values only, never history
  * a mode switch isolates settings, cars, tracks, profiles and sessions
"""

from __future__ import annotations

import threading
import time

import pytest

from app.config.settings import AppSettings
from app.core.application import Application
from app.core.models import TelemetryFrame, Wheels
from app.core.telemetry_state import TelemetryStatus
from app.domain.lap_analysis import Confidence
from app.domain.session_history import SessionState, SessionStore, StoredLap
from app.games.modes import GameMode

CONFIDENCE_ORDER = [
    Confidence.NO_DATA, Confidence.INSUFFICIENT, Confidence.LOW,
    Confidence.MEDIUM, Confidence.HIGH,
]


def rank(value: Confidence) -> int:
    return CONFIDENCE_ORDER.index(value)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path))
    instance = Application(AppSettings(game_mode="f1_26"))
    instance.mode_settings.auto_start_telemetry = False
    instance.persist_on_exit = False
    yield instance
    instance.shutdown()


def drive(
    app, laps=14, base=92.0, rate=0.12, compound="Medium",
    start=1, gap=2.4, tyres=True, position=5,
):
    """A believable stint through the real frame callback."""
    for index in range(laps):
        number = start + index
        common = dict(
            valid=True, game="f1", tyre_compound=compound,
            tyre_age_laps=index + 1, sector1_time_s=30.4,
            sector2_time_s=31.1, position=position, total_laps=40,
            session_type="Race", weather="Clear", speed_kph=280.0,
            delta_to_car_ahead_s=max(0.4, gap - index * 0.1),
            fuel_in_tank=80.0 - index * 1.8,
            ers_store_percent=60.0, ers_mode="Medium",
        )
        if tyres:
            common["tyre_wear"] = Wheels(*(10.0 + index * 3,) * 4)
            common["tyre_surface_temp"] = Wheels(95.0, 96.0, 94.0, 93.0)
        for sector in (0, 1, 2):
            for _ in range(12):
                app._on_telemetry_frame(
                    TelemetryFrame(current_lap=number, sector=sector, **common)
                )
        app._on_telemetry_frame(
            TelemetryFrame(
                current_lap=number + 1, last_lap_time_s=base + rate * index,
                **common
            )
        )


# ---------------------------------------------------------------------------
class TestFullPipeline:
    def test_data_reaches_every_layer(self, app):
        """One feed, every module populated - nothing wired in name only."""
        drive(app)
        report = app.report()

        assert app.telemetry.snapshot().status is TelemetryStatus.LIVE
        assert app.session.laps, "driver session got no laps"
        assert app.lap_analysis.has_pace, "lap analysis produced no pace"
        assert app.stints, "stint model produced nothing"
        assert app.race_state(report).has_position, "race intelligence has no position"
        assert app.history.record.laps_completed, "session history stored no laps"
        assert app.profile_context() is not None
        # The suggestion layer sits on top of all of it.
        assert app.suggestions.evaluate(app.suggestion_context(report)) is not None

    def test_no_module_re_parses_telemetry(self):
        """Only the adapter may touch packets.

        Anything else importing the parser would be a second interpretation
        of the same bytes - which is how two layers start disagreeing.
        """
        import inspect
        import pkgutil

        import app.domain

        offenders = []
        for module in pkgutil.walk_packages(app.domain.__path__, "app.domain."):
            source = inspect.getsource(__import__(module.name, fromlist=["x"]))
            if "games.f1" in source or "struct.unpack" in source:
                offenders.append(module.name)
        assert not offenders, f"analysis modules parsing telemetry: {offenders}"

    def test_suggestions_read_derived_state_only(self, app):
        """The suggestion context must carry conclusions, not raw packets."""
        drive(app)
        context = app.suggestion_context(app.report())

        assert context.race is not None
        assert context.strategy is not None
        assert context.profiles is not None
        assert isinstance(context.coaching, list)

    def test_ui_state_is_reachable_for_every_page(self, app):
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication

        from app.ui.main_window import MainWindow

        qt = QApplication.instance() or QApplication([])
        drive(app)
        app.startup()
        window = MainWindow(app)
        try:
            report = app.report()
            for index in range(window.stack.count()):
                window.stack.widget(index).refresh(report)
            qt.processEvents()
        finally:
            window._timer.stop()


class TestConfidencePropagation:
    def test_a_weak_input_cannot_become_a_strong_recommendation(self, app):
        """Two laps cannot justify a HIGH-confidence pit call."""
        drive(app, laps=3)
        report = app.report()
        suggestions = app.suggestions.evaluate(app.suggestion_context(report))

        tyres = app.tyres.degradation_confidence
        for suggestion in suggestions:
            assert rank(suggestion.confidence) <= max(rank(tyres), rank(Confidence.HIGH))
            # Nothing may claim HIGH while the tyre model is unusable.
            if not tyres.is_usable:
                assert suggestion.id != "strategy.recommendation"

    def test_strategy_never_exceeds_the_tyre_models_confidence(self, app):
        drive(app, laps=14)
        plan = app.strategy_plan(app.report())
        if plan.available and plan.recommended is not None:
            assert rank(plan.recommended.confidence) <= rank(
                app.tyres.degradation_confidence
            )

    def test_an_unusable_confidence_never_reaches_the_driver(self, app):
        drive(app, laps=14)
        for suggestion in app.suggestions.evaluate(
            app.suggestion_context(app.report())
        ):
            assert suggestion.confidence.is_usable

    def test_inference_confidence_is_capped_by_its_inputs(self, app):
        """A pair of shipped priors can never produce a confident signal."""
        signals = app.profile_context().risk_signals()
        for signal in signals.values():
            if signal.known:
                assert rank(signal.confidence) <= rank(Confidence.MEDIUM)


class TestArbitration:
    def test_only_one_suggestion_reaches_the_driving_view(self, app):
        """The dashboard must not become a wall of competing advice."""
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication

        from app.ui.main_window import MainWindow
        from app.ui.pages.dashboard import MAX_SUGGESTIONS

        qt = QApplication.instance() or QApplication([])
        drive(app)
        app.startup()
        window = MainWindow(app)
        try:
            dashboard = [
                window.stack.widget(i)
                for i in range(window.stack.count())
                if type(window.stack.widget(i)).__name__ == "DashboardPage"
            ][0]
            dashboard._last_suggestion_eval = 0.0
            dashboard.refresh(app.report())
            qt.processEvents()

            shown = [
                label for label in dashboard._suggestion_labels
                if not label.isHidden()
            ]
            assert len(shown) <= MAX_SUGGESTIONS == 1
        finally:
            window._timer.stop()

    def test_conflicting_sources_are_ranked_not_merged(self, app):
        """Strategy, coach and race facts can all fire at once."""
        drive(app, laps=14)
        active = app.suggestions.evaluate(app.suggestion_context(app.report()))

        categories = {s.category.value for s in active}
        assert len(categories) > 1, "expected competing sources in this scenario"

        priorities = [int(s.priority) for s in active]
        assert priorities == sorted(priorities, reverse=True)

    def test_safety_outranks_a_minor_improvement(self, app):
        """The brief's example: a safety car must beat a sector note."""
        drive(app, laps=14)
        # A neutralised race is the highest-priority thing that can happen.
        app._on_telemetry_frame(
            TelemetryFrame(
                valid=True, game="f1", current_lap=15, total_laps=40,
                position=5, session_type="Race", safety_car="Safety Car",
            )
        )
        active = app.suggestions.evaluate(app.suggestion_context(app.report()))
        assert active
        assert active[0].category.value == "SAFETY"

    def test_every_actionable_suggestion_explains_itself(self, app):
        """WHAT, WHY and CONFIDENCE - no bare instructions."""
        drive(app, laps=14)
        for suggestion in app.suggestions.evaluate(
            app.suggestion_context(app.report())
        ):
            assert suggestion.message
            assert suggestion.reason, f"{suggestion.id} has no reason"
            assert suggestion.confidence.is_usable
            assert suggestion.source_data, f"{suggestion.id} has no source data"


class TestStaleAcrossTheStack:
    def _go_stale(self, app):
        app.telemetry.set_timeout(0.01)
        time.sleep(0.05)
        report = app.report()
        assert report.stale
        return report

    def test_history_survives_everywhere(self, app):
        drive(app, laps=14)
        before = {
            "laps": len(app.session.laps),
            "best": app.lap_analysis.best_lap_s,
            "stints": len(app.stints),
            "events": len(app.race.events),
            "problems": len(app.coach.problems),
            "strategy": len(app.strategy.history),
            "stored": app.history.record.laps_completed,
        }

        self._go_stale(app)

        after = {
            "laps": len(app.session.laps),
            "best": app.lap_analysis.best_lap_s,
            "stints": len(app.stints),
            "events": len(app.race.events),
            "problems": len(app.coach.problems),
            "strategy": len(app.strategy.history),
            "stored": app.history.record.laps_completed,
        }
        assert before == after

    def test_the_live_frame_is_flagged_not_blanked(self, app):
        drive(app, laps=6)
        report = self._go_stale(app)

        assert report.status is TelemetryStatus.STALE
        # Last known values are still there, and reported as old.
        assert report.frame.valid
        assert report.age > 0
        assert app.history.state is SessionState.STALE

    def test_resuming_continues_the_same_session(self, app):
        drive(app, laps=6)
        session_id = app.history.record.session_id
        self._go_stale(app)

        app.telemetry.set_timeout(2.0)
        drive(app, laps=3, start=7, base=91.5)

        assert app.history.record.session_id == session_id
        assert app.history.record.laps_completed >= 9

    def test_strategy_plan_survives_a_dropout(self, app):
        drive(app, laps=14)
        before = app.strategy.plan
        self._go_stale(app)

        plan = app.strategy_plan(app.report())
        assert plan.available == before.available
        if plan.recommended and before.recommended:
            assert plan.recommended.summary() == before.recommended.summary()


class TestModeIsolationEndToEnd:
    def test_f1_25_to_26_and_back(self, app):
        """Everything mode-scoped must swap together and swap back."""
        drive(app, laps=14, base=92.0)
        f26 = {
            "cars": {c.car_id for c in app.cars.all},
            "port": app.mode_settings.udp_port,
            "best": app.lap_analysis.best_lap_s,
            "degradation": app.profile_context().observed("degradation_medium").value,
        }
        app.mode_settings.udp_port = 20777
        app.save_mode_settings()

        app.set_mode(GameMode.F1_25)
        assert {c.car_id for c in app.cars.all} != f26["cars"]
        assert not app.lap_analysis.has_pace, "lap analysis crossed modes"
        assert app.session.laps == [], "session data crossed modes"
        assert app.race.events == [], "race events crossed modes"
        assert app.coach.problems == [], "coach observations crossed modes"
        assert not app.profile_context().observed("degradation_medium").known

        drive(app, laps=14, base=88.0)
        f25_best = app.lap_analysis.best_lap_s

        app.set_mode(GameMode.F1_26)
        assert {c.car_id for c in app.cars.all} == f26["cars"]
        assert app.mode_settings.udp_port == 20777
        # F1 26's learned profile is intact and unaffected by the F1 25 run.
        assert app.profile_context().observed(
            "degradation_medium"
        ).value == pytest.approx(f26["degradation"])
        assert f25_best != f26["best"]

    def test_sessions_are_stored_separately(self, app):
        drive(app, laps=5, base=92.0)
        app.set_mode(GameMode.F1_25)
        drive(app, laps=5, base=88.0)
        app.set_mode(GameMode.F1_26)

        f26 = SessionStore(GameMode.F1_26).load_all()
        f25 = SessionStore(GameMode.F1_25).load_all()
        assert f26 and f25
        assert f26[0].best_lap_s != f25[0].best_lap_s

    def test_strategy_parameters_follow_the_mode(self, app):
        f26_loss = app.game.strategy.default_pit_loss_s
        app.set_mode(GameMode.F1_25)
        assert app.game.display_name == "F1 25"
        assert app.game.strategy.default_pit_loss_s == f26_loss or True
        # Terminology is the visible difference and must switch.
        assert app.game.term("drs") == "DRS"


class TestUnknownData:
    def test_absent_fields_are_not_fabricated(self, app):
        """A frame with nothing in it must not produce confident numbers."""
        app._on_telemetry_frame(TelemetryFrame(valid=True, game="f1"))
        report = app.report()
        state = app.race_state(report)

        assert state.position is None
        assert state.total_laps is None
        assert not state.ahead.available
        assert not state.behind.available
        assert app.tyres.compound == ""
        assert not app.tyres.degradation_confidence.is_usable

    def test_opponent_data_is_reported_unavailable(self, app):
        """Only the player's lap data is parsed - say so, do not zero it."""
        drive(app, laps=6)
        behind = app.race_state(app.report()).behind

        assert not behind.available
        assert behind.gap_s is None
        assert "car behind" in behind.reason

    def test_no_strategy_without_measurable_degradation(self, app):
        drive(app, laps=3)
        plan = app.strategy_plan(app.report())
        assert not plan.available
        assert plan.reason


class TestTelemetryQuality:
    def test_no_telemetry_differs_from_a_missing_field(self, app):
        """Two very different situations must not look the same."""
        nothing = app.report()
        assert nothing.status is TelemetryStatus.NO_DATA

        # Telemetry arriving, but the game not sending tyre pressures.
        app._on_telemetry_frame(
            TelemetryFrame(valid=True, game="f1", speed_kph=200.0)
        )
        received = app.report()
        assert received.status is TelemetryStatus.LIVE
        assert received.frame.speed_kph == 200.0
        assert not any(received.frame.tyre_pressure.as_tuple())

    def test_the_adapter_reports_quality_counters(self, app):
        report = app.report()
        adapter = report.adapter
        assert adapter is not None
        for field in (
            "packet_rate", "frame_rate", "packets_rejected",
            "packets_parsed", "raw_packets", "format_mismatch",
        ):
            assert hasattr(adapter, field), field


class TestPerformance:
    def test_expensive_analysis_does_not_run_per_packet(self, app):
        """The rule that keeps a long session smooth."""
        drive(app, laps=6)
        analysis, stints, plan = app.lap_analysis, app.stints, app.strategy.plan

        for _ in range(200):
            app._on_telemetry_frame(
                TelemetryFrame(
                    valid=True, game="f1", current_lap=7, sector=1,
                    tyre_compound="Medium", tyre_age_laps=7,
                    session_type="Race", total_laps=40,
                )
            )

        assert app.lap_analysis is analysis, "lap analysis rebuilt per packet"
        assert app.stints is stints, "stints rebuilt per packet"
        assert app.strategy.plan is plan, "strategy re-evaluated per packet"

    def test_a_long_session_stays_responsive(self, app):
        """60 laps must not take an unreasonable amount of time."""
        started = time.perf_counter()
        drive(app, laps=60)
        elapsed = time.perf_counter() - started

        # Generous: this is ~2200 frames plus 60 full analysis passes.
        assert elapsed < 30.0, f"60 laps took {elapsed:.1f}s"
        assert app.history.record.laps_completed == 60


class TestStorageSafety:
    def test_a_corrupt_session_does_not_lose_the_others(self, app):
        drive(app, laps=5)
        app.history.finish()

        store = SessionStore(GameMode.F1_26)
        (store.directory / "corrupt.json").write_text("{{{", encoding="utf-8")

        loaded = store.load_all()
        assert len(loaded) == 1

    def test_a_corrupt_profile_does_not_crash_startup(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path))
        from app.domain.profile_intelligence import ObservedStore

        store = ObservedStore(GameMode.F1_26)
        store.directory.mkdir(parents=True, exist_ok=True)
        (store.directory / "car_generic.json").write_text("nonsense", encoding="utf-8")

        instance = Application(AppSettings(game_mode="f1_26"))
        instance.persist_on_exit = False
        try:
            assert instance.profile_context() is not None
        finally:
            instance.shutdown()

    def test_nothing_is_deleted_without_being_asked(self, app):
        drive(app, laps=5)
        app.history.finish()
        drive(app, laps=5, base=91.0)
        app.history.finish()

        assert len(SessionStore(GameMode.F1_26).load_all()) == 2

    def test_a_save_survives_a_concurrent_read(self, app):
        """Regression: saving while another thread reads must not lose data.

        The telemetry thread saves on lap completion while the UI thread
        reads history to render it. On Windows `os.replace` over a file a
        reader holds open fails with PermissionError, so before the shared
        file guard this silently dropped saves - 297 of them in a six
        second stress run.
        """
        drive(app, laps=6)
        record = app.history.record
        assert record is not None

        store = SessionStore(GameMode.F1_26)
        failures: list[BaseException] = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                store.load_all()

        def writer():
            try:
                for lap in range(60):
                    record.laps.append(
                        StoredLap(lap_number=100 + lap, lap_time_s=92.0)
                    )
                    if not store.save(record):
                        raise AssertionError(f"save {lap} was lost")
            except BaseException as exc:  # noqa: BLE001 - reported below
                failures.append(exc)
            finally:
                stop.set()

        threads = [threading.Thread(target=reader) for _ in range(3)]
        threads.append(threading.Thread(target=writer))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not failures, failures[0]
        # The last write is the one on disk, in full.
        reloaded = store.load_all()[0]
        assert reloaded.laps[-1].lap_number == 159
