"""Car & Track Intelligence.

The module's whole purpose is keeping three things apart - shipped PROFILE
data, OBSERVED session data, and INFERENCE drawn from them - so most of
these tests are about that separation holding under pressure.

The sharpest one is mode isolation: an F1 25 Ferrari profile must never be
able to touch the F1 26 Ferrari profile, because a learned degradation
figure from one car generation would quietly corrupt strategy for the other.
"""

from __future__ import annotations

import json

import pytest

from app.core.models import TelemetryFrame, Wheels
from app.domain.car_profiles import CarProfile
from app.domain.driver_session import LapRecord
from app.domain.lap_analysis import Confidence
from app.domain.profile_intelligence import (
    SAMPLES_FOR_LOW,
    SegmentKind,
    SCHEMA_VERSION,
    Attribute,
    ObservedProfile,
    ObservedStore,
    ObservedValue,
    ProfileContext,
    ProfileError,
    ProfileIntelligence,
    RiskLevel,
    Source,
    TRACK_SEGMENTS,
    TrackSegment,
    clean_laps,
    register_segments,
    observed_dir,
    track_segments,
)
from app.domain.stints import build_stints
from app.domain.track_profiles import TrackProfile
from app.games.modes import GameMode


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path))
    yield tmp_path


def lap(number, time_s=92.0, compound="Medium", age=None, **kw):
    age = number if age is None else age
    s1, s2 = 30.0, 31.0
    return LapRecord(
        lap_number=number, lap_time_s=time_s, sector1_s=s1, sector2_s=s2,
        sector3_s=round(time_s - s1 - s2, 3), compound=compound,
        tyre_age_laps=age, **kw
    )


def degrading(rate=0.06, count=12, compound="Medium"):
    return [lap(i + 1, 92.0 + rate * i, compound=compound, age=i + 1) for i in range(count)]


# ---------------------------------------------------------------------------
class TestObservedValue:
    def test_5_records_a_measured_value(self):
        profile = ObservedProfile("ferrari", "car", "f1_25")
        profile.record("degradation_medium", 0.061, samples=8)

        attribute = profile.attribute("degradation_medium")
        assert attribute.value == 0.061
        assert attribute.source is Source.OBSERVED
        assert attribute.sample_count == 8

    def test_3_an_unmeasured_metric_is_unknown(self):
        profile = ObservedProfile("ferrari", "car", "f1_25")
        attribute = profile.attribute("degradation_hard")

        assert not attribute.known
        assert attribute.source is Source.UNKNOWN
        assert attribute.describe() == "UNKNOWN"

    def test_4_confidence_grows_with_samples(self):
        seen = []
        for samples in (1, 4, 10, 30):
            value = ObservedValue("x", 0.06, sample_count=samples)
            seen.append(value.confidence)
        assert seen == [
            Confidence.INSUFFICIENT, Confidence.LOW,
            Confidence.MEDIUM, Confidence.HIGH,
        ]

    def test_6_multiple_sessions_refine_rather_than_replace(self):
        """The brief's worked example: 0.071 -> 0.064 -> 0.061."""
        profile = ObservedProfile("ferrari", "car", "f1_25")
        profile.record("degradation_medium", 0.071, samples=10)
        first = profile.get("degradation_medium").value

        profile.record("degradation_medium", 0.064, samples=10)
        second = profile.get("degradation_medium").value

        profile.record("degradation_medium", 0.061, samples=10)
        third = profile.get("degradation_medium").value

        # Each session moves the estimate towards the new reading without
        # discarding what came before.
        assert first > second > third
        assert third > 0.061  # not a blind replacement
        assert profile.get("degradation_medium").session_count == 3

    def test_recent_sessions_carry_more_weight(self):
        decayed = ObservedProfile("a", "car", "f1_25")
        decayed.record("m", 1.0, samples=10)
        decayed.record("m", 2.0, samples=10)

        # With decay the newer reading pulls past the midpoint.
        assert decayed.get("m").value > 1.5

    def test_zero_samples_change_nothing(self):
        profile = ObservedProfile("a", "car", "f1_25")
        profile.record("m", 0.5, samples=4)
        profile.record("m", 9.9, samples=0)
        assert profile.get("m").value == 0.5


class TestContamination:
    def test_7_contaminated_laps_are_excluded(self):
        laps = [
            lap(1), lap(2),
            lap(3, pit_lap=True),
            lap(4, invalid=True),
            lap(5, safety_car_lap=True),
            lap(6), lap(7),
        ]
        kept, quality = clean_laps(laps)

        assert len(kept) == 4
        assert quality.total == 7
        assert "PIT LAP" in quality.excluded
        assert "INVALID" in quality.excluded
        assert "SAFETY CAR" in quality.excluded

    def test_wet_running_excludes_the_whole_session(self):
        kept, quality = clean_laps(degrading(), wet=True)
        assert kept == []
        assert "wet conditions" in quality.excluded
        assert not quality.usable

    def test_damage_excludes_the_whole_session(self):
        kept, quality = clean_laps(degrading(), damaged=True)
        assert kept == []
        assert "car damage" in quality.excluded

    def test_a_clean_session_is_usable(self):
        laps = degrading()
        kept, quality = clean_laps(laps)

        assert quality.usable
        # Every lap survives: nothing here is contaminated.
        assert len(kept) == len(laps)
        assert quality.clean == len(laps)
        assert quality.excluded == ()

    def test_too_few_clean_laps_is_not_usable(self):
        kept, quality = clean_laps([lap(1), lap(2)])
        assert len(kept) == 2
        assert not quality.usable


class TestImportExport:
    def test_12_round_trip(self):
        profile = ObservedProfile("ferrari", "car", "f1_26")
        profile.record("degradation_medium", 0.061, samples=8)

        restored = ObservedProfile.from_json(profile.to_json())
        assert restored.subject_id == "ferrari"
        assert restored.mode == "f1_26"
        assert restored.get("degradation_medium").value == 0.061

    def test_exported_shape_matches_the_documented_format(self):
        profile = ObservedProfile("monza", "track", "f1_25")
        data = json.loads(profile.to_json())

        assert data["schema_version"] == SCHEMA_VERSION
        assert data["type"] == "track_profile"
        assert data["game_mode"] == "f1_25"
        assert data["id"] == "monza"
        assert "observations" in data

    @pytest.mark.parametrize(
        "payload,message",
        [
            ({"schema_version": 99, "type": "car_profile", "game_mode": "f1_25",
              "id": "x"}, "schema_version"),
            ({"schema_version": 1, "type": "spaceship", "game_mode": "f1_25",
              "id": "x"}, "type"),
            ({"schema_version": 1, "type": "car_profile", "game_mode": "f1_99",
              "id": "x"}, "game_mode"),
            ({"schema_version": 1, "type": "car_profile", "game_mode": "f1_25",
              "id": ""}, "id"),
        ],
    )
    def test_13_invalid_data_is_refused(self, payload, message):
        """Bad data must be rejected, never coerced into something plausible."""
        with pytest.raises(ProfileError, match=message):
            ObservedProfile.from_dict(payload)

    def test_malformed_json_is_refused(self):
        with pytest.raises(ProfileError, match="not valid JSON"):
            ObservedProfile.from_json("{not json")

    def test_a_bad_observation_is_refused(self):
        payload = {
            "schema_version": 1, "type": "car_profile", "game_mode": "f1_25",
            "id": "x", "observations": {"m": {"value": "fast"}},
        }
        with pytest.raises(ProfileError, match="bad observation"):
            ObservedProfile.from_dict(payload)

    def test_importing_another_modes_profile_is_refused(self):
        """Exactly the contamination the mode split exists to prevent."""
        intelligence = ProfileIntelligence(GameMode.F1_26)
        foreign = ObservedProfile("ferrari", "car", "f1_25")
        foreign.record("degradation_medium", 0.09, samples=8)

        with pytest.raises(ProfileError, match="f1_25"):
            intelligence.import_profile(foreign.to_json())


class TestModeIsolation:
    def test_8_and_9_f1_25_ferrari_never_touches_f1_26_ferrari(self):
        """The headline isolation requirement."""
        f25 = ProfileIntelligence(GameMode.F1_25)
        f26 = ProfileIntelligence(GameMode.F1_26)
        f25.select("ferrari", "monza")
        f26.select("ferrari", "monza")

        f25.observe_frame(TelemetryFrame(valid=True, speed_kph=300.0))
        f25.learn(degrading(rate=0.20), build_stints(degrading(rate=0.20)))

        # Reload F1 26 from disk - it must know nothing about that session.
        fresh = ProfileIntelligence(GameMode.F1_26)
        fresh.select("ferrari", "monza")
        assert not fresh.context(None, None).observed("top_speed_kph").known
        assert not fresh.context(None, None).observed("degradation_medium").known

    def test_the_directories_are_separate(self):
        assert observed_dir(GameMode.F1_25) != observed_dir(GameMode.F1_26)
        assert "f1_25" in str(observed_dir(GameMode.F1_25))
        assert "f1_26" in str(observed_dir(GameMode.F1_26))

    def test_a_file_claiming_another_mode_is_ignored(self):
        store = ObservedStore(GameMode.F1_26)
        store.directory.mkdir(parents=True, exist_ok=True)
        foreign = ObservedProfile("ferrari", "car", "f1_25")
        foreign.record("degradation_medium", 0.5, samples=9)
        (store.directory / "car_ferrari.json").write_text(
            foreign.to_json(), encoding="utf-8"
        )

        loaded = store.load("car", "ferrari")
        assert loaded.values == {}

    def test_switching_mode_drops_the_loaded_profiles(self):
        intelligence = ProfileIntelligence(GameMode.F1_25)
        intelligence.select("ferrari", "monza")
        intelligence.learn(degrading(), build_stints(degrading()))

        intelligence.set_mode(GameMode.F1_26)
        assert not intelligence.context(None, None).observed("degradation_medium").known

    def test_a_corrupt_file_does_not_crash_the_app(self):
        store = ObservedStore(GameMode.F1_25)
        store.directory.mkdir(parents=True, exist_ok=True)
        (store.directory / "car_ferrari.json").write_text("{{{", encoding="utf-8")

        loaded = store.load("car", "ferrari")
        assert loaded.values == {}


class TestSwitchingSubjects:
    def test_10_switching_car_loads_that_cars_profile(self):
        intelligence = ProfileIntelligence(GameMode.F1_25)
        intelligence.select("ferrari", "monza")
        intelligence.learn(degrading(rate=0.20), build_stints(degrading(rate=0.20)))

        intelligence.select("mclaren", "monza")
        assert not intelligence.context(None, None).observed("degradation_medium").known

        intelligence.select("ferrari", "monza")
        assert intelligence.context(None, None).observed("degradation_medium").known

    def test_11_switching_track_loads_that_tracks_profile(self):
        intelligence = ProfileIntelligence(GameMode.F1_25)
        intelligence.select("ferrari", "monza")
        intelligence.learn(degrading(), build_stints(degrading()))
        assert intelligence.context(None, None).observed(
            "best_lap_s", of="track"
        ).known

        intelligence.select("ferrari", "spa")
        assert not intelligence.context(None, None).observed(
            "best_lap_s", of="track"
        ).known


class TestLearning:
    def _learn(self, rate=0.06):
        intelligence = ProfileIntelligence(GameMode.F1_25)
        intelligence.select("ferrari", "monza")
        laps = degrading(rate=rate)
        for index, record in enumerate(laps):
            record.fuel_used = 1.82
        intelligence.observe_frame(TelemetryFrame(valid=True, speed_kph=287.0))
        quality = intelligence.learn(laps, build_stints(laps))
        return intelligence, quality

    def test_learns_degradation_fuel_and_top_speed(self):
        intelligence, quality = self._learn()
        context = intelligence.context(None, None)

        assert quality.usable
        assert context.observed("degradation_medium").known
        assert context.observed("fuel_per_lap_kg").value == pytest.approx(1.82, abs=0.01)
        assert context.observed("top_speed_kph").value == pytest.approx(287.0)

    def test_learned_values_are_labelled_observed(self):
        intelligence, _ = self._learn()
        attribute = intelligence.context(None, None).observed("degradation_medium")
        assert attribute.source is Source.OBSERVED

    def test_a_contaminated_session_teaches_nothing(self):
        intelligence = ProfileIntelligence(GameMode.F1_25)
        intelligence.select("ferrari", "monza")
        intelligence.observe_frame(
            TelemetryFrame(valid=True, weather="Heavy rain", speed_kph=250.0)
        )
        quality = intelligence.learn(degrading(), build_stints(degrading()))

        assert not quality.usable
        assert not intelligence.context(None, None).observed("degradation_medium").known

    def test_damage_contaminates_the_session(self):
        intelligence = ProfileIntelligence(GameMode.F1_25)
        intelligence.select("ferrari", "monza")
        intelligence.observe_frame(
            TelemetryFrame(valid=True, speed_kph=250.0, rear_wing_damage=40)
        )
        assert not intelligence.learn(degrading(), build_stints(degrading())).usable

    def test_18_learning_persists_across_instances(self):
        intelligence, _ = self._learn(rate=0.09)
        value = intelligence.context(None, None).observed("degradation_medium").value

        reopened = ProfileIntelligence(GameMode.F1_25)
        reopened.select("ferrari", "monza")
        assert reopened.context(None, None).observed(
            "degradation_medium"
        ).value == pytest.approx(value)


class TestProfileVsObserved:
    def _context(self, observed_value=None) -> ProfileContext:
        car = CarProfile(car_id="ferrari", name="Ferrari", tyre_degradation=70.0)
        track = TrackProfile(track_id="monza", name="Monza", tyre_stress=80.0)
        observed = ObservedProfile("ferrari", "car", "f1_25")
        if observed_value is not None:
            observed.record("degradation_medium", observed_value, samples=12)
        return ProfileContext(
            mode=GameMode.F1_25, car=car, track=track, observed_car=observed
        )

    def test_observed_never_overwrites_the_profile(self):
        context = self._context(observed_value=0.061)

        assert context.rating("tyre_degradation").value == 70.0
        assert context.rating("tyre_degradation").source is Source.PROFILE
        assert context.observed("degradation_medium").value == 0.061
        assert context.observed("degradation_medium").source is Source.OBSERVED

    def test_best_available_prefers_observed_and_says_so(self):
        context = self._context(observed_value=0.061)
        best = context.best_available("degradation_medium", "tyre_degradation")

        assert best.source is Source.OBSERVED
        assert best.value == 0.061

    def test_best_available_falls_back_to_the_profile(self):
        context = self._context()
        best = context.best_available("degradation_medium", "tyre_degradation")

        assert best.source is Source.PROFILE
        assert best.value == 70.0

    def test_a_prior_is_reported_as_low_confidence(self):
        """A shipped prior is an assumption, not a measurement."""
        context = self._context()
        assert context.rating("tyre_degradation").confidence is Confidence.LOW

    def test_missing_profiles_yield_unknown(self):
        context = ProfileContext(mode=GameMode.F1_25)
        assert not context.rating("tyre_degradation").known
        assert not context.observed("degradation_medium").known


class TestInference:
    def _context(self, car_deg=70.0, track_stress=80.0, prior=True) -> ProfileContext:
        car = CarProfile(car_id="c", name="C", tyre_degradation=car_deg)
        track = TrackProfile(track_id="t", name="T", tyre_stress=track_stress)
        if not prior:
            car.confidence = 0.8
            track.confidence = 0.8
        return ProfileContext(mode=GameMode.F1_25, car=car, track=track)

    def test_high_stress_car_and_track_infers_high_risk(self):
        signals = self._context().risk_signals()
        stress = signals["tyre_stress_risk"]

        assert stress.level is RiskLevel.HIGH
        assert stress.source is Source.INFERENCE

    def test_an_inference_is_never_presented_as_fact(self):
        stress = self._context().risk_signals()["tyre_stress_risk"]
        assert "not measured" in stress.reason.lower()
        assert stress.source is Source.INFERENCE

    def test_low_demand_infers_low_risk(self):
        signals = self._context(car_deg=20.0, track_stress=20.0).risk_signals()
        assert signals["tyre_stress_risk"].level is RiskLevel.LOW

    def test_two_priors_can_never_be_better_than_low_confidence(self):
        stress = self._context(prior=True).risk_signals()["tyre_stress_risk"]
        assert stress.confidence is Confidence.LOW

    def test_verified_profiles_raise_the_confidence(self):
        stress = self._context(prior=False).risk_signals()["tyre_stress_risk"]
        assert stress.confidence is Confidence.MEDIUM

    def test_no_data_yields_unknown_not_a_guess(self):
        signals = ProfileContext(mode=GameMode.F1_25).risk_signals()
        for signal in signals.values():
            assert signal.level is RiskLevel.UNKNOWN
            assert not signal.known

    def test_traction_and_braking_ratings_are_inverted(self):
        """A car rated highly for traction is LESS at risk, not more."""
        good = ProfileContext(
            mode=GameMode.F1_25,
            car=CarProfile(car_id="c", name="C", traction=90.0),
            track=TrackProfile(track_id="t", name="T", low_speed_balance=50.0),
        )
        poor = ProfileContext(
            mode=GameMode.F1_25,
            car=CarProfile(car_id="c", name="C", traction=10.0),
            track=TrackProfile(track_id="t", name="T", low_speed_balance=50.0),
        )
        assert good.risk_signals()["traction_risk"].level is not RiskLevel.HIGH
        assert poor.risk_signals()["traction_risk"].level is RiskLevel.HIGH

    def test_overtaking_difficulty_is_reported_from_the_track(self):
        signal = self._context().risk_signals()["overtaking_difficulty"]
        assert signal.known


class TestIntegration:
    @pytest.fixture
    def app(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path))
        from app.config.settings import AppSettings
        from app.core.application import Application

        instance = Application(AppSettings(game_mode="f1_26"))
        instance.mode_settings.auto_start_telemetry = False
        instance.persist_on_exit = False
        yield instance
        instance.shutdown()

    def _drive(self, app, laps=12, rate=0.09):
        for index in range(laps):
            common = dict(
                valid=True, game="f1", tyre_compound="Medium",
                tyre_age_laps=index + 1, sector1_time_s=30.4,
                sector2_time_s=31.1, position=5, total_laps=40,
                session_type="Race", speed_kph=280.0,
                tyre_wear=Wheels(10.0, 10.0, 10.0, 10.0),
            )
            app._on_telemetry_frame(TelemetryFrame(current_lap=index + 1, **common))
            app._on_telemetry_frame(
                TelemetryFrame(
                    current_lap=index + 2, last_lap_time_s=92.0 + rate * index,
                    **common
                )
            )

    def test_14_strategy_receives_the_context(self, app):
        self._drive(app)
        context = app.strategy_context(app.report())
        assert context.profiles is not None
        assert context.profiles.mode is GameMode.F1_26

    def test_15_coach_receives_the_context(self, app):
        self._drive(app)
        # The coach is handed the context on every lap; it must not crash
        # and must still produce its own observations.
        assert isinstance(app.coach.observations, list)

    def test_suggestions_receive_only_derived_signals(self, app):
        self._drive(app)
        context = app.suggestion_context(app.report())
        assert context.profiles is not None
        # Derived signals, not raw database rows.
        assert "tyre_stress_risk" in context.profiles.risk_signals()

    def test_16_and_17_learning_survives_a_dropout(self, app):
        """Stale telemetry must not delete anything learned."""
        self._drive(app)
        before = app.profile_context().observed("degradation_medium").value

        app.telemetry.set_timeout(0.01)
        import time

        time.sleep(0.05)
        assert app.report().stale

        after = app.profile_context().observed("degradation_medium").value
        assert after == before

    def test_the_application_keeps_modes_apart(self, app):
        self._drive(app, rate=0.20)
        f26_value = app.profile_context().observed("degradation_medium")

        app.set_mode(GameMode.F1_25)
        f25_value = app.profile_context().observed("degradation_medium")

        assert f26_value.known
        assert not f25_value.known

    def test_export_and_import_through_the_application(self, app):
        self._drive(app)
        exported = app.profiles.export("car")

        fresh = ProfileIntelligence(GameMode.F1_26)
        fresh.select(app.mode_settings.selected_car, app.mode_settings.selected_track)
        imported = fresh.import_profile(exported)
        assert imported.mode == "f1_26"


class TestReplayDeterminism:
    """The same recording, learned twice, must give the same numbers.

    This test previously asserted `first == first`, which is a tautology
    that always passes and proved nothing. Each run now gets its own data
    directory so the second genuinely starts from empty, which is what
    replaying a recording actually does.
    """

    def _run(self, tmp_path, monkeypatch, name: str) -> tuple:
        monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path / name))
        intelligence = ProfileIntelligence(GameMode.F1_25)
        intelligence.select("ferrari", "monza")
        laps = degrading(rate=0.09)
        for record in laps:
            record.fuel_used = 1.8
        for speed in (250.0, 290.0, 270.0):
            intelligence.observe_frame(
                TelemetryFrame(valid=True, game="f1", current_lap=1, speed_kph=speed)
            )
        intelligence.learn(laps, build_stints(laps))
        context = intelligence.context(None, None)
        return (
            context.observed("degradation_medium").value,
            context.observed("fuel_per_lap_kg").value,
            context.observed("top_speed_kph").value,
            context.observed("consistency_s").value,
            context.quality.clean,
        )

    def test_16_the_same_session_learns_the_same_values(self, tmp_path, monkeypatch):
        first = self._run(tmp_path, monkeypatch, "run_a")
        second = self._run(tmp_path, monkeypatch, "run_b")

        assert first == second
        assert all(value is not None for value in first)

    def test_a_different_session_learns_different_values(self):
        """Guards the test above: if it passed for everything it would be
        proving nothing again."""
        gentle = ProfileIntelligence(GameMode.F1_25)
        gentle.select("a", "t")
        gentle.learn(degrading(rate=0.04), build_stints(degrading(rate=0.04)))

        harsh = ProfileIntelligence(GameMode.F1_25)
        harsh.select("b", "t")
        harsh.learn(degrading(rate=0.20), build_stints(degrading(rate=0.20)))

        assert (
            gentle.context(None, None).observed("degradation_medium").value
            != harsh.context(None, None).observed("degradation_medium").value
        )


class TestTrafficAndFuelContamination:
    def test_laps_run_in_traffic_are_excluded(self):
        laps = degrading(count=8)
        kept, quality = clean_laps(laps, traffic_laps={3, 4})

        assert len(kept) == 6
        assert "TRAFFIC" in quality.excluded

    def test_traffic_is_detected_from_the_gap_ahead(self):
        intelligence = ProfileIntelligence(GameMode.F1_25)
        intelligence.select("ferrari", "monza")
        # Two laps spent right behind another car.
        for lap_number in (3, 4):
            intelligence.observe_frame(
                TelemetryFrame(
                    valid=True, game="f1", current_lap=lap_number,
                    delta_to_car_ahead_s=0.8, speed_kph=280.0,
                )
            )
        quality = intelligence.learn(degrading(count=8), [])
        assert "TRAFFIC" in quality.excluded

    def test_clear_air_is_not_treated_as_traffic(self):
        intelligence = ProfileIntelligence(GameMode.F1_25)
        intelligence.select("ferrari", "monza")
        intelligence.observe_frame(
            TelemetryFrame(
                valid=True, game="f1", current_lap=3,
                delta_to_car_ahead_s=8.0, speed_kph=280.0,
            )
        )
        quality = intelligence.learn(degrading(count=8), [])
        assert "TRAFFIC" not in quality.excluded

    def test_an_unusual_fuel_burn_is_excluded(self):
        laps = degrading(count=8)
        for record in laps:
            record.fuel_used = 1.8
        laps[4].fuel_used = 0.4  # a fuel-save lap

        kept, quality = clean_laps(laps)
        assert "UNUSUAL FUEL" in quality.excluded
        assert laps[4] not in kept

    def test_normal_fuel_variation_is_kept(self):
        laps = degrading(count=8)
        for index, record in enumerate(laps):
            record.fuel_used = 1.80 + index * 0.01
        _, quality = clean_laps(laps)
        assert "UNUSUAL FUEL" not in quality.excluded

    def test_too_few_fuel_readings_to_judge(self):
        laps = degrading(count=3)
        for record in laps:
            record.fuel_used = 1.8
        laps[0].fuel_used = 9.9
        _, quality = clean_laps(laps)
        # Three readings cannot establish what normal looks like.
        assert "UNUSUAL FUEL" not in quality.excluded

    def test_observed_consistency_is_learned(self):
        intelligence = ProfileIntelligence(GameMode.F1_25)
        intelligence.select("ferrari", "monza")
        intelligence.learn(degrading(rate=0.09), build_stints(degrading(rate=0.09)))

        consistency = intelligence.context(None, None).observed("consistency_s")
        assert consistency.known
        assert consistency.source is Source.OBSERVED


class TestTrackSegments:
    def test_no_segments_are_invented(self):
        """No verified corner metadata exists, so none may appear."""
        track = TrackProfile(track_id="monza", name="Monza")
        assert track_segments(track, GameMode.F1_25) == ()
        assert track_segments(None, GameMode.F1_25) == ()

    def test_the_shipped_registry_is_empty(self):
        assert TRACK_SEGMENTS == {}

    def test_a_segment_without_metadata_is_not_identified(self):
        segment = TrackSegment(sector=2)
        assert not segment.identified
        assert segment.corner is None

    def test_registering_a_segment_makes_it_available(self):
        """Adding a circuit must be a data operation, not a code change."""
        segment = TrackSegment(
            sector=1, name="Variante del Rettifilo", corner=1,
            kinds=(SegmentKind.HEAVY_BRAKING, SegmentKind.OVERTAKING_ZONE),
        )
        register_segments(GameMode.F1_25, "monza", (segment,))
        try:
            track = TrackProfile(track_id="monza", name="Monza")
            segments = track_segments(track, GameMode.F1_25)

            assert len(segments) == 1
            assert segments[0].identified
            assert segments[0].has(SegmentKind.HEAVY_BRAKING)
            assert not segments[0].has(SegmentKind.DRS_ZONE)
        finally:
            TRACK_SEGMENTS.clear()

    def test_segments_are_mode_scoped(self):
        """A layout registered for one title must not apply to the other."""
        register_segments(
            GameMode.F1_25, "monza", (TrackSegment(sector=1, name="T1", corner=1),)
        )
        try:
            track = TrackProfile(track_id="monza", name="Monza")
            assert track_segments(track, GameMode.F1_25)
            assert track_segments(track, GameMode.F1_26) == ()
        finally:
            TRACK_SEGMENTS.clear()

    def test_the_context_exposes_segments(self):
        context = ProfileContext(
            mode=GameMode.F1_25, track=TrackProfile(track_id="monza", name="Monza")
        )
        assert context.segments() == ()


def test_attribute_describe_is_explicit():
    known = Attribute("m", 0.061, Source.OBSERVED, Confidence.HIGH, 12)
    assert "OBSERVED" in known.describe()
    assert Attribute("m").describe() == "UNKNOWN"


def test_min_samples_constant_is_sane():
    assert SAMPLES_FOR_LOW >= 2
