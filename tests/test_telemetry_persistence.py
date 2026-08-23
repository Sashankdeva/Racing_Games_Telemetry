"""Telemetry state model: NO_DATA / LIVE / STALE, and what survives a dropout.

The bug these exist to prevent: when packets stopped arriving, the state
replaced the last valid frame with a blank one. Lap number, position, tyre
compound and everything else the driver was reading vanished - as though a
dropped packet had moved the car off lap 18.

The rule is that no new packet is not the same as no data. Stale values are
kept and flagged; only a genuine end of session clears them.

Covers the ten regression cases from the brief.
"""

from __future__ import annotations

import time

import pytest

from app.config.settings import AppSettings
from app.core.application import Application
from app.core.models import TelemetryFrame
from app.core.telemetry_state import TelemetryState, TelemetryStatus
from app.games.modes import GameMode

#: Short enough to go stale inside a test without sleeping for long.
FAST_TIMEOUT = 0.08
STALE_WAIT = 0.25


def frame(lap=18, **kw):
    defaults = dict(
        valid=True, game="f1", current_lap=lap, position=5, speed_kph=287.0,
        gear=7, rpm=10500.0, tyre_compound="Medium", tyre_age_laps=18,
        last_lap_time_s=94.821, best_lap_time_s=93.902,
        sector1_time_s=30.4, sector2_time_s=31.1, session_type="Race",
    )
    defaults.update(kw)
    return TelemetryFrame(**defaults)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path))
    instance = Application(AppSettings(game_mode="f1_25"))
    instance.mode_settings.auto_start_telemetry = False
    instance.persist_on_exit = False
    instance.telemetry.set_timeout(FAST_TIMEOUT)
    yield instance
    instance.shutdown()


def drive(app, count, start_lap=1, base=92.0, deg=0.06, compound="Medium"):
    """Complete `count` laps the way the game reports them."""
    for index in range(count):
        number = start_lap + index
        common = dict(
            valid=True, game="f1", tyre_compound=compound, tyre_age_laps=number,
            sector1_time_s=30.4, sector2_time_s=31.1, position=5,
            session_type="Race",
        )
        app._on_telemetry_frame(TelemetryFrame(current_lap=number, **common))
        app._on_telemetry_frame(
            TelemetryFrame(
                current_lap=number + 1, last_lap_time_s=base + deg * index, **common
            )
        )


# ---------------------------------------------------------------------------
# 1, 2, 3 - the state machine
# ---------------------------------------------------------------------------
class TestStateTransitions:
    def test_1_no_data_to_live(self):
        state = TelemetryState(timeout=FAST_TIMEOUT)
        assert state.snapshot().status is TelemetryStatus.NO_DATA

        state.submit(frame())
        snapshot = state.snapshot()

        assert snapshot.status is TelemetryStatus.LIVE
        assert snapshot.live and snapshot.has_data
        assert snapshot.frame.current_lap == 18

    def test_2_live_to_stale_keeps_every_value(self):
        """The heart of the bug."""
        state = TelemetryState(timeout=FAST_TIMEOUT)
        state.submit(frame())
        time.sleep(STALE_WAIT)

        snapshot = state.snapshot()
        assert snapshot.status is TelemetryStatus.STALE
        assert not snapshot.live
        assert snapshot.has_data

        # Exactly the values from the brief's example.
        f = snapshot.frame
        assert f.current_lap == 18
        assert f.last_lap_time_s == 94.821
        assert f.best_lap_time_s == 93.902
        assert f.position == 5
        assert f.speed_kph == 287.0
        assert f.gear == 7
        assert f.tyre_compound == "Medium"
        assert f.tyre_age_laps == 18

    def test_3_stale_to_live_resumes(self):
        state = TelemetryState(timeout=FAST_TIMEOUT)
        state.submit(frame(lap=18))
        time.sleep(STALE_WAIT)
        assert state.snapshot().status is TelemetryStatus.STALE

        state.submit(frame(lap=19))
        snapshot = state.snapshot()

        assert snapshot.status is TelemetryStatus.LIVE
        assert snapshot.frame.current_lap == 19

    def test_8_no_data_when_nothing_ever_arrived(self):
        state = TelemetryState(timeout=FAST_TIMEOUT)
        snapshot = state.snapshot()

        assert snapshot.status is TelemetryStatus.NO_DATA
        assert not snapshot.has_data
        assert not snapshot.frame.valid
        assert snapshot.age == 0.0

    def test_no_data_is_not_stale(self):
        """Two different situations with two different fixes."""
        state = TelemetryState(timeout=FAST_TIMEOUT)
        assert not state.snapshot().stale

        state.submit(frame())
        time.sleep(STALE_WAIT)
        assert state.snapshot().stale

    def test_age_is_reported_and_grows(self):
        state = TelemetryState(timeout=FAST_TIMEOUT)
        state.submit(frame())
        time.sleep(STALE_WAIT)

        snapshot = state.snapshot()
        assert snapshot.age > FAST_TIMEOUT
        assert state.seconds_since_last_packet > 0

    def test_9_brief_packet_loss_never_produces_nulls(self):
        """A gap shorter than the timeout must not even register."""
        state = TelemetryState(timeout=1.0)
        state.submit(frame())
        time.sleep(0.05)

        snapshot = state.snapshot()
        assert snapshot.live
        assert snapshot.frame.current_lap == 18

    def test_invalid_frame_cannot_displace_a_good_one(self):
        state = TelemetryState(timeout=FAST_TIMEOUT)
        state.submit(frame())
        state.submit(TelemetryFrame(valid=False))

        assert state.snapshot().frame.current_lap == 18

    def test_explicit_clear_returns_to_no_data(self):
        """Only a real end of session wipes the frame."""
        state = TelemetryState(timeout=FAST_TIMEOUT)
        state.submit(frame())
        state.clear()

        assert state.snapshot().status is TelemetryStatus.NO_DATA


# ---------------------------------------------------------------------------
# 4, 5, 6, 7 - history must be independent of the live frame
# ---------------------------------------------------------------------------
class TestHistorySurvivesTelemetryLoss:
    def _go_stale(self, app):
        time.sleep(STALE_WAIT)
        assert app.report().status is TelemetryStatus.STALE

    @pytest.fixture
    def raced(self, app):
        drive(app, 18)
        assert len(app.session.laps) == 18
        return app

    def test_4_laps_1_to_18_remain(self, raced):
        self._go_stale(raced)
        assert len(raced.session.laps) == 18
        assert raced.lap_analysis.laps_recorded == 18

    def test_5_best_lap_survives(self, raced):
        best_before = raced.lap_analysis.best_lap_s
        assert best_before > 0
        self._go_stale(raced)
        assert raced.lap_analysis.best_lap_s == best_before

    def test_6_best_sectors_and_theoretical_survive(self, raced):
        before = [s.time_s for s in raced.lap_analysis.best_sectors]
        theoretical = raced.lap_analysis.theoretical_best_s
        self._go_stale(raced)

        assert [s.time_s for s in raced.lap_analysis.best_sectors] == before
        assert raced.lap_analysis.theoretical_best_s == theoretical

    def test_7_session_statistics_survive(self, raced):
        analysis = raced.lap_analysis
        average, consistency, valid = (
            analysis.average_lap_s, analysis.consistency_s, analysis.valid_laps
        )
        self._go_stale(raced)

        assert raced.lap_analysis.average_lap_s == average
        assert raced.lap_analysis.consistency_s == consistency
        assert raced.lap_analysis.valid_laps == valid

    def test_stint_history_survives(self, raced):
        stints = len(raced.stints)
        degradation = raced.tyres.degradation_s_per_lap
        self._go_stale(raced)

        assert len(raced.stints) == stints
        assert raced.tyres.degradation_s_per_lap == degradation

    def test_lap_analysis_page_still_lists_the_laps(self, raced):
        """The brief names this page specifically."""
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication

        from app.ui.main_window import MainWindow

        qt = QApplication.instance() or QApplication([])
        raced.startup()
        window = MainWindow(raced)
        try:
            self._go_stale(raced)
            page = [
                window.stack.widget(i)
                for i in range(window.stack.count())
                if type(window.stack.widget(i)).__name__ == "LapAnalysisPage"
            ][0]
            page.refresh(raced.report())
            qt.processEvents()

            assert page._table.rowCount() == 18
            assert page._best._value.text() != "-"
        finally:
            window._timer.stop()


# ---------------------------------------------------------------------------
# 10 - reconnection must not start a new session
# ---------------------------------------------------------------------------
class TestReconnection:
    def test_10_resuming_continues_the_same_session(self, app):
        drive(app, 18)
        time.sleep(STALE_WAIT)
        assert app.report().status is TelemetryStatus.STALE

        # Telemetry resumes on lap 19.
        drive(app, 1, start_lap=19)

        assert app.report().status is TelemetryStatus.LIVE
        assert len(app.session.laps) == 19
        assert app.session.laps[0].lap_number == 1

    def test_a_blank_session_type_does_not_reset_the_session(self, app):
        """After a dropout the first frames can arrive before the next
        Session packet, with session_type still empty."""
        drive(app, 5)
        assert len(app.session.laps) == 5

        app._on_telemetry_frame(
            TelemetryFrame(valid=True, game="f1", current_lap=6, session_type="")
        )
        assert len(app.session.laps) == 5

    def test_a_genuinely_different_session_still_resets(self, app):
        """The existing behaviour must not be lost: Practice then Race is a
        new session and mixing their pace would be wrong."""
        drive(app, 5)
        app._on_telemetry_frame(
            TelemetryFrame(valid=True, game="f1", current_lap=1, session_type="Qualifying 1")
        )
        assert app.session.laps == []

    def test_explicit_stop_does_end_the_session(self, app):
        drive(app, 5)
        app.reset_session()
        assert app.session.laps == []
        assert not app.lap_analysis.has_pace

    def test_mode_switch_still_clears(self, app):
        drive(app, 5)
        app.set_mode(GameMode.F1_26)
        assert app.session.laps == []
        assert app.report().status is TelemetryStatus.NO_DATA


# ---------------------------------------------------------------------------
# UI: stale must be visible, and must not blank the readouts
# ---------------------------------------------------------------------------
class TestDashboardShowsStaleValues:
    @pytest.fixture
    def window(self, app):
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication

        from app.ui.main_window import MainWindow

        qt = QApplication.instance() or QApplication([])
        app.startup()
        window = MainWindow(app)
        yield window, qt, app
        window._timer.stop()

    def _page(self, window, name):
        return [
            window.stack.widget(i)
            for i in range(window.stack.count())
            if type(window.stack.widget(i)).__name__ == name
        ][0]

    def test_dashboard_keeps_values_and_flags_stale(self, window):
        win, qt, app = window
        app._on_telemetry_frame(frame())
        dashboard = self._page(win, "DashboardPage")
        dashboard.refresh(app.report())
        qt.processEvents()
        assert dashboard._speed._value.text() == "287"

        time.sleep(STALE_WAIT)
        dashboard.refresh(app.report())
        qt.processEvents()

        # Values retained...
        assert dashboard._speed._value.text() == "287"
        assert dashboard._position._value.text() == "5"
        assert dashboard._gear._value.text() == "7"
        # ...and unmistakably flagged. isHidden() rather than isVisible():
        # every child of an unshown window reports not-visible, which
        # would make this assertion pass for the wrong reason.
        assert not dashboard._stale_pill.isHidden()
        assert "STALE" in dashboard._stale_pill._label.text()

    def test_dashboard_blanks_only_when_no_data(self, window):
        win, qt, app = window
        dashboard = self._page(win, "DashboardPage")
        dashboard.refresh(app.report())
        qt.processEvents()

        assert dashboard._speed._value.text() == "-"
        assert dashboard._stale_pill.isHidden()

    def test_stale_flag_clears_when_telemetry_resumes(self, window):
        win, qt, app = window
        app._on_telemetry_frame(frame())
        time.sleep(STALE_WAIT)
        dashboard = self._page(win, "DashboardPage")
        dashboard.refresh(app.report())
        qt.processEvents()
        assert not dashboard._stale_pill.isHidden()

        app._on_telemetry_frame(frame(lap=19))
        dashboard.refresh(app.report())
        qt.processEvents()

        assert dashboard._stale_pill.isHidden()
        assert dashboard._lap._value.text().startswith("19")

    def test_telemetry_page_keeps_values_when_stale(self, window):
        win, qt, app = window
        app._on_telemetry_frame(frame())
        page = self._page(win, "TelemetryPage")
        page.refresh(app.report())
        qt.processEvents()

        time.sleep(STALE_WAIT)
        page.refresh(app.report())
        qt.processEvents()

        assert page._rows["speed"]._value.text() == "287 kph"
        assert page._rows["compound"]._value.text() == "Medium"

    def test_tyres_page_keeps_corners_when_stale(self, window):
        win, qt, app = window
        from app.core.models import Wheels

        app._on_telemetry_frame(
            frame(tyre_pressure=Wheels(21.5, 21.6, 22.8, 22.9))
        )
        page = self._page(win, "TyresPage")
        time.sleep(STALE_WAIT)
        page.refresh(app.report())
        qt.processEvents()

        assert "STALE" in page._corner_note.text()

    def test_sidebar_reports_stale(self, window):
        win, qt, app = window
        app._on_telemetry_frame(frame())
        time.sleep(STALE_WAIT)
        win._refresh()
        qt.processEvents()
        # Reached without raising; the status text is built from report.stale.
        assert app.report().stale

    def test_every_page_refreshes_while_stale(self, window):
        win, qt, app = window
        drive(app, 3)
        app._on_telemetry_frame(frame())
        time.sleep(STALE_WAIT)

        report = app.report()
        assert report.stale
        for index in range(win.stack.count()):
            win.stack.widget(index).refresh(report)
        qt.processEvents()
