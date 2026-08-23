"""Game modes: capabilities, per-mode settings, per-mode databases, switching."""

from __future__ import annotations

import pytest

from app.config.mode_settings import ModeSettings
from app.config.settings import AppSettings
from app.core.application import Application
from app.domain.car_profiles import cars_dir, create_car_store
from app.domain.track_profiles import tracks_dir
from app.games.modes import Capability, GameMode, all_profiles, game_profile


class TestGameMode:
    def test_both_modes_exist(self):
        assert {m.value for m in GameMode} == {"f1_25", "f1_26"}

    def test_labels(self):
        assert GameMode.F1_25.label == "F1 25"
        assert GameMode.F1_26.label == "F1 26"

    @pytest.mark.parametrize("value", [None, "", "nonsense", "f1_99"])
    def test_parse_never_raises(self, value):
        """A corrupt settings file must not stop the app starting."""
        assert GameMode.parse(value) is GameMode.F1_25

    def test_parse_roundtrips_known_values(self):
        for mode in GameMode:
            assert GameMode.parse(mode.value) is mode


class TestGameProfiles:
    def test_every_mode_has_a_profile(self):
        assert len(all_profiles()) == len(list(GameMode))

    def test_expected_packet_formats_differ(self):
        """The whole point of versioning: the titles are not assumed equal."""
        assert 2025 in game_profile(GameMode.F1_25).expected_formats
        assert 2026 in game_profile(GameMode.F1_26).expected_formats

    def test_f1_26_also_accepts_2025(self):
        """F1 26 content running inside the F1 25 framework may report
        either format, so both count as expected."""
        assert 2025 in game_profile(GameMode.F1_26).expected_formats

    def test_format_matching(self):
        f26 = game_profile(GameMode.F1_26)
        assert f26.format_matches(2026)
        assert not f26.format_matches(2022)

    def test_core_telemetry_supported_in_both(self):
        for mode in GameMode:
            assert game_profile(mode).supports(Capability.CORE_TELEMETRY)

    def test_unimplemented_features_are_reported_unavailable(self):
        """Never claim a feature that does not exist yet."""
        profile = game_profile(GameMode.F1_25)
        assert profile.status(Capability.STRATEGY) == "unavailable"
        assert not profile.supports(Capability.STRATEGY)

    def test_status_is_one_of_three_states(self):
        for profile in all_profiles():
            for capability in Capability:
                assert profile.status(capability) in (
                    "available", "unconfirmed", "unavailable"
                )

    def test_no_capability_is_both_available_and_unavailable(self):
        for profile in all_profiles():
            assert not (profile.capabilities & profile.unavailable)
            assert not (profile.unconfirmed & profile.capabilities)


class TestModeSettings:
    def test_defaults_are_sane(self, tmp_path):
        settings = ModeSettings.load(GameMode.F1_25, tmp_path / "missing.json")
        assert settings.udp_port == 20777
        assert settings.units == "metric"

    def test_roundtrip(self, tmp_path):
        path = tmp_path / "f1_25.json"
        settings = ModeSettings(mode="f1_25", udp_port=20800, units="imperial")
        assert settings.save(path)

        loaded = ModeSettings.load(GameMode.F1_25, path)
        assert loaded.udp_port == 20800
        assert loaded.units == "imperial"

    def test_file_cannot_claim_a_different_mode(self, tmp_path):
        """Guards against a hand-edited or mis-copied file."""
        path = tmp_path / "f1_25.json"
        ModeSettings(mode="f1_26", udp_port=20801).save(path)
        assert ModeSettings.load(GameMode.F1_25, path).mode == "f1_25"

    def test_bad_values_are_clamped(self, tmp_path):
        path = tmp_path / "x.json"
        ModeSettings(mode="f1_25", udp_port=10, telemetry_timeout=999).save(path)
        loaded = ModeSettings.load(GameMode.F1_25, path)
        assert loaded.udp_port >= 1024
        assert loaded.telemetry_timeout <= 10.0

    def test_unknown_units_fall_back(self, tmp_path):
        path = tmp_path / "x.json"
        ModeSettings(mode="f1_25", units="furlongs").save(path)
        assert ModeSettings.load(GameMode.F1_25, path).units == "metric"

    def test_each_mode_has_its_own_file(self):
        assert ModeSettings.path_for(GameMode.F1_25) != ModeSettings.path_for(
            GameMode.F1_26
        )


class TestModeScopedDatabases:
    def test_car_directories_differ_per_mode(self):
        assert cars_dir(GameMode.F1_25) != cars_dir(GameMode.F1_26)

    def test_track_directories_differ_per_mode(self):
        assert tracks_dir(GameMode.F1_25) != tracks_dir(GameMode.F1_26)

    def test_editing_one_mode_does_not_touch_the_other(self, tmp_path):
        """The same team can be rated differently between titles."""
        store_25 = create_car_store(tmp_path / "f1_25" / "cars")
        store_26 = create_car_store(tmp_path / "f1_26" / "cars")

        car = store_25.get("ferrari")
        car.race_pace = 12.0
        store_25.save(car)

        assert store_25.get("ferrari").race_pace == 12.0
        assert store_26.get("ferrari").race_pace != 12.0


class TestModeSwitching:
    @pytest.fixture
    def app(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path))
        instance = Application(AppSettings(game_mode="f1_25"))
        yield instance
        instance.shutdown()

    def test_starts_in_the_stored_mode(self, app):
        assert app.mode is GameMode.F1_25
        assert app.game.display_name == "F1 25"

    def test_switch_changes_the_active_profile(self, app):
        app.set_mode(GameMode.F1_26)
        assert app.mode is GameMode.F1_26
        assert 2026 in app.game.expected_formats

    def test_switching_preserves_each_modes_settings(self, app):
        app.mode_settings.udp_port = 20777
        app.save_mode_settings()

        app.set_mode(GameMode.F1_26)
        app.mode_settings.udp_port = 20800
        app.save_mode_settings()

        app.set_mode(GameMode.F1_25)
        assert app.mode_settings.udp_port == 20777, "F1 25 settings were clobbered"

        app.set_mode(GameMode.F1_26)
        assert app.mode_settings.udp_port == 20800, "F1 26 settings were lost"

    def test_switching_reloads_the_car_database(self, app):
        """Each mode has its own roster, not the same list relabelled.

        This test previously assumed "ferrari" existed in both modes, which
        was exactly the bug: the directories were mode-scoped but the
        built-in data was shared, so the list never really changed.
        """
        car = app.cars.get("ferrari")
        car.race_pace = 11.0
        app.cars.save(car)

        app.set_mode(GameMode.F1_26)
        # A different roster entirely - 2026 has its own teams.
        assert app.cars.get("ferrari") is None
        assert app.cars.get("ferrari_26") is not None

        app.set_mode(GameMode.F1_25)
        assert app.cars.get("ferrari").race_pace == 11.0

    def test_switch_is_idempotent(self, app):
        app.set_mode(GameMode.F1_25)  # already there
        assert app.mode is GameMode.F1_25

    def test_shared_infrastructure_survives_a_switch(self, app):
        """No restart required: the bus and telemetry state are the same
        objects before and after."""
        bus, telemetry = app.bus, app.telemetry
        app.set_mode(GameMode.F1_26)
        assert app.bus is bus
        assert app.telemetry is telemetry

    def test_mode_persists_to_global_settings(self, app):
        app.set_mode(GameMode.F1_26)
        assert app.settings.game_mode == "f1_26"

    def test_capabilities_available_via_application(self, app):
        assert app.supports(Capability.CORE_TELEMETRY)
        assert not app.supports(Capability.STRATEGY)
