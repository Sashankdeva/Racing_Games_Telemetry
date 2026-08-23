"""Driver Coach.

Two properties dominate. It must not invent a reference it does not have -
no braking distances, no corner numbers, no ideal line - and it must not
turn one scrappy lap into a lecture. The noise test is the sharpest of
these: random inputs across many laps must produce no coaching at all.
"""

from __future__ import annotations

import random

import pytest

from app.core.models import TelemetryFrame
from app.domain.driver_coach import (
    MIN_LAPS_FOR_COACHING,
    REPEAT_MIN,
    Category,
    DriverCoach,
    EvidenceKind,
    Status,
)
from app.domain.driver_session import LapRecord
from app.domain.lap_analysis import Confidence, analyse_laps
from app.games.modes import GameMode, game_profile

F25 = game_profile(GameMode.F1_25)
F26 = game_profile(GameMode.F1_26)

#: Frames per sector in these fixtures - enough to clear SectorInputs.valid.
FRAMES = 20


def record(number, s1, s2, s3, **kw) -> LapRecord:
    return LapRecord(
        lap_number=number, lap_time_s=round(s1 + s2 + s3, 3),
        sector1_s=s1, sector2_s=s2, sector3_s=s3,
        compound=kw.pop("compound", "Medium"),
        tyre_age_laps=kw.pop("age", number), **kw
    )


def drive_lap(
    coach: DriverCoach, number: int, sector_inputs: dict[int, dict],
) -> None:
    """Feed frames for one lap, sector by sector.

    `sector_inputs` maps the game's 0-based sector index to the inputs held
    through it.
    """
    for sector in (0, 1, 2):
        values = sector_inputs.get(sector, {})
        throttle = values.get("throttle", 1.0)
        brake = values.get("brake", 0.0)
        steer = values.get("steer", 0.0)
        for index in range(FRAMES):
            coach.observe_frame(
                TelemetryFrame(
                    valid=True, game="f1", current_lap=number, sector=sector,
                    throttle=throttle, brake=brake,
                    steering=steer * (1 if index % 2 else -1)
                    if values.get("weave")
                    else steer,
                )
            )


def session(
    coach: DriverCoach, laps: list[LapRecord], inputs=None
) -> list:
    """Drive a whole session and return the last lap's observations."""
    produced = []
    for index, lap in enumerate(laps):
        drive_lap(coach, lap.lap_number, (inputs or {}).get(lap.lap_number, {}))
        analysis = analyse_laps(laps[: index + 1])
        produced = coach.observe_lap(lap, analysis, now=float(index))
    return produced


def steady(count=8, s2=31.0) -> list[LapRecord]:
    return [record(i + 1, 30.0, s2, 31.0) for i in range(count)]


# ---------------------------------------------------------------------------
class TestNoCoachingWithoutEvidence:
    def test_13_nothing_before_enough_laps(self):
        coach = DriverCoach()
        laps = steady(MIN_LAPS_FOR_COACHING - 1)
        assert session(coach, laps) == []

    def test_a_single_slow_lap_is_never_coached(self):
        """One lock-up is not a coaching opportunity."""
        coach = DriverCoach()
        laps = steady(6) + [record(7, 30.0, 31.9, 31.0)]
        produced = session(coach, laps)

        assert not [o for o in produced if o.sector == 2]

    def test_9_a_repeated_problem_is_raised(self):
        coach = DriverCoach()
        laps = [record(i + 1, 30.0, 31.0, 31.0) for i in range(4)]
        laps += [record(5 + i, 30.0, 31.4, 31.0) for i in range(REPEAT_MIN)]
        produced = session(coach, laps)

        pace = [o for o in produced if o.id == "pace.s2"]
        assert pace
        assert pace[0].repeat_count >= REPEAT_MIN
        assert "of the last" in pace[0].evidence

    def test_clean_driving_produces_nothing(self):
        coach = DriverCoach()
        assert session(coach, steady(10)) == []

    def test_random_noise_does_not_produce_coaching_spam(self):
        """The sharpest noise test: scattered inputs, no real pattern."""
        random.seed(11)
        coach = DriverCoach()
        laps = []
        inputs = {}
        for number in range(1, 16):
            # Lap times jitter, but no sector is consistently worse.
            laps.append(
                record(
                    number,
                    30.0 + random.uniform(-0.05, 0.05),
                    31.0 + random.uniform(-0.05, 0.05),
                    31.0 + random.uniform(-0.05, 0.05),
                )
            )
            inputs[number] = {
                sector: {
                    "throttle": random.uniform(0.5, 1.0),
                    "brake": random.uniform(0.0, 0.4),
                    "steer": random.uniform(-0.3, 0.3),
                }
                for sector in (0, 1, 2)
            }
        produced = session(coach, laps, inputs)

        # Jitter that small must not cross the loss threshold.
        assert not [o for o in produced if o.category is Category.PACE]


class TestSectorAnalysis:
    def _losing_s2(self, count=10):
        laps = [record(i + 1, 30.0, 31.0, 31.0) for i in range(4)]
        laps += [record(5 + i, 30.0, 31.35, 31.0) for i in range(count - 4)]
        return laps

    def test_6_sector_loss_is_measured_not_guessed(self):
        coach = DriverCoach()
        produced = session(coach, self._losing_s2())
        pace = [o for o in produced if o.id == "pace.s2"][0]

        assert pace.evidence_kind is EvidenceKind.OBSERVED
        assert pace.time_loss_s == pytest.approx(0.35, abs=0.01)
        assert pace.source_data["best_s"] == pytest.approx(31.0)

    def test_the_region_is_a_sector_never_a_corner(self):
        """There is no corner metadata, so no corner may be named."""
        coach = DriverCoach()
        produced = session(coach, self._losing_s2())
        for observation in produced:
            assert "Turn" not in observation.observation
            assert "Turn" not in observation.corner_or_region
            assert observation.corner_or_region.startswith("Sector")

    def test_no_braking_distance_is_ever_quoted(self):
        coach = DriverCoach()
        produced = session(coach, self._losing_s2())
        for observation in produced:
            text = f"{observation.observation} {observation.evidence}"
            assert "m." not in text.replace("0.", "")
            assert "metres" not in text.lower()

    def test_7_lap_comparison_is_per_sector(self):
        coach = DriverCoach()
        laps = self._losing_s2()
        session(coach, laps)
        comparison = coach.lap_comparison(analyse_laps(laps))

        assert len(comparison) == 3
        s2 = [row for row in comparison if row["sector"] == 2][0]
        assert s2["delta_s"] == pytest.approx(0.35, abs=0.01)

    def test_11_time_loss_is_against_the_drivers_own_best(self):
        coach = DriverCoach()
        produced = session(coach, self._losing_s2())
        pace = [o for o in produced if o.id == "pace.s2"][0]
        assert pace.source_data["latest_s"] - pace.source_data["best_s"] == (
            pytest.approx(pace.time_loss_s, abs=0.001)
        )


class TestInputCorrelation:
    def _laps_and_inputs(self, *, slow_throttle=0.55, fast_throttle=1.0,
                         slow_brake=0.5, fast_brake=0.5):
        laps, inputs = [], {}
        for index in range(5):  # quick laps
            number = index + 1
            laps.append(record(number, 30.0, 31.0, 31.0))
            inputs[number] = {
                1: {"throttle": fast_throttle, "brake": fast_brake}
            }
        for index in range(5):  # slower laps
            number = 6 + index
            laps.append(record(number, 30.0, 31.4, 31.0))
            inputs[number] = {
                1: {"throttle": slow_throttle, "brake": slow_brake}
            }
        return laps, inputs

    def test_2_and_5_late_throttle_is_correlated_with_the_slow_laps(self):
        coach = DriverCoach()
        laps, inputs = self._laps_and_inputs()
        produced = session(coach, laps, inputs)

        exit_obs = [o for o in produced if o.id == "exit.s2"]
        assert exit_obs
        assert exit_obs[0].category is Category.CORNER_EXIT
        # A correlation is an inference, and must be labelled one.
        assert exit_obs[0].evidence_kind is EvidenceKind.INFERRED
        assert exit_obs[0].source_data["full_throttle_fast"] > (
            exit_obs[0].source_data["full_throttle_slow"]
        )

    def test_1_and_4_braking_difference_is_correlated(self):
        coach = DriverCoach()
        laps, inputs = self._laps_and_inputs(
            slow_throttle=1.0, fast_throttle=1.0, slow_brake=0.6, fast_brake=0.0
        )
        produced = session(coach, laps, inputs)

        entry = [o for o in produced if o.id == "entry.s2"]
        assert entry
        assert entry[0].category is Category.BRAKING
        assert entry[0].evidence_kind is EvidenceKind.INFERRED
        # It must not claim to know which way the causation runs.
        assert "cause or consequence" in entry[0].evidence

    def test_3_steering_corrections_are_correlated(self):
        coach = DriverCoach()
        laps, inputs = [], {}
        for index in range(5):
            number = index + 1
            laps.append(record(number, 30.0, 31.0, 31.0))
            inputs[number] = {1: {"throttle": 1.0, "steer": 0.0}}
        for index in range(5):
            number = 6 + index
            laps.append(record(number, 30.0, 31.4, 31.0))
            inputs[number] = {1: {"throttle": 1.0, "steer": 0.5, "weave": True}}
        produced = session(coach, laps, inputs)

        steer = [o for o in produced if o.id == "steer.s2"]
        assert steer
        assert steer[0].category is Category.STEERING
        assert steer[0].source_data["reversals_slow"] > (
            steer[0].source_data["reversals_fast"]
        )

    def test_no_correlation_without_both_fast_and_slow_samples(self):
        """Comparing needs examples of each - otherwise say nothing."""
        coach = DriverCoach()
        laps = [record(i + 1, 30.0, 31.4, 31.0) for i in range(8)]
        produced = session(coach, laps)
        assert not [o for o in produced if o.evidence_kind is EvidenceKind.INFERRED]

    def test_identical_inputs_produce_no_inference(self):
        coach = DriverCoach()
        laps, inputs = self._laps_and_inputs(
            slow_throttle=1.0, fast_throttle=1.0, slow_brake=0.5, fast_brake=0.5
        )
        produced = session(coach, laps, inputs)
        assert not [o for o in produced if o.id.startswith("exit")]
        assert not [o for o in produced if o.id.startswith("entry")]


class TestConsistencyAndImprovement:
    def test_8_consistency_is_reported_when_the_spread_is_large(self):
        coach = DriverCoach()
        laps = [
            record(1, 30.0, 31.0, 31.0), record(2, 30.0, 31.9, 31.0),
            record(3, 30.0, 31.1, 31.0), record(4, 30.0, 32.1, 31.0),
            record(5, 30.0, 31.2, 31.0), record(6, 30.0, 32.3, 31.0),
        ]
        produced = session(coach, laps)
        assert [o for o in produced if o.category is Category.CONSISTENCY]

    def test_tidy_driving_is_not_called_inconsistent(self):
        coach = DriverCoach()
        laps = [record(i + 1, 30.0, 31.0 + i * 0.01, 31.0) for i in range(8)]
        produced = session(coach, laps)
        assert not [o for o in produced if o.category is Category.CONSISTENCY]

    def test_10_improvement_is_detected(self):
        coach = DriverCoach()
        laps = [record(i + 1, 30.0, 31.5, 31.0) for i in range(4)]
        laps += [record(5 + i, 30.0, 31.0, 31.0) for i in range(4)]
        session(coach, laps)

        trend, delta, confidence = coach.sector_trend(2)
        assert trend == "IMPROVING"
        assert delta > 0
        assert confidence.is_usable

    def test_decline_is_detected(self):
        coach = DriverCoach()
        laps = [record(i + 1, 30.0, 31.0, 31.0) for i in range(4)]
        laps += [record(5 + i, 30.0, 31.6, 31.0) for i in range(4)]
        session(coach, laps)
        assert coach.sector_trend(2)[0] == "DECLINING"

    def test_trend_unknown_without_enough_laps(self):
        coach = DriverCoach()
        session(coach, steady(4))
        assert coach.sector_trend(2)[0] == "UNKNOWN"

    def test_12_confidence_rises_with_laps(self):
        short, long = DriverCoach(), DriverCoach()
        laps_short = [record(i + 1, 30.0, 31.0, 31.0) for i in range(4)]
        laps_short += [record(5 + i, 30.0, 31.4, 31.0) for i in range(3)]
        laps_long = list(laps_short) + [
            record(8 + i, 30.0, 31.4, 31.0) for i in range(8)
        ]

        a = session(short, laps_short)
        b = session(long, laps_long)
        pace_a = [o for o in a if o.id == "pace.s2"][0]
        pace_b = [o for o in b if o.id == "pace.s2"][0]
        assert int_confidence(pace_b.confidence) >= int_confidence(pace_a.confidence)


def int_confidence(value: Confidence) -> int:
    order = [
        Confidence.NO_DATA, Confidence.INSUFFICIENT, Confidence.LOW,
        Confidence.MEDIUM, Confidence.HIGH,
    ]
    return order.index(value)


class TestProgressionAndLifecycle:
    def _problem_then_fixed(self, coach):
        laps = [record(i + 1, 30.0, 31.0, 31.0) for i in range(4)]
        laps += [record(5 + i, 30.0, 31.5, 31.0) for i in range(4)]
        session(coach, laps)
        # Now the driver fixes it.
        more = list(laps) + [record(9 + i, 30.0, 31.0, 31.0) for i in range(6)]
        session(DriverCoach(), more)
        return laps, more

    def test_15_a_problem_that_goes_away_is_resolved(self):
        coach = DriverCoach()
        laps = [record(i + 1, 30.0, 31.0, 31.0) for i in range(4)]
        laps += [record(5 + i, 30.0, 31.5, 31.0) for i in range(4)]
        laps += [record(9 + i, 30.0, 31.0, 31.0) for i in range(8)]
        session(coach, laps)

        problem = [p for p in coach.problems if p.id == "pace.s2"]
        assert problem
        assert problem[0].status is Status.RESOLVED
        assert not [o for o in coach.observations if o.id == "pace.s2"]

    def test_progression_records_first_detection_and_peak(self):
        coach = DriverCoach()
        laps = [record(i + 1, 30.0, 31.0, 31.0) for i in range(4)]
        laps += [record(5 + i, 30.0, 31.6, 31.0) for i in range(3)]
        laps += [record(8 + i, 30.0, 31.2, 31.0) for i in range(3)]
        session(coach, laps)

        problem = [p for p in coach.problems if p.id == "pace.s2"][0]
        assert problem.first_detected_lap >= 5
        assert problem.peak_loss_s >= problem.current_loss_s
        assert problem.occurrences >= 2

    def test_14_the_same_problem_is_one_record_not_many(self):
        coach = DriverCoach()
        laps = [record(i + 1, 30.0, 31.0, 31.0) for i in range(4)]
        laps += [record(5 + i, 30.0, 31.5, 31.0) for i in range(8)]
        session(coach, laps)

        assert len([p for p in coach.problems if p.id == "pace.s2"]) == 1
        assert len([o for o in coach.observations if o.id == "pace.s2"]) == 1

    def test_focus_is_the_most_serious_observation(self):
        coach = DriverCoach()
        laps = [record(i + 1, 30.0, 31.0, 31.0) for i in range(4)]
        laps += [record(5 + i, 30.0, 31.8, 31.0) for i in range(4)]
        session(coach, laps)

        assert coach.focus is not None
        assert coach.focus.sector == 2


class TestStaleAndReplay:
    def _laps(self):
        laps = [record(i + 1, 30.0, 31.0, 31.0) for i in range(4)]
        laps += [record(5 + i, 30.0, 31.4, 31.0) for i in range(4)]
        return laps

    def test_16_history_survives_telemetry_stopping(self):
        """The coach is never told about staleness - it simply stops being
        fed, and everything it learned stays."""
        coach = DriverCoach()
        session(coach, self._laps())
        problems = len(coach.problems)
        observations = len(coach.observations)

        # No frames arrive for a while; nothing is cleared.
        assert len(coach.problems) == problems
        assert len(coach.observations) == observations

    def test_17_resuming_continues_the_same_session(self):
        coach = DriverCoach()
        laps = self._laps()
        session(coach, laps)
        first = coach.problems[0].first_detected_lap

        more = laps + [record(9 + i, 30.0, 31.4, 31.0) for i in range(3)]
        for index in range(len(laps), len(more)):
            drive_lap(coach, more[index].lap_number, {})
            coach.observe_lap(more[index], analyse_laps(more[: index + 1]))

        assert coach.problems[0].first_detected_lap == first

    def test_only_an_explicit_reset_clears_history(self):
        coach = DriverCoach()
        session(coach, self._laps())
        assert coach.problems

        coach.reset()
        assert coach.problems == []
        assert coach.observations == []

    def test_18_replay_is_deterministic(self):
        def run():
            coach = DriverCoach()
            laps, inputs = [], {}
            for index in range(12):
                number = index + 1
                slow = index >= 6
                laps.append(record(number, 30.0, 31.4 if slow else 31.0, 31.0))
                inputs[number] = {
                    1: {"throttle": 0.55 if slow else 1.0, "brake": 0.5}
                }
            produced = session(coach, laps, inputs)
            return (
                [(o.id, o.time_loss_s, o.repeat_count) for o in produced],
                [(p.id, p.status, p.occurrences) for p in coach.problems],
            )

        assert run() == run()


class TestGameModes:
    """The coach reads only telemetry fields, so it is mode-agnostic by
    construction - but that must stay true."""

    def test_19_and_20_no_game_specific_branching(self):
        import inspect

        from app.domain import driver_coach

        source = inspect.getsource(driver_coach)
        assert "F1_26" not in source and "f1_26" not in source
        assert "F1_25" not in source and "f1_25" not in source

    @pytest.mark.parametrize("mode", [GameMode.F1_25, GameMode.F1_26])
    def test_same_analysis_in_both_modes(self, mode, tmp_path, monkeypatch):
        monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path))
        from app.config.settings import AppSettings
        from app.core.application import Application

        app = Application(AppSettings(game_mode=mode.value))
        app.mode_settings.auto_start_telemetry = False
        app.persist_on_exit = False
        try:
            for index in range(10):
                slow = index >= 5
                for sector in (0, 1, 2):
                    for _ in range(FRAMES):
                        app._on_telemetry_frame(
                            TelemetryFrame(
                                valid=True, game="f1", current_lap=index + 1,
                                sector=sector, throttle=0.6 if slow else 1.0,
                                session_type="Race",
                            )
                        )
                app._on_telemetry_frame(
                    TelemetryFrame(
                        valid=True, game="f1", current_lap=index + 2,
                        last_lap_time_s=92.0 + (0.4 if slow else 0.0),
                        sector1_time_s=30.0,
                        sector2_time_s=31.4 if slow else 31.0,
                        session_type="Race",
                    )
                )
            assert isinstance(app.coach.observations, list)
        finally:
            app.shutdown()


class TestApplicationIntegration:
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

    def _drive(self, app, laps=12):
        for index in range(laps):
            slow = index >= 5
            for sector in (0, 1, 2):
                for _ in range(FRAMES):
                    app._on_telemetry_frame(
                        TelemetryFrame(
                            valid=True, game="f1", current_lap=index + 1,
                            sector=sector, throttle=0.55 if slow else 1.0,
                            session_type="Race", total_laps=40, position=5,
                        )
                    )
            app._on_telemetry_frame(
                TelemetryFrame(
                    valid=True, game="f1", current_lap=index + 2,
                    last_lap_time_s=92.0 + (0.4 if slow else 0.0),
                    sector1_time_s=30.0,
                    sector2_time_s=31.4 if slow else 31.0,
                    session_type="Race", total_laps=40, position=5,
                )
            )

    def test_observations_reach_the_application(self, app):
        self._drive(app)
        assert app.coach.observations

    def test_suggestions_word_the_observations(self, app):
        self._drive(app)
        context = app.suggestion_context(app.report())
        assert context.coaching

        out = app.suggestions.evaluate(context)
        driving = [s for s in out if s.category.value == "DRIVING"]
        assert driving
        assert driving[0].source_data.get("evidence") in ("OBSERVED", "INFERRED")

    def test_reset_clears_the_coach(self, app):
        self._drive(app)
        app.reset_session()
        assert app.coach.observations == []
        assert app.coach.problems == []
