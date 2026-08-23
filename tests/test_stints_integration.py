"""Phase 2 wiring: stints into the Application and onto the Tyres page.

Also covers the rule the brief is strictest about - F1 25 and F1 26 must
not share session state - and the performance rule that analysis runs on
lap completion rather than per packet.
"""

from __future__ import annotations

import pytest

from app.config.settings import AppSettings
from app.core.application import Application
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


def drive(app, count, compound="Medium", start_lap=1, start_age=1, base=92.0, deg=0.06):
    """Feed frames so the game closes each lap, as it would live."""
    for index in range(count):
        number = start_lap + index
        age = start_age + index
        common = dict(
            valid=True, game="f1", tyre_compound=compound, tyre_age_laps=age,
            sector1_time_s=30.0, sector2_time_s=31.0,
        )
        app._on_telemetry_frame(TelemetryFrame(current_lap=number, **common))
        app._on_telemetry_frame(
            TelemetryFrame(
                current_lap=number + 1, last_lap_time_s=base + deg * index, **common
            )
        )


class TestApplicationWiring:
    def test_starts_with_no_stints(self, app):
        assert app.stints == []
        assert not app.tyres.available

    def test_stints_build_as_laps_complete(self, app):
        drive(app, 6)
        assert len(app.stints) == 1
        assert app.stints[0].compound == "Medium"
        assert app.tyres.compound == "Medium"

    def test_a_tyre_change_starts_a_new_stint(self, app):
        drive(app, 8, compound="Medium")
        drive(app, 8, compound="Hard", start_lap=9, start_age=1)

        assert len(app.stints) == 2
        assert app.tyres.stint_number == 2
        assert app.tyres.compound == "Hard"

    def test_degradation_reaches_the_application(self, app):
        drive(app, 12, deg=0.061)
        assert app.tyres.degradation_confidence is Confidence.HIGH
        assert app.tyres.degradation_s_per_lap == pytest.approx(0.061, abs=0.005)

    def test_short_stint_reports_insufficient(self, app):
        drive(app, 2)
        assert app.tyres.describe_degradation() == "INSUFFICIENT DATA"

    def test_not_rebuilt_mid_lap(self, app):
        """Expensive analysis must not run per UDP packet."""
        drive(app, 4)
        before = app.stints

        for _ in range(30):
            app._on_telemetry_frame(
                TelemetryFrame(valid=True, game="f1", current_lap=5,
                               tyre_compound="Medium", tyre_age_laps=5)
            )

        assert app.stints is before, "stints rebuilt without a lap change"


class TestSessionAndModeIsolation:
    def test_reset_clears_stints(self, app):
        drive(app, 6)
        app.reset_session()
        assert app.stints == []
        assert not app.tyres.available

    def test_mode_switch_clears_stints(self, app):
        drive(app, 6)
        app.set_mode(GameMode.F1_26)

        assert app.stints == []
        assert not app.tyres.available

    def test_mode_switch_does_not_disturb_mode_scoped_data(self, app):
        """Stints reset, but the per-mode databases must not."""
        drive(app, 6)
        f25_cars = {car.car_id for car in app.cars.all}

        app.set_mode(GameMode.F1_26)
        assert {car.car_id for car in app.cars.all} != f25_cars

        app.set_mode(GameMode.F1_25)
        assert {car.car_id for car in app.cars.all} == f25_cars


class TestTyresPage:
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

    def test_page_is_registered(self, window):
        win, _ = window
        names = {type(win.stack.widget(i)).__name__ for i in range(win.stack.count())}
        assert "TyresPage" in names

    def test_renders_without_telemetry(self, window):
        win, qt = window
        page = self._page(win, "TyresPage")
        page.refresh(win.app.report())
        qt.processEvents()
        assert page._compound._value.text() == "-"
        assert page._table.rowCount() == 0

    def test_shows_measured_degradation(self, window):
        win, qt = window
        drive(win.app, 12, deg=0.061)

        page = self._page(win, "TyresPage")
        page.refresh(win.app.report())
        qt.processEvents()

        assert page._compound._value.text() == "Medium"
        assert page._degradation._value.text() == "+0.061s/lap"
        assert page._confidence.text() == Confidence.HIGH.value

    def test_insufficient_data_is_stated_not_faked(self, window):
        win, qt = window
        drive(win.app, 2)

        page = self._page(win, "TyresPage")
        page.refresh(win.app.report())
        qt.processEvents()

        assert page._degradation._value.text() == "INSUFFICIENT DATA"

    def test_stint_table_lists_every_stint(self, window):
        win, qt = window
        drive(win.app, 8, compound="Medium")
        drive(win.app, 10, compound="Hard", start_lap=9, start_age=1)

        page = self._page(win, "TyresPage")
        page.refresh(win.app.report())
        qt.processEvents()

        assert page._table.rowCount() == 2
        assert page._table.item(0, 1).text() == "Medium"
        assert page._table.item(1, 1).text() == "Hard"

    def test_dashboard_shows_the_current_stint(self, window):
        win, qt = window
        drive(win.app, 12, deg=0.061)

        page = self._page(win, "DashboardPage")
        page.refresh(win.app.report())
        qt.processEvents()

        assert "Stint 1" in page._stint_label.text()
        assert "+0.061s/lap" in page._stint_label.text()

    def test_every_page_still_refreshes(self, window):
        win, qt = window
        drive(win.app, 6)
        report = win.app.report()
        for index in range(win.stack.count()):
            win.stack.widget(index).refresh(report)
        qt.processEvents()
