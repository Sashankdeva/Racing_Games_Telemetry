"""Regression tests for game-mode isolation.

These exist because two real bugs shipped: the car *directories* were
mode-scoped but `builtin_cars()` returned identical data for both modes, so
the list never appeared to change; and `selected_car` existed in
ModeSettings but nothing ever wrote to it.

Covers A-F from the brief: car database isolation, selected-car isolation,
ERS isolation, DRS isolation, live switching, and UI capability switching.
"""

from __future__ import annotations

import pytest

from app.config.mode_settings import ModeSettings
from app.config.settings import AppSettings
from app.core.application import Application
from app.domain.car_profiles import builtin_cars
from app.domain.track_profiles import builtin_tracks
from app.games.modes import Capability, GameMode, game_profile


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path))
    instance = Application(AppSettings(game_mode="f1_25"))
    instance.mode_settings.auto_start_telemetry = False
    yield instance
    instance.shutdown()


# ---------------------------------------------------------------- A
class TestCarDatabaseIsolation:
    def test_rosters_are_genuinely_different(self):
        """The bug: both modes returned the same list."""
        f25 = {car.car_id for car in builtin_cars(GameMode.F1_25)}
        f26 = {car.car_id for car in builtin_cars(GameMode.F1_26)}
        assert f25 != f26

    def test_f1_26_has_its_own_teams(self):
        ids = {car.car_id for car in builtin_cars(GameMode.F1_26)}
        # Audi replaces Sauber and Cadillac joins for 2026.
        assert "audi_26" in ids
        assert "cadillac_26" in ids
        assert "sauber" not in ids

    def test_f1_26_ratings_are_not_copied_from_f1_25(self):
        """Copying 2025 ratings across would invent a 2026 pecking order."""
        f26 = {car.car_id: car for car in builtin_cars(GameMode.F1_26)}
        assert f26["mclaren_26"].overall == 50.0
        assert f26["mclaren_26"].confidence < 0.1
        assert "unknown" in f26["mclaren_26"].notes.lower()

    def test_f1_25_ratings_still_differentiate(self):
        f25 = {car.car_id: car for car in builtin_cars(GameMode.F1_25)}
        assert f25["mclaren"].race_pace > f25["sauber"].race_pace

    def test_application_swaps_the_database_on_switch(self, app):
        before = {car.car_id for car in app.cars.all}
        app.set_mode(GameMode.F1_26)
        after = {car.car_id for car in app.cars.all}
        assert before != after

    def test_edits_do_not_leak_between_modes(self, app):
        car = app.cars.get("ferrari")
        car.race_pace = 11.0
        app.cars.save(car)

        app.set_mode(GameMode.F1_26)
        assert app.cars.get("ferrari") is None  # different roster entirely

        app.set_mode(GameMode.F1_25)
        assert app.cars.get("ferrari").race_pace == 11.0

    def test_track_calendars_differ(self):
        f25 = {t.track_id for t in builtin_tracks(GameMode.F1_25)}
        f26 = {t.track_id for t in builtin_tracks(GameMode.F1_26)}
        assert "imola" in f25 and "imola" not in f26
        assert "madrid" in f26 and "madrid" not in f25


# ---------------------------------------------------------------- B
class TestSelectedCarIsolation:
    def test_selection_is_stored_per_mode(self, app):
        app.mode_settings.selected_car = "ferrari"
        app.save_mode_settings()

        app.set_mode(GameMode.F1_26)
        assert app.mode_settings.selected_car != "ferrari"
        app.mode_settings.selected_car = "mclaren_26"
        app.save_mode_settings()

        app.set_mode(GameMode.F1_25)
        assert app.mode_settings.selected_car == "ferrari"

        app.set_mode(GameMode.F1_26)
        assert app.mode_settings.selected_car == "mclaren_26"

    def test_selected_track_is_also_per_mode(self, app):
        app.mode_settings.selected_track = "monza"
        app.save_mode_settings()
        app.set_mode(GameMode.F1_26)
        app.mode_settings.selected_track = "madrid"
        app.save_mode_settings()

        app.set_mode(GameMode.F1_25)
        assert app.mode_settings.selected_track == "monza"


# ---------------------------------------------------------------- C
class TestErsIsolation:
    def test_configuration_differs_by_regulation(self):
        f25 = game_profile(GameMode.F1_25).ers
        f26 = game_profile(GameMode.F1_26).ers
        assert f25.has_mguh and not f26.has_mguh
        assert f26.max_deploy_kw > f25.max_deploy_kw
        assert f26.store_joules > f25.store_joules

    def test_f1_26_ers_telemetry_is_marked_unconfirmed(self):
        """Regulation facts are asserted; telemetry mapping is not."""
        assert game_profile(GameMode.F1_26).ers.telemetry_unconfirmed
        assert not game_profile(GameMode.F1_25).ers.telemetry_unconfirmed

    def test_mguh_capability_reflects_the_ruleset(self):
        assert game_profile(GameMode.F1_25).supports(Capability.ERS_MGUH)
        assert not game_profile(GameMode.F1_26).supports(Capability.ERS_MGUH)

    def test_settings_do_not_leak_between_modes(self, app):
        app.mode_settings.ers_reserve = 0.55
        app.save_mode_settings()

        app.set_mode(GameMode.F1_26)
        assert app.mode_settings.ers_reserve != 0.55
        app.mode_settings.ers_reserve = 0.2
        app.save_mode_settings()

        app.set_mode(GameMode.F1_25)
        assert app.mode_settings.ers_reserve == pytest.approx(0.55)

    def test_a_mode_specific_value_is_reconciled_on_switch(self):
        """A deploy mode from one title must not survive into another that
        has no such mode."""
        settings = ModeSettings(mode="f1_26", ers_default_mode="NotARealMode")
        settings.validate_against(game_profile(GameMode.F1_26))
        assert settings.ers_default_mode in game_profile(GameMode.F1_26).ers.modes


# ---------------------------------------------------------------- D
class TestDrsIsolation:
    def test_f1_26_replaces_drs_with_active_aero(self):
        f25 = game_profile(GameMode.F1_25).drs
        f26 = game_profile(GameMode.F1_26).drs
        assert f25.has_drs and not f25.has_active_aero
        assert not f26.has_drs
        assert f26.has_active_aero and f26.has_manual_override

    def test_terminology_differs(self):
        assert game_profile(GameMode.F1_25).term("drs") == "DRS"
        assert game_profile(GameMode.F1_26).term("drs") == "Manual Override"

    def test_aero_modes_only_exist_in_2026(self):
        assert game_profile(GameMode.F1_25).drs.aero_modes == ()
        assert len(game_profile(GameMode.F1_26).drs.aero_modes) >= 2

    def test_capabilities_reflect_the_change(self):
        assert game_profile(GameMode.F1_25).status(Capability.DRS) == "available"
        assert game_profile(GameMode.F1_26).status(Capability.DRS) == "unavailable"
        assert game_profile(GameMode.F1_26).status(Capability.ACTIVE_AERO) == "unconfirmed"

    def test_settings_do_not_leak_between_modes(self, app):
        app.mode_settings.drs_alert_gap_s = 2.5
        app.save_mode_settings()

        app.set_mode(GameMode.F1_26)
        assert app.mode_settings.drs_alert_gap_s != 2.5

        app.set_mode(GameMode.F1_25)
        assert app.mode_settings.drs_alert_gap_s == pytest.approx(2.5)

    def test_aero_mode_is_cleared_where_the_game_has_no_active_aero(self):
        settings = ModeSettings(mode="f1_25", aero_default_mode="X-mode (low drag)")
        settings.validate_against(game_profile(GameMode.F1_25))
        assert settings.aero_default_mode == ""


# ---------------------------------------------------------------- E
class TestLiveSwitching:
    def test_full_round_trip_restores_everything(self, app):
        app.mode_settings.udp_port = 20777
        app.mode_settings.selected_car = "ferrari"
        app.mode_settings.ers_reserve = 0.4
        app.mode_settings.drs_alert_gap_s = 1.8
        app.save_mode_settings()
        f25_cars = {car.car_id for car in app.cars.all}

        app.set_mode(GameMode.F1_26)
        app.mode_settings.udp_port = 20800
        app.mode_settings.selected_car = "audi_26"
        app.mode_settings.ers_reserve = 0.1
        app.mode_settings.drs_alert_gap_s = 0.9
        app.save_mode_settings()
        f26_cars = {car.car_id for car in app.cars.all}

        app.set_mode(GameMode.F1_25)
        assert app.mode_settings.udp_port == 20777
        assert app.mode_settings.selected_car == "ferrari"
        assert app.mode_settings.ers_reserve == pytest.approx(0.4)
        assert app.mode_settings.drs_alert_gap_s == pytest.approx(1.8)
        assert {car.car_id for car in app.cars.all} == f25_cars

        app.set_mode(GameMode.F1_26)
        assert app.mode_settings.udp_port == 20800
        assert app.mode_settings.selected_car == "audi_26"
        assert app.mode_settings.ers_reserve == pytest.approx(0.1)
        assert {car.car_id for car in app.cars.all} == f26_cars

    def test_shared_infrastructure_is_not_recreated(self, app):
        bus, telemetry, inspector = app.bus, app.telemetry, app.inspector
        app.set_mode(GameMode.F1_26)
        app.set_mode(GameMode.F1_25)
        assert app.bus is bus
        assert app.telemetry is telemetry
        assert app.inspector is inspector

    def test_strategy_parameters_are_per_mode(self):
        f25 = game_profile(GameMode.F1_25).strategy
        f26 = game_profile(GameMode.F1_26).strategy
        # A brand-new ruleset must not claim the same confidence.
        assert f26.confidence < f25.confidence


# ---------------------------------------------------------------- F
class TestUiCapabilitySwitching:
    @pytest.fixture
    def window(self, app):
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication

        from app.ui.main_window import MainWindow

        qt = QApplication.instance() or QApplication([])
        app.startup()
        window = MainWindow(app)
        for index in range(window.stack.count()):
            window._on_page_selected(index)
        yield window, qt
        window._timer.stop()

    def _page(self, window, name):
        return [
            window.stack.widget(i)
            for i in range(window.stack.count())
            if type(window.stack.widget(i)).__name__ == name
        ][0]

    def test_car_list_updates_on_switch(self, window):
        win, qt = window
        car_page = self._page(win, "CarPage")

        before = [car_page._combo.itemData(i) for i in range(car_page._combo.count())]
        win.sidebar._mode_combo.setCurrentIndex(1)
        qt.processEvents()
        after = [car_page._combo.itemData(i) for i in range(car_page._combo.count())]

        assert before != after
        assert any(str(k).endswith("_26") for k in after)

    def test_selected_car_restores_per_mode(self, window):
        win, qt = window
        car_page = self._page(win, "CarPage")

        index = car_page._combo.findData("ferrari")
        car_page._combo.setCurrentIndex(index)
        qt.processEvents()

        win.sidebar._mode_combo.setCurrentIndex(1)
        qt.processEvents()
        assert car_page._combo.currentData() != "ferrari"

        win.sidebar._mode_combo.setCurrentIndex(0)
        qt.processEvents()
        assert car_page._combo.currentData() == "ferrari"

    def test_dashboard_terminology_follows_the_mode(self, window):
        win, qt = window
        dashboard = self._page(win, "DashboardPage")

        assert "DRS" in dashboard._drs._label.text()
        win.sidebar._mode_combo.setCurrentIndex(1)
        qt.processEvents()
        assert "OVERRIDE" in dashboard._drs._label.text().upper()

    def test_setup_page_rebuilds_its_controls(self, window):
        win, qt = window
        setup = self._page(win, "SetupPage")

        # F1 25 has no active aero, so no wing-state selector.
        assert setup._aero_mode is None

        win.sidebar._mode_combo.setCurrentIndex(1)
        qt.processEvents()
        setup.on_shown()
        assert setup._aero_mode is not None, "active aero control missing in F1 26"
