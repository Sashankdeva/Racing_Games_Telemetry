"""Car and track databases: defaults, editing, persistence, reset."""

from __future__ import annotations

import json

import pytest

from app.domain.car_profiles import CarProfile, builtin_cars, create_car_store
from app.domain.store import dataclass_from_dict
from app.domain.track_profiles import TrackProfile, builtin_tracks, create_track_store


@pytest.fixture
def cars(tmp_path):
    return create_car_store(tmp_path / "cars")


@pytest.fixture
def tracks(tmp_path):
    return create_track_store(tmp_path / "tracks")


class TestBuiltins:
    def test_every_team_has_a_car(self, cars):
        ids = {c.car_id for c in cars.all}
        for expected in ("mclaren", "red_bull", "ferrari", "mercedes", "generic"):
            assert expected in ids

    def test_calendar_tracks_present(self, tracks):
        ids = {t.track_id for t in tracks.all}
        for expected in ("monaco", "monza", "silverstone", "spa", "generic"):
            assert expected in ids

    def test_ratings_are_in_range(self):
        for car in builtin_cars():
            for name, _ in (("overall", ""), ("race_pace", ""), ("cornering", "")):
                assert 0.0 <= getattr(car, name) <= 100.0
        for track in builtin_tracks():
            assert 0.0 <= track.degradation <= 100.0
            assert 10.0 <= track.pit_loss_s <= 45.0

    def test_shipped_values_are_marked_as_priors(self, cars, tracks):
        """They must not masquerade as measured data."""
        assert cars.get("mclaren").is_prior
        assert tracks.get("monaco").is_prior
        assert cars.get("mclaren").confidence < 0.5

    def test_relative_ordering_is_meaningful(self, cars):
        """The point of the ratings is ordering, not absolute values."""
        assert cars.get("mclaren").race_pace > cars.get("sauber").race_pace

    def test_track_characteristics_differentiate(self, tracks):
        """Monaco and Monza must not look alike to a strategy engine."""
        monaco, monza = tracks.get("monaco"), tracks.get("monza")
        assert monaco.overtaking_difficulty > monza.overtaking_difficulty
        assert monza.high_speed_balance > monaco.high_speed_balance
        assert monaco.low_speed_balance > monza.low_speed_balance


class TestEditing:
    def test_save_then_reload(self, tmp_path):
        store = create_car_store(tmp_path / "cars")
        car = store.get("ferrari")
        car.race_pace = 41.0
        assert store.save(car)

        reloaded = create_car_store(tmp_path / "cars")
        assert reloaded.get("ferrari").race_pace == 41.0

    def test_reset_restores_shipped_values(self, cars):
        original = cars.get("ferrari").race_pace
        car = cars.get("ferrari")
        car.race_pace = 10.0
        cars.save(car)
        assert cars.get("ferrari").race_pace == 10.0

        cars.reset("ferrari")
        assert cars.get("ferrari").race_pace == original

    def test_is_customised_tracks_overrides(self, cars):
        assert not cars.is_customised("haas")
        cars.save(cars.get("haas"))
        assert cars.is_customised("haas")
        cars.reset("haas")
        assert not cars.is_customised("haas")

    def test_writes_are_atomic_leaving_no_temp_file(self, cars, tmp_path):
        cars.save(cars.get("alpine"))
        assert list((tmp_path / "cars").glob("*.tmp")) == []

    def test_track_pit_loss_persists(self, tmp_path):
        store = create_track_store(tmp_path / "tracks")
        track = store.get("monaco")
        track.pit_loss_s = 23.5
        store.save(track)
        assert create_track_store(tmp_path / "tracks").get("monaco").pit_loss_s == 23.5


class TestTolerantLoading:
    def test_out_of_range_values_are_clamped(self):
        car = dataclass_from_dict(
            CarProfile, {"car_id": "x", "race_pace": 9999, "cornering": -50}
        ).clamped()
        assert car.race_pace == 100.0
        assert car.cornering == 0.0

    def test_unknown_keys_are_ignored(self):
        car = dataclass_from_dict(
            CarProfile, {"car_id": "x", "race_pace": 60, "from_the_future": True}
        )
        assert car.race_pace == 60.0

    def test_missing_keys_take_defaults(self):
        track = dataclass_from_dict(TrackProfile, {"track_id": "x"})
        assert track.pit_loss_s == 21.0
        assert track.degradation == 50.0

    def test_bad_types_fall_back_rather_than_raising(self):
        car = dataclass_from_dict(CarProfile, {"car_id": "x", "race_pace": "quick"})
        assert car.race_pace == 50.0

    def test_corrupt_file_is_skipped_not_fatal(self, tmp_path):
        directory = tmp_path / "cars"
        directory.mkdir(parents=True)
        (directory / "broken.json").write_text("{ not json", encoding="utf-8")

        store = create_car_store(directory)
        assert len(store.all) >= 10  # builtins still available

    def test_file_with_wrong_shape_is_skipped(self, tmp_path):
        directory = tmp_path / "tracks"
        directory.mkdir(parents=True)
        (directory / "odd.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")

        store = create_track_store(directory)
        assert len(store.all) >= 15


class TestApplicationWiring:
    def test_stores_available_on_application(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path))
        from app.config.settings import AppSettings
        from app.core.application import Application

        app = Application(AppSettings())
        app.mode_settings.auto_start_telemetry = False
        try:
            assert app.cars.get("mclaren") is not None
            assert app.tracks.get("monza") is not None
        finally:
            app.shutdown()
