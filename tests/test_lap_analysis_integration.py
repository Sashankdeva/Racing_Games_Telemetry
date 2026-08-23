"""Phase A/B wiring: analysis into the Application, and onto the pages.

The unit tests cover the maths. These cover the things that only break once
it is plugged in: recomputation timing, session resets, mode isolation, and
the pages actually rendering what the analysis holds.
"""

from __future__ import annotations

import pytest

from app.config.settings import AppSettings
from app.core.application import Application
from app.core.events import Event
from app.core.models import TelemetryFrame
from app.domain.lap_analysis import Confidence
from app.games.modes import GameMode


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path))
    instance = Application(AppSettings(game_mode="f1_25"))
    instance.mode_settings.auto_start_telemetry = False
    instance.persist_on_exit = False
    yield instance
    instance.shutdown()


def frame(lap: int, last_lap_s: float = 0.0, s1: float = 0.0, s2: float = 0.0, **kw):
    return TelemetryFrame(
        valid=True, game="f1", current_lap=lap, last_lap_time_s=last_lap_s,
        sector1_time_s=s1, sector2_time_s=s2, **kw
    )


def drive(app, laps):
    """Feed frames so the session closes each lap, as the game would."""
    for index, (time_s, s1, s2) in enumerate(laps, start=1):
        app._on_telemetry_frame(frame(index, s1=s1, s2=s2))
        app._on_telemetry_frame(frame(index + 1, last_lap_s=time_s, s1=s1, s2=s2))


class TestApplicationWiring:
    def test_starts_with_no_analysis(self, app):
        assert app.lap_analysis.confidence is Confidence.NO_DATA
        assert not app.lap_analysis.has_pace

    def test_analysis_updates_when_a_lap_completes(self, app):
        drive(app, [(92.0, 30.0, 31.0)])
        assert app.lap_analysis.has_pace
        assert app.lap_analysis.best_lap_s == pytest.approx(92.0)

    def test_analysis_is_not_recomputed_mid_lap(self, app):
        """Recomputing per frame would be ~60x the work for no new answer."""
        drive(app, [(92.0, 30.0, 31.0)])
        before = app.lap_analysis

        for _ in range(20):
            app._on_telemetry_frame(frame(2, s1=30.0, s2=31.0))

        assert app.lap_analysis is before, "analysis rebuilt without a lap change"

    def test_lap_completed_event_is_emitted(self, app):
        seen = []
        app.bus.subscribe(Event.LAP_COMPLETED, lambda lap: seen.append(lap))

        drive(app, [(92.0, 30.0, 31.0), (91.5, 30.0, 31.0)])

        assert len(seen) == 2
        assert seen[0].lap_time_s == pytest.approx(92.0)

    def test_theoretical_best_reaches_the_application(self, app):
        drive(app, [(92.0, 30.5, 31.5), (91.8, 30.2, 31.8)])
        analysis = app.lap_analysis

        assert analysis.theoretical_available
        assert analysis.theoretical_best_s <= analysis.best_lap_s


class TestSessionReset:
    def test_reset_clears_the_analysis(self, app):
        drive(app, [(92.0, 30.0, 31.0)])
        assert app.lap_analysis.has_pace

        app.reset_session()

        assert not app.lap_analysis.has_pace
        assert app.lap_analysis.confidence is Confidence.NO_DATA
        assert app.session.laps == []

    def test_switching_mode_clears_the_analysis(self, app):
        """A different title is a different session; its pace is not ours."""
        drive(app, [(92.0, 30.0, 31.0)])
        assert app.lap_analysis.has_pace

        app.set_mode(GameMode.F1_26)

        assert not app.lap_analysis.has_pace
        assert app.session.laps == []

    def test_starting_telemetry_clears_the_analysis(self, app):
        drive(app, [(92.0, 30.0, 31.0)])
        app.mode_settings.udp_port = 20895
        app._configure_adapter()
        app.start_telemetry()
        try:
            assert not app.lap_analysis.has_pace
        finally:
            app.stop_telemetry()


class TestPagesRender:
    @pytest.fixture
    def window(self, app):
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication

        from app.ui.main_window import MainWindow

        qt = QApplication.instance() or QApplication([])
        app.startup()
        window = MainWindow(app)
        yield window, qt
        window._timer.stop()

    def _page(self, window, name):
        return [
            window.stack.widget(i)
            for i in range(window.stack.count())
            if type(window.stack.widget(i)).__name__ == name
        ][0]

    def test_new_pages_are_registered(self, window):
        win, _ = window
        names = {type(win.stack.widget(i)).__name__ for i in range(win.stack.count())}
        assert {"DashboardPage", "TelemetryPage", "LapAnalysisPage"} <= names

    def test_every_page_refreshes_without_telemetry(self, window):
        """A page that crashes on empty data is worse than a blank one."""
        win, qt = window
        report = win.app.report()
        for index in range(win.stack.count()):
            win.stack.widget(index).refresh(report)
        qt.processEvents()

    def test_lap_page_shows_measured_pace(self, window):
        win, qt = window
        drive(win.app, [(92.000, 30.500, 31.500), (91.800, 30.200, 31.800)])

        page = self._page(win, "LapAnalysisPage")
        page.refresh(win.app.report())
        qt.processEvents()

        assert page._best._value.text() == "1:31.800"
        assert page._table.rowCount() == 2
        assert page._confidence.text() == Confidence.LOW.value

    def test_lap_page_lists_invalid_laps_without_ranking_them(self, window):
        win, qt = window
        # `lap_invalid` describes the lap in progress, so a lap is marked
        # invalid on ITS OWN frames - not on the first frame of the next one.
        win.app._on_telemetry_frame(frame(1, s1=30.0, s2=31.0, lap_invalid=True))
        win.app._on_telemetry_frame(frame(2, last_lap_s=88.0, s1=30.0, s2=31.0))
        win.app._on_telemetry_frame(frame(2, s1=30.0, s2=31.0))
        win.app._on_telemetry_frame(frame(3, last_lap_s=92.0, s1=30.0, s2=31.0))

        page = self._page(win, "LapAnalysisPage")
        page.refresh(win.app.report())
        qt.processEvents()

        assert page._table.rowCount() == 2
        # The 88.0 lap is listed but never becomes the best.
        assert win.app.lap_analysis.best_lap_s == pytest.approx(92.0)

    def test_dashboard_pace_card_reflects_the_analysis(self, window):
        win, qt = window
        drive(win.app, [(92.000, 30.500, 31.500), (91.800, 30.200, 31.800)])

        page = self._page(win, "DashboardPage")
        page.refresh(win.app.report())
        qt.processEvents()

        assert page._theoretical._value.text() != "-"
        assert "valid lap" in page._pace_note.text()

    def test_dashboard_no_longer_carries_the_full_dataset(self, window):
        """Phase A: density moved to the Telemetry page."""
        win, _ = window
        dashboard = self._page(win, "DashboardPage")
        assert not hasattr(dashboard, "_conditions_label")
        assert not hasattr(dashboard, "_damage_label")

    def test_telemetry_page_covers_the_frame(self, window):
        win, _ = window
        page = self._page(win, "TelemetryPage")
        for key in ("speed", "rpm", "tyre_wear", "ers_store", "weather", "damage"):
            assert key in page._rows

    def test_telemetry_page_uses_mode_terminology(self, window):
        win, qt = window
        page = self._page(win, "TelemetryPage")
        page._show(frame(1, drs_active=True))
        qt.processEvents()
        assert "DRS" in page._rows["drs"]._value.text()

        win.sidebar._mode_combo.setCurrentIndex(1)
        qt.processEvents()
        page._show(frame(1, drs_active=True))
        assert "Manual Override" in page._rows["drs"]._value.text()
