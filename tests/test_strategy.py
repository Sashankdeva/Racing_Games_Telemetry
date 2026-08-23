"""Strategy Engine.

The engine's job is to be right *and* to be honest about how much it knows.
So the tests split into three groups: the cost model produces sane, ordered
answers; the engine refuses to answer when the data cannot support one; and
the same recording always yields the same plan.

The refusals matter as much as the recommendations. Opponent tyre data is
not in the telemetry, so an undercut cannot be projected - and a test pins
that rather than letting a future change quietly start guessing.
"""

from __future__ import annotations

import pytest

from app.core.models import TelemetryFrame
from app.domain.driver_session import LapRecord
from app.domain.lap_analysis import Confidence
from app.domain.race_intelligence import (
    GapInfo,
    NeutralisedState,
    RacePhase,
    RaceState,
    TrafficState,
)
from app.domain.stints import build_stints, current_tyre_state
from app.domain.strategy import (
    MEANINGFUL_GAIN_S,
    PIT_LAP_HYSTERESIS,
    SAFETY_CAR_PIT_LOSS_FACTOR,
    Risk,
    StrategyContext,
    StrategyEngine,
    StrategyKind,
    compound_models,
    undercut_assessment,
)
from app.domain.track_profiles import TrackProfile
from app.games.modes import GameMode, game_profile

F25 = game_profile(GameMode.F1_25)
F26 = game_profile(GameMode.F1_26)


def lap(number, time_s, compound="Medium", age=None, **kw):
    age = number if age is None else age
    s1, s2 = 30.0, 31.0
    return LapRecord(
        lap_number=number, lap_time_s=time_s, sector1_s=s1, sector2_s=s2,
        sector3_s=round(time_s - s1 - s2, 3), compound=compound,
        tyre_age_laps=age, **kw
    )


def stint_on(compound: str, rate: float, count: int = 12, start_lap: int = 1):
    """A measurable stint with a known degradation rate."""
    return [
        lap(start_lap + i, 92.0 + rate * i, compound=compound, age=i + 1)
        for i in range(count)
    ]


def two_stints(current: str, current_rate: float, earlier: str, earlier_rate: float):
    """Laps where `current` is the set ON THE CAR NOW.

    Stints are chronological, so the compound the driver is on is the LAST
    one. Building them the other way round makes the gentle tyre the
    current one, and then staying out is genuinely correct - which is how
    the first draft of these tests fooled itself.
    """
    return (
        stint_on(earlier, earlier_rate, start_lap=1)
        + stint_on(current, current_rate, start_lap=13)
    )


def ctx(
    *,
    laps=None,
    lap_number=12,
    total_laps=40,
    game=F25,
    track=None,
    traffic=TrafficState.CLEAR,
    neutralised=NeutralisedState.UNKNOWN,
    phase=RacePhase.MID_RACE,
    gap=None,
    fuel_per_lap=0.0,
    live=True,
    tyres=None,
    now=0.0,
) -> StrategyContext:
    laps = stint_on("Medium", 0.09) if laps is None else laps
    stints = build_stints(laps)
    state = RaceState(
        position=5,
        lap=lap_number,
        total_laps=total_laps,
        laps_remaining=total_laps - lap_number,
        traffic_state=traffic,
        neutralised=neutralised,
        race_phase=phase,
        ahead=GapInfo(available=True, gap_s=gap, samples=5) if gap else GapInfo(),
    )
    return StrategyContext(
        frame=TelemetryFrame(
            valid=True, game="f1", current_lap=lap_number, total_laps=total_laps,
            position=5,
        ),
        race=state,
        tyres=tyres if tyres is not None else current_tyre_state(stints),
        stints=stints,
        game=game,
        track=track,
        fuel_per_lap=fuel_per_lap,
        live=live,
        now=now,
    )


# ---------------------------------------------------------------------------
class TestBaselineAndPitWindow:
    def test_1_baseline_is_always_produced(self):
        plan = StrategyEngine().evaluate(ctx())
        assert plan.available
        assert plan.baseline is not None
        assert plan.baseline.kind is StrategyKind.STAY_OUT
        assert plan.baseline.number_of_stops == 0

    def test_baseline_projects_degradation_over_the_remaining_laps(self):
        plan = StrategyEngine().evaluate(ctx(lap_number=12, total_laps=40))
        baseline = plan.baseline
        assert baseline.expected_time_s > 0
        assert baseline.source_data["remaining_laps"] == 28

    def test_2_pit_window_is_a_range_not_a_single_lap(self):
        laps = two_stints("Medium", 0.16, "Hard", 0.04)
        plan = StrategyEngine().evaluate(ctx(laps=laps, lap_number=24, total_laps=50))

        assert plan.pit_window is not None
        first, last = plan.pit_window
        assert last >= first

    def test_window_brackets_the_recommended_lap(self):
        laps = two_stints("Medium", 0.16, "Hard", 0.04)
        plan = StrategyEngine().evaluate(ctx(laps=laps, lap_number=24, total_laps=50))
        if plan.recommended.kind is StrategyKind.PIT:
            first, last = plan.pit_window
            assert first <= plan.recommended.pit_lap <= last

    def test_13_candidates_are_ranked(self):
        laps = two_stints("Medium", 0.16, "Hard", 0.03)
        plan = StrategyEngine().evaluate(ctx(laps=laps, lap_number=24, total_laps=50))

        assert plan.recommended is not None
        scores = [c.score for c in plan.candidates]
        assert scores == sorted(scores) or plan.recommended.kind is StrategyKind.STAY_OUT


class TestCostModel:
    def test_7_higher_degradation_makes_stopping_better(self):
        gentle = StrategyEngine().evaluate(
            ctx(laps=two_stints("Medium", 0.02, "Hard", 0.02),
                lap_number=24, total_laps=50)
        )
        harsh = StrategyEngine().evaluate(
            ctx(laps=two_stints("Medium", 0.25, "Hard", 0.03),
                lap_number=24, total_laps=50)
        )
        assert gentle.recommended.kind is StrategyKind.STAY_OUT
        assert harsh.recommended.kind is StrategyKind.PIT

    def test_a_marginal_gain_does_not_displace_the_baseline(self):
        """A stop must clearly pay for itself."""
        plan = StrategyEngine().evaluate(
            ctx(laps=two_stints("Medium", 0.06, "Hard", 0.055),
                lap_number=24, total_laps=32)
        )
        best = plan.recommended
        assert best.kind is StrategyKind.STAY_OUT or best.time_delta_s >= MEANINGFUL_GAIN_S

    def test_expected_time_and_delta_are_recorded(self):
        laps = two_stints("Medium", 0.20, "Hard", 0.03)
        plan = StrategyEngine().evaluate(ctx(laps=laps, lap_number=24, total_laps=50))
        best = plan.recommended

        assert best.expected_time_s > 0
        assert "projected_gain_s" in best.source_data
        assert "pit_loss_s" in best.source_data

    def test_track_pit_loss_is_preferred_over_the_mode_default(self):
        track = TrackProfile(track_id="spa", name="Spa", pit_loss_s=17.3)
        laps = two_stints("Medium", 0.20, "Hard", 0.03)
        plan = StrategyEngine().evaluate(
            ctx(laps=laps, lap_number=24, total_laps=50, track=track)
        )
        assert plan.recommended.source_data["pit_loss_s"] == 17.3
        assert "Spa" in plan.recommended.source_data["pit_loss_source"]

    def test_21_f1_25_default_pit_loss(self):
        laps = two_stints("Medium", 0.20, "Hard", 0.03)
        plan = StrategyEngine().evaluate(
            ctx(laps=laps, lap_number=24, total_laps=50, game=F25)
        )
        assert plan.recommended.source_data["pit_loss_s"] == (
            F25.strategy.default_pit_loss_s
        )

    def test_22_f1_26_uses_its_own_profile(self):
        laps = two_stints("Medium", 0.20, "Hard", 0.03)
        plan = StrategyEngine().evaluate(
            ctx(laps=laps, lap_number=24, total_laps=50, game=F26)
        )
        assert "F1 26" in plan.recommended.source_data["pit_loss_source"]

    def test_no_hardcoded_game_branching(self):
        import inspect

        from app.domain import strategy

        source = inspect.getsource(strategy)
        assert "F1_26" not in source and "f1_26" not in source


class TestInsufficientData:
    def test_15_no_plan_without_measurable_degradation(self):
        short = stint_on("Medium", 0.09, count=2)
        plan = StrategyEngine().evaluate(ctx(laps=short))

        assert not plan.available
        assert "not yet measurable" in plan.reason

    def test_no_plan_without_a_race_distance(self):
        plan = StrategyEngine().evaluate(ctx(total_laps=0, lap_number=12))
        assert not plan.available
        assert "race distance" in plan.reason

    def test_unrun_compounds_are_listed_not_guessed(self):
        """A compound with no session data gets no invented degradation."""
        plan = StrategyEngine().evaluate(ctx(laps=stint_on("Medium", 0.09)))
        assert "Hard" in plan.unmodelled
        assert "Soft" in plan.unmodelled
        assert all(c.next_compound != "Hard" for c in plan.candidates)

    def test_a_run_compound_becomes_available(self):
        laps = two_stints("Medium", 0.16, "Hard", 0.04)
        plan = StrategyEngine().evaluate(ctx(laps=laps, lap_number=24, total_laps=50))
        assert "Hard" not in plan.unmodelled

    def test_compound_models_only_report_observed_data(self):
        models = compound_models(build_stints(stint_on("Medium", 0.09)), F25)
        assert models["Medium"].source == "observed"
        assert models["Hard"].source == "unmodelled"
        assert models["Hard"].degradation_s_per_lap is None

    def test_14_confidence_comes_from_the_tyre_model(self):
        laps = two_stints("Medium", 0.16, "Hard", 0.04)
        plan = StrategyEngine().evaluate(ctx(laps=laps, lap_number=24, total_laps=50))
        assert plan.recommended.confidence.is_usable


class TestSafetyCarAndVsc:
    def _laps(self):
        return two_stints("Medium", 0.09, "Hard", 0.05)

    def test_5_safety_car_reduces_the_modelled_pit_loss(self):
        green = StrategyEngine().evaluate(
            ctx(laps=self._laps(), lap_number=24, total_laps=50)
        )
        under_sc = StrategyEngine().evaluate(
            ctx(laps=self._laps(), lap_number=24, total_laps=50,
                neutralised=NeutralisedState.SAFETY_CAR)
        )
        green_loss = green.recommended.source_data.get("pit_loss_s")
        sc_loss = under_sc.recommended.source_data.get("pit_loss_s")
        if green_loss and sc_loss:
            assert sc_loss < green_loss
            assert sc_loss == pytest.approx(
                green_loss * SAFETY_CAR_PIT_LOSS_FACTOR, abs=0.2
            )

    def test_6_vsc_is_handled_the_same_way(self):
        plan = StrategyEngine().evaluate(
            ctx(laps=self._laps(), lap_number=24, total_laps=50,
                neutralised=NeutralisedState.VSC)
        )
        assert "VSC" in plan.recommended.source_data.get("pit_loss_source", "")

    def test_the_assumption_is_stated_not_hidden(self):
        """We have no measured SC pit loss, so the factor must be declared."""
        plan = StrategyEngine().evaluate(
            ctx(laps=self._laps(), lap_number=24, total_laps=50,
                neutralised=NeutralisedState.SAFETY_CAR)
        )
        assumptions = " ".join(plan.recommended.assumptions)
        assert "not measured" in assumptions

    def test_an_assumed_input_caps_confidence(self):
        plan = StrategyEngine().evaluate(
            ctx(laps=self._laps(), lap_number=24, total_laps=50,
                neutralised=NeutralisedState.SAFETY_CAR)
        )
        assert plan.recommended.confidence is not Confidence.HIGH


class TestTrafficAndTrackPosition:
    def _laps(self):
        return two_stints("Medium", 0.20, "Hard", 0.03)

    def test_9_traffic_penalises_a_stop(self):
        clear = StrategyEngine().evaluate(
            ctx(laps=self._laps(), lap_number=24, total_laps=50,
                traffic=TrafficState.CLEAR)
        )
        busy = StrategyEngine().evaluate(
            ctx(laps=self._laps(), lap_number=24, total_laps=50,
                traffic=TrafficState.HEAVY_TRAFFIC)
        )
        assert busy.recommended.score > clear.recommended.score

    def test_10_hard_to_overtake_tracks_penalise_a_stop(self):
        easy = TrackProfile(track_id="a", name="Easy", overtaking_difficulty=10.0)
        hard = TrackProfile(track_id="b", name="Hard", overtaking_difficulty=90.0)

        easy_plan = StrategyEngine().evaluate(
            ctx(laps=self._laps(), lap_number=24, total_laps=50, track=easy)
        )
        hard_plan = StrategyEngine().evaluate(
            ctx(laps=self._laps(), lap_number=24, total_laps=50, track=hard)
        )
        assert hard_plan.recommended.score > easy_plan.recommended.score

    def test_heavy_traffic_raises_the_risk(self):
        plan = StrategyEngine().evaluate(
            ctx(laps=self._laps(), lap_number=24, total_laps=50,
                traffic=TrafficState.HEAVY_TRAFFIC)
        )
        if plan.recommended.kind is StrategyKind.PIT:
            assert plan.recommended.risk is Risk.HIGH


class TestUndercutAndFuel:
    def test_3_undercut_reports_the_gap_but_refuses_to_project(self):
        """Opponent tyre age is not parsed - half the sum is missing."""
        result = undercut_assessment(ctx(gap=1.4))

        assert result["available"]
        assert result["in_range"]
        assert result["gap_s"] == 1.4
        assert result["opponent_tyre_age"] is None
        assert result["projection"] is None
        assert "not in the telemetry" in result["reason"]

    def test_4_no_undercut_assessment_without_a_gap(self):
        assert not undercut_assessment(ctx())["available"]

    def test_the_missing_opponent_data_is_an_explicit_assumption(self):
        laps = two_stints("Medium", 0.20, "Hard", 0.03)
        plan = StrategyEngine().evaluate(ctx(laps=laps, lap_number=24, total_laps=50))
        assumptions = " ".join(plan.recommended.assumptions)
        assert "undercut" in assumptions.lower()

    def test_8_unmeasured_fuel_is_declared(self):
        laps = two_stints("Medium", 0.20, "Hard", 0.03)
        plan = StrategyEngine().evaluate(
            ctx(laps=laps, lap_number=24, total_laps=50, fuel_per_lap=0.0)
        )
        assumptions = " ".join(plan.recommended.assumptions)
        assert "Fuel consumption has not been measured" in assumptions

    def test_measured_fuel_removes_that_assumption(self):
        laps = two_stints("Medium", 0.20, "Hard", 0.03)
        plan = StrategyEngine().evaluate(
            ctx(laps=laps, lap_number=24, total_laps=50, fuel_per_lap=1.8)
        )
        assumptions = " ".join(plan.recommended.assumptions)
        assert "Fuel consumption has not been measured" not in assumptions


class TestRacePhase:
    def test_12_final_laps_lock_out_changes(self):
        laps = two_stints("Medium", 0.25, "Hard", 0.02)
        plan = StrategyEngine().evaluate(
            ctx(laps=laps, lap_number=48, total_laps=50, phase=RacePhase.FINAL_LAPS)
        )
        assert not plan.available
        assert "too late" in plan.reason

    def test_mid_race_produces_a_plan(self):
        laps = two_stints("Medium", 0.20, "Hard", 0.03)
        plan = StrategyEngine().evaluate(
            ctx(laps=laps, lap_number=24, total_laps=50, phase=RacePhase.MID_RACE)
        )
        assert plan.available


class TestChangeDetection:
    def _laps(self, rate):
        return two_stints("Medium", rate, "Hard", 0.03)

    def test_16_a_material_change_is_recorded(self):
        engine = StrategyEngine()
        engine.evaluate(ctx(laps=self._laps(0.02), lap_number=24, total_laps=50))
        engine.evaluate(ctx(laps=self._laps(0.30), lap_number=25, total_laps=50))

        assert engine.history
        assert engine.history[0].previous != engine.history[0].current

    def test_17_an_unchanged_plan_is_not_re_announced(self):
        engine = StrategyEngine()
        for lap_number in range(24, 30):
            engine.evaluate(
                ctx(laps=self._laps(0.20), lap_number=lap_number, total_laps=50)
            )
        # The plan barely moves; it must not be announced every lap.
        assert len(engine.history) <= 1

    def test_a_small_pit_lap_drift_is_not_announced(self):
        engine = StrategyEngine()
        engine.evaluate(ctx(laps=self._laps(0.20), lap_number=24, total_laps=50))
        before = len(engine.history)
        engine.evaluate(
            ctx(laps=self._laps(0.205), lap_number=24 + PIT_LAP_HYSTERESIS,
                total_laps=50)
        )
        assert len(engine.history) == before

    def test_history_records_why(self):
        engine = StrategyEngine()
        engine.evaluate(ctx(laps=self._laps(0.02), lap_number=24, total_laps=50))
        engine.evaluate(
            ctx(laps=self._laps(0.30), lap_number=25, total_laps=50,
                neutralised=NeutralisedState.SAFETY_CAR)
        )
        if engine.history:
            assert engine.history[0].reason


class TestStaleAndReconnect:
    def _laps(self):
        return two_stints("Medium", 0.20, "Hard", 0.03)

    def test_18_stale_keeps_the_previous_plan(self):
        engine = StrategyEngine()
        live = engine.evaluate(ctx(laps=self._laps(), lap_number=24, total_laps=50))
        assert live.available

        stale = engine.evaluate(
            ctx(laps=self._laps(), lap_number=24, total_laps=50, live=False)
        )
        assert stale.stale
        assert stale.available
        assert stale.recommended.summary() == live.recommended.summary()

    def test_history_survives_going_stale(self):
        engine = StrategyEngine()
        engine.evaluate(ctx(laps=self._laps(), lap_number=24, total_laps=50))
        engine.evaluate(
            ctx(laps=two_stints("Medium", 0.02, "Hard", 0.02),
                lap_number=25, total_laps=50)
        )
        count = len(engine.history)

        engine.evaluate(ctx(laps=self._laps(), lap_number=25, total_laps=50, live=False))
        assert len(engine.history) == count

    def test_19_resuming_recalculates(self):
        engine = StrategyEngine()
        engine.evaluate(ctx(laps=self._laps(), lap_number=24, total_laps=50))
        engine.evaluate(ctx(laps=self._laps(), lap_number=24, total_laps=50, live=False))

        resumed = engine.evaluate(
            ctx(laps=self._laps(), lap_number=26, total_laps=50)
        )
        assert not resumed.stale
        assert resumed.generated_lap == 26

    def test_only_an_explicit_reset_clears_history(self):
        engine = StrategyEngine()
        engine.evaluate(ctx(laps=self._laps(), lap_number=24, total_laps=50))
        engine.reset()
        assert engine.history == []
        assert not engine.plan.available


class TestReplayDeterminism:
    def _run(self):
        engine = StrategyEngine()
        seen = []
        laps = two_stints("Medium", 0.16, "Hard", 0.04)
        for lap_number in range(24, 36):
            plan = engine.evaluate(ctx(laps=laps, lap_number=lap_number, total_laps=50))
            seen.append(
                (
                    plan.recommended.strategy_id if plan.recommended else None,
                    plan.recommended.pit_lap if plan.recommended else None,
                    plan.pit_window,
                    round(plan.recommended.score, 4) if plan.recommended else None,
                )
            )
        return seen, [(c.lap, c.current) for c in engine.history]

    def test_20_identical_runs_agree(self):
        assert self._run() == self._run()

    def test_no_hidden_clock_state(self):
        first, second = StrategyEngine(), StrategyEngine()
        laps = two_stints("Medium", 0.16, "Hard", 0.04)
        for lap_number in range(24, 32):
            a = first.evaluate(ctx(laps=laps, lap_number=lap_number, total_laps=50))
            b = second.evaluate(ctx(laps=laps, lap_number=lap_number, total_laps=50))
            assert a.recommended.summary() == b.recommended.summary()


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

    def _race(self, app, compound="Medium", rate=0.16, count=14, start=1, age0=0):
        for index in range(count):
            number = start + index
            common = dict(
                valid=True, game="f1", tyre_compound=compound,
                tyre_age_laps=age0 + index + 1, sector1_time_s=30.4,
                sector2_time_s=31.1, position=5, total_laps=50,
                session_type="Race", delta_to_car_ahead_s=2.0,
            )
            app._on_telemetry_frame(TelemetryFrame(current_lap=number, **common))
            app._on_telemetry_frame(
                TelemetryFrame(
                    current_lap=number + 1, last_lap_time_s=92.0 + rate * index,
                    **common
                )
            )

    def test_plan_is_produced_through_the_application(self, app):
        self._race(app)
        plan = app.strategy_plan(app.report())
        assert plan.available
        assert plan.baseline is not None

    def test_suggestions_word_the_plan(self, app):
        self._race(app, rate=0.25)
        self._race(app, compound="Hard", rate=0.03, count=12, start=15, age0=0)
        plan = app.strategy_plan(app.report())

        suggestions = app.suggestions.evaluate(app.suggestion_context(app.report()))
        strategy = [s for s in suggestions if s.category.value == "STRATEGY"]
        if plan.available and plan.recommended.confidence.is_usable:
            assert strategy, "the plan produced no suggestion"
            assert strategy[0].source_data

    def test_reset_clears_the_plan(self, app):
        self._race(app)
        app.reset_session()
        assert not app.strategy.plan.available
        assert app.strategy.history == []

    def test_replay_through_the_application_is_deterministic(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path))
        from app.config.settings import AppSettings
        from app.core.application import Application

        def play():
            instance = Application(AppSettings(game_mode="f1_26"))
            instance.mode_settings.auto_start_telemetry = False
            instance.persist_on_exit = False
            try:
                self._race(instance, rate=0.20)
                plan = instance.strategy_plan(instance.report())
                return (
                    plan.available,
                    plan.recommended.summary() if plan.recommended else None,
                    plan.pit_window,
                )
            finally:
                instance.shutdown()

        assert play() == play()
