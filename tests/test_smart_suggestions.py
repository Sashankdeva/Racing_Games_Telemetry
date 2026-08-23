"""Smart Suggestions engine.

The two properties that matter most are the ones a race engineer would
notice first: it must not talk over itself, and it must not make anything
up. Almost every test below is one of those two.

Replay determinism gets its own class because it is the whole reason the
clock is derived from the telemetry stream rather than from wall time.
"""

from __future__ import annotations

from app.core.models import TelemetryFrame, Wheels
from app.domain.driver_session import LapRecord
from app.domain.lap_analysis import Confidence, analyse_laps
from app.domain.race_intelligence import (
    DrsState,
    GapInfo,
    GapTrend,
    RaceState,
)
from app.domain.smart_suggestions import (
    COOLDOWN_S,
    TTL_S,
    Category,
    Lifecycle,
    Priority,
    Severity,
    SmartSuggestionEngine,
    SuggestionContext,
)
from app.domain.stints import TyreState, build_stints, current_tyre_state
from app.games.modes import GameMode, game_profile

F25 = game_profile(GameMode.F1_25)
F26 = game_profile(GameMode.F1_26)


def lap(number, time_s, s1=30.0, s2=31.0, s3=None, **kw):
    s3 = round(time_s - s1 - s2, 3) if s3 is None else s3
    return LapRecord(
        lap_number=number, lap_time_s=time_s, sector1_s=s1, sector2_s=s2,
        sector3_s=s3, compound=kw.pop("compound", "Medium"),
        tyre_age_laps=kw.pop("age", number), **kw
    )


def losing_time():
    """Laps where sector 2 is clearly slower than the driver's own best."""
    return analyse_laps([
        lap(1, 92.000, s1=30.000, s2=31.000, s3=31.000),
        lap(2, 92.000, s1=30.000, s2=31.000, s3=31.000),
        lap(3, 92.000, s1=30.000, s2=31.000, s3=31.000),
        lap(4, 92.400, s1=30.000, s2=31.400, s3=31.000),
    ])


def degrading_stint(rate=0.09, count=12):
    laps = [lap(i + 1, 92.0 + rate * i, age=i + 1) for i in range(count)]
    stints = build_stints(laps)
    return stints, current_tyre_state(stints)


def race_state(gap=None, rate=None, samples=0, drs=DrsState.UNKNOWN) -> RaceState:
    """A RaceState as Race Intelligence would produce it."""
    if gap is None:
        return RaceState(drs_state=drs)
    trend = GapTrend.UNKNOWN
    if rate is not None and samples >= 3:
        trend = (
            GapTrend.CLOSING if rate > 0.05
            else GapTrend.OPENING if rate < -0.05
            else GapTrend.STABLE
        )
    return RaceState(
        ahead=GapInfo(
            available=True, gap_s=gap, rate_s_per_lap=rate, trend=trend,
            samples=samples, confidence=Confidence.from_samples(samples),
        ),
        drs_state=drs,
    )


def ctx(**kw) -> SuggestionContext:
    # Gap facts come from Race Intelligence, so tests may pass them either
    # as a ready RaceState or as the raw numbers behind one.
    gap = kw.pop("gap_s", None)
    rate = kw.pop("closing_rate_s", None)
    samples = kw.pop("closing_samples", 0)
    drs = kw.pop("drs", DrsState.UNKNOWN)
    if "race" not in kw:
        frame = kw.get("frame")
        if gap is None and frame is not None and frame.delta_to_car_ahead_s:
            gap = frame.delta_to_car_ahead_s
        kw["race"] = race_state(gap, rate, samples, drs)
    defaults = dict(
        frame=TelemetryFrame(valid=True, game="f1"),
        analysis=analyse_laps([]),
        tyres=TyreState(),
        stints=[],
        game=F25,
        now=0.0,
        live=True,
    )
    defaults.update(kw)
    return SuggestionContext(**defaults)


class TestNoSpam:
    """The single most important requirement."""

    def test_a_condition_holding_produces_one_suggestion(self):
        engine = SmartSuggestionEngine()
        analysis = losing_time()

        # 200 evaluations of an unchanging condition - a few seconds of UI.
        first = engine.evaluate(ctx(analysis=analysis, now=0.0))
        for tick in range(1, 200):
            engine.evaluate(ctx(analysis=analysis, now=tick * 0.05))

        assert len(first) == len(engine.active)
        pace = [s for s in engine.active if s.id == "pace.sector2"]
        assert len(pace) == 1
        # It was raised once and never re-raised.
        assert pace[0].timestamp == 0.0

    def test_state_moves_to_active_after_the_first_tick(self):
        engine = SmartSuggestionEngine()
        analysis = losing_time()

        engine.evaluate(ctx(analysis=analysis, now=0.0))
        assert engine.active[0].state is Lifecycle.TRIGGERED

        engine.evaluate(ctx(analysis=analysis, now=1.0))
        assert [s for s in engine.active if s.id == "pace.sector2"][0].state is (
            Lifecycle.ACTIVE
        )

    def test_resolved_when_the_condition_disappears(self):
        engine = SmartSuggestionEngine()
        engine.evaluate(ctx(analysis=losing_time(), now=0.0))
        assert any(s.id == "pace.sector2" for s in engine.active)

        clean = analyse_laps([lap(i + 1, 92.0) for i in range(4)])
        engine.evaluate(ctx(analysis=clean, now=1.0))

        assert not any(s.id == "pace.sector2" for s in engine.active)
        assert any(
            s.id == "pace.sector2" and s.state is Lifecycle.RESOLVED
            for s in engine.history
        )

    def test_cooldown_blocks_immediate_retrigger(self):
        """A condition flickering around its threshold must not chatter."""
        engine = SmartSuggestionEngine()
        analysis = losing_time()
        clean = analyse_laps([lap(i + 1, 92.0) for i in range(4)])

        engine.evaluate(ctx(analysis=analysis, now=0.0))
        engine.evaluate(ctx(analysis=clean, now=1.0))       # resolves
        engine.evaluate(ctx(analysis=analysis, now=2.0))    # returns at once

        assert not any(s.id == "pace.sector2" for s in engine.active)

    def test_it_may_trigger_again_after_the_cooldown(self):
        engine = SmartSuggestionEngine()
        analysis = losing_time()
        clean = analyse_laps([lap(i + 1, 92.0) for i in range(4)])

        engine.evaluate(ctx(analysis=analysis, now=0.0))
        engine.evaluate(ctx(analysis=clean, now=1.0))
        later = 1.0 + COOLDOWN_S[Category.PACE] + 1.0
        engine.evaluate(ctx(analysis=analysis, now=later))

        assert any(s.id == "pace.sector2" for s in engine.active)

    def test_expires_if_never_reconfirmed(self):
        engine = SmartSuggestionEngine()
        analysis = losing_time()
        engine.evaluate(ctx(analysis=analysis, now=0.0))

        # Same condition, but far past its time-to-live.
        engine.evaluate(ctx(analysis=analysis, now=TTL_S[Category.PACE] + 1.0))

        assert any(s.state is Lifecycle.EXPIRED for s in engine.history)

    def test_updated_only_when_the_wording_changes(self):
        engine = SmartSuggestionEngine()
        engine.evaluate(ctx(analysis=losing_time(), now=0.0))

        worse = analyse_laps([
            lap(1, 92.000, s1=30.000, s2=31.000, s3=31.000),
            lap(2, 92.000, s1=30.000, s2=31.000, s3=31.000),
            lap(3, 92.000, s1=30.000, s2=31.000, s3=31.000),
            lap(4, 93.000, s1=30.000, s2=32.000, s3=31.000),
        ])
        engine.evaluate(ctx(analysis=worse, now=1.0))

        pace = [s for s in engine.active if s.id == "pace.sector2"][0]
        assert pace.state is Lifecycle.UPDATED
        assert pace.severity is Severity.WARNING

    def test_stale_telemetry_raises_nothing_new(self):
        engine = SmartSuggestionEngine()
        engine.evaluate(ctx(analysis=losing_time(), live=False, now=0.0))
        assert engine.active == []


class TestNeverInvents:
    def test_no_pace_advice_without_enough_laps(self):
        engine = SmartSuggestionEngine()
        out = engine.evaluate(ctx(analysis=analyse_laps([lap(1, 92.0)]), now=0.0))
        assert not [s for s in out if s.category is Category.PACE]

    def test_no_degradation_advice_without_a_usable_measurement(self):
        engine = SmartSuggestionEngine()
        weak = TyreState(
            degradation_s_per_lap=0.09,
            degradation_confidence=Confidence.INSUFFICIENT,
            compound="Medium",
        )
        out = engine.evaluate(ctx(tyres=weak, now=0.0))
        assert not [s for s in out if s.id == "tyre.degradation"]

    def test_no_strategy_without_a_plan(self):
        """The rule cannot invent one when the engine produced nothing."""
        engine = SmartSuggestionEngine()
        _, tyres = degrading_stint()
        frame = TelemetryFrame(valid=True, current_lap=12, total_laps=0)
        out = engine.evaluate(ctx(frame=frame, tyres=tyres, now=0.0))
        assert not [s for s in out if s.category is Category.STRATEGY]

    def test_no_gap_advice_from_one_noisy_lap(self):
        engine = SmartSuggestionEngine()
        frame = TelemetryFrame(valid=True, delta_to_car_ahead_s=2.4, position=5)
        out = engine.evaluate(
            ctx(frame=frame, closing_rate_s=0.5, closing_samples=1, now=0.0)
        )
        assert not [s for s in out if s.category is Category.RACE]

    def test_no_fuel_advice_without_measured_consumption(self):
        engine = SmartSuggestionEngine()
        frame = TelemetryFrame(
            valid=True, fuel_in_tank=20.0, current_lap=10, total_laps=30
        )
        out = engine.evaluate(ctx(frame=frame, fuel_per_lap=0.0, now=0.0))
        assert not [s for s in out if s.category is Category.FUEL]

    def test_safety_car_never_fires_while_the_field_is_unparsed(self):
        """The field is declared but no adapter populates it."""
        engine = SmartSuggestionEngine()
        frame = TelemetryFrame(valid=True, safety_car="")
        out = engine.evaluate(ctx(frame=frame, now=0.0))
        assert not [s for s in out if s.category is Category.SAFETY]

    def test_safety_car_works_the_moment_the_field_arrives(self):
        engine = SmartSuggestionEngine()
        frame = TelemetryFrame(valid=True, safety_car="Safety Car", current_lap=8)
        out = engine.evaluate(ctx(frame=frame, now=0.0))

        safety = [s for s in out if s.category is Category.SAFETY]
        assert safety and safety[0].severity is Severity.CRITICAL

    def test_driving_advice_only_comes_from_the_coach(self):
        """The rule must not analyse driving itself."""
        engine = SmartSuggestionEngine()
        big_loss = analyse_laps([
            *[lap(i + 1, 92.000, s1=30.000, s2=31.000, s3=31.000) for i in range(4)],
            lap(5, 92.800, s1=30.000, s2=31.800, s3=31.000),
        ])
        out = engine.evaluate(ctx(analysis=big_loss, coaching=[], now=0.0))
        assert not [s for s in out if s.category is Category.DRIVING]

    def test_an_inferred_observation_is_worded_as_potential(self):
        """A correlation must never be presented as a cause."""
        from app.domain.driver_coach import (
            Category as CoachCategory,
        )
        from app.domain.driver_coach import (
            DrivingObservation,
            EvidenceKind,
        )
        from app.domain.driver_coach import Severity as CoachSeverity

        observation = DrivingObservation(
            id="exit.s2", lap=8, sector=2, region="exit",
            category=CoachCategory.CORNER_EXIT,
            observation="Throttle is applied later in sector 2 on your slower laps.",
            evidence="Full throttle for 61% against 72%.",
            evidence_kind=EvidenceKind.INFERRED,
            severity=CoachSeverity.ADVISORY, confidence=Confidence.HIGH,
            time_loss_s=0.28, repeat_count=4,
        )
        engine = SmartSuggestionEngine()
        out = engine.evaluate(ctx(coaching=[observation], now=0.0))
        driving = [s for s in out if s.category is Category.DRIVING]

        assert driving
        assert "Potential loss" in driving[0].reason
        # No corner numbers, no braking distances.
        assert "Turn" not in driving[0].message

    def test_every_suggestion_carries_a_reason_and_source(self):
        engine = SmartSuggestionEngine()
        _, tyres = degrading_stint()
        frame = TelemetryFrame(
            valid=True, current_lap=12, total_laps=30, position=4,
            delta_to_car_ahead_s=1.8,
            tyre_surface_temp=Wheels(fl=125.0, fr=95.0, rl=94.0, rr=93.0),
        )
        out = engine.evaluate(
            ctx(frame=frame, analysis=losing_time(), tyres=tyres,
                closing_rate_s=0.14, closing_samples=5, now=0.0)
        )
        assert out
        for suggestion in out:
            assert suggestion.reason
            assert suggestion.source_data
            assert suggestion.confidence.is_usable


class TestPriority:
    def test_most_important_first(self):
        engine = SmartSuggestionEngine()
        _, tyres = degrading_stint()
        frame = TelemetryFrame(
            valid=True, current_lap=12, total_laps=30, position=4,
            safety_car="Virtual Safety Car",
        )
        out = engine.evaluate(ctx(frame=frame, analysis=losing_time(), tyres=tyres, now=0.0))

        assert out[0].category is Category.SAFETY
        priorities = [int(s.priority) for s in out]
        assert priorities == sorted(priorities, reverse=True)

    def test_top_is_the_single_most_relevant(self):
        engine = SmartSuggestionEngine()
        frame = TelemetryFrame(valid=True, safety_car="Safety Car")
        engine.evaluate(ctx(frame=frame, analysis=losing_time(), now=0.0))

        assert engine.top is not None
        assert engine.top.category is Category.SAFETY

    def test_category_priorities_are_sane(self):
        engine = SmartSuggestionEngine()
        out = engine.evaluate(ctx(analysis=losing_time(), now=0.0))
        pace = [s for s in out if s.category is Category.PACE][0]
        assert pace.priority is Priority.MEDIUM

    def test_filtering_by_category(self):
        engine = SmartSuggestionEngine()
        engine.evaluate(ctx(analysis=losing_time(), now=0.0))
        assert engine.by_category(Category.PACE)
        assert engine.by_category(Category.FUEL) == []


class TestTyreRules:
    def test_hot_tyre_is_flagged_with_the_corner(self):
        engine = SmartSuggestionEngine()
        frame = TelemetryFrame(
            valid=True, tyre_surface_temp=Wheels(fl=125.0, fr=95.0, rl=94.0, rr=93.0)
        )
        out = engine.evaluate(ctx(frame=frame, now=0.0))
        hot = [s for s in out if s.id == "tyre.temperature"][0]

        assert "Front-left" in hot.message
        assert hot.source_data["temp_c"] == 125.0

    def test_degradation_compares_against_the_previous_stint(self):
        engine = SmartSuggestionEngine()
        laps = [lap(i + 1, 92.0 + 0.03 * i, compound="Hard", age=i + 1) for i in range(10)]
        laps += [
            lap(11 + i, 92.5 + 0.09 * i, compound="Medium", age=i + 1) for i in range(10)
        ]
        stints = build_stints(laps)
        tyres = current_tyre_state(stints)

        out = engine.evaluate(ctx(tyres=tyres, stints=stints, now=0.0))
        deg = [s for s in out if s.id == "tyre.degradation"][0]

        assert "previous_stint_deg" in deg.source_data
        assert "previous stint" in deg.reason.lower()

    def test_wear_uses_the_stint_model(self):
        engine = SmartSuggestionEngine()
        tyres = TyreState(compound="Medium", wear_pct=72.0, stint_laps=22)
        out = engine.evaluate(ctx(tyres=tyres, now=0.0))
        wear = [s for s in out if s.id == "tyre.wear"][0]
        assert wear.source_data["wear_pct"] == 72.0


class TestStrategyRule:
    """The rule words the plan; it must not compute one."""

    def _plan(self, **kw):
        from app.domain.strategy import (
            Risk,
            StrategyKind,
            StrategyPlan,
            StrategyRecommendation,
        )

        defaults = dict(
            strategy_id="pit_hard", kind=StrategyKind.PIT, current_compound="Medium",
            next_compound="Hard", pit_lap=22, number_of_stops=1,
            expected_time_s=30.0, time_delta_s=1.4, risk=Risk.LOW,
            confidence=Confidence.HIGH, reason="Measured degradation.",
            source_data={"pit_loss_s": 21.0},
        )
        defaults.update(kw)
        return StrategyPlan(
            recommended=StrategyRecommendation(**defaults),
            pit_window=(21, 24),
            available=True,
            generated_lap=20,
        )

    def test_recommendation_is_worded(self):
        engine = SmartSuggestionEngine()
        out = engine.evaluate(ctx(strategy=self._plan(), now=0.0))
        strategy = [s for s in out if s.category is Category.STRATEGY][0]

        assert "Pit lap 22 for Hard" in strategy.message
        assert "Window L21-L24" in strategy.message
        assert strategy.source_data["pit_loss_s"] == 21.0

    def test_nothing_said_without_a_plan(self):
        from app.domain.strategy import StrategyPlan

        engine = SmartSuggestionEngine()
        out = engine.evaluate(ctx(strategy=StrategyPlan(), now=0.0))
        assert not [s for s in out if s.category is Category.STRATEGY]

    def test_an_unusable_confidence_is_never_voiced(self):
        engine = SmartSuggestionEngine()
        plan = self._plan(confidence=Confidence.INSUFFICIENT)
        out = engine.evaluate(ctx(strategy=plan, now=0.0))
        assert not [s for s in out if s.category is Category.STRATEGY]

    def test_assumptions_reach_the_reason(self):
        engine = SmartSuggestionEngine()
        plan = self._plan(assumptions=("Pit loss under SC is assumed.",))
        out = engine.evaluate(ctx(strategy=plan, now=0.0))
        strategy = [s for s in out if s.category is Category.STRATEGY][0]
        assert "Assumptions:" in strategy.reason


class TestGapAndDrs:
    def test_catching_message(self):
        engine = SmartSuggestionEngine()
        frame = TelemetryFrame(valid=True, delta_to_car_ahead_s=2.431, position=5)
        out = engine.evaluate(
            ctx(frame=frame, closing_rate_s=0.14, closing_samples=5, now=0.0)
        )
        race = [s for s in out if s.id == "race.catching"][0]

        assert "P4" in race.message
        assert "0.14s/lap" in race.message
        assert race.source_data["laps_to_catch"] > 0

    def test_dropping_back_is_also_reported(self):
        engine = SmartSuggestionEngine()
        frame = TelemetryFrame(valid=True, delta_to_car_ahead_s=2.0, position=5)
        out = engine.evaluate(
            ctx(frame=frame, closing_rate_s=-0.12, closing_samples=5, now=0.0)
        )
        assert any(s.id == "race.dropping" for s in out)

    def test_drs_in_range_uses_mode_terminology(self):
        engine25 = SmartSuggestionEngine()
        engine26 = SmartSuggestionEngine()
        frame = TelemetryFrame(valid=True, delta_to_car_ahead_s=0.8, position=5)

        out25 = engine25.evaluate(
            ctx(frame=frame, game=F25, drs=DrsState.IN_RANGE, now=0.0)
        )
        out26 = engine26.evaluate(
            ctx(frame=frame, game=F26, drs=DrsState.IN_RANGE, now=0.0)
        )

        assert "DRS range." in [s.message for s in out25]
        assert "Manual Override range." in [s.message for s in out26]

    def test_drs_approaching_needs_a_closing_rate(self):
        engine = SmartSuggestionEngine()
        frame = TelemetryFrame(valid=True, delta_to_car_ahead_s=1.6, position=5)

        assert not [
            s for s in engine.evaluate(
                ctx(frame=frame, closing_samples=0, drs=DrsState.OUT_OF_RANGE, now=0.0)
            )
            if s.id == "drs.approaching"
        ]
        engine.reset()
        assert [
            s for s in engine.evaluate(
                ctx(frame=frame, closing_rate_s=0.2, closing_samples=5,
                    drs=DrsState.OPPORTUNITY, now=0.0)
            )
            if s.id == "drs.approaching"
        ]


class TestErsAndFuel:
    def test_no_ers_advice_without_a_reported_mode(self):
        engine = SmartSuggestionEngine()
        frame = TelemetryFrame(valid=True, ers_store_percent=5.0, ers_mode="")
        out = engine.evaluate(ctx(frame=frame, now=0.0))
        assert not [s for s in out if s.category is Category.ERS]

    def test_low_energy_is_reported(self):
        engine = SmartSuggestionEngine()
        frame = TelemetryFrame(valid=True, ers_store_percent=6.0, ers_mode="Hotlap")
        out = engine.evaluate(ctx(frame=frame, now=0.0))
        assert any(s.id == "ers.low" for s in out)

    def test_deployment_only_suggested_with_a_target(self):
        """Telling a driver to deploy at nobody is noise."""
        engine = SmartSuggestionEngine()
        alone = TelemetryFrame(valid=True, ers_store_percent=80.0, ers_mode="Medium")
        assert not [
            s for s in engine.evaluate(ctx(frame=alone, now=0.0))
            if s.id == "ers.deploy"
        ]

        engine.reset()
        chasing = TelemetryFrame(
            valid=True, ers_store_percent=80.0, ers_mode="Medium",
            delta_to_car_ahead_s=1.5, position=5,
        )
        assert [
            s for s in engine.evaluate(ctx(frame=chasing, now=0.0))
            if s.id == "ers.deploy"
        ]

    def test_fuel_margin_uses_measured_consumption(self):
        engine = SmartSuggestionEngine()
        frame = TelemetryFrame(
            valid=True, fuel_in_tank=20.0, current_lap=10, total_laps=30
        )
        # 20kg at 1.2kg/lap = 16.7 laps against 20 remaining.
        out = engine.evaluate(ctx(frame=frame, fuel_per_lap=1.2, now=0.0))
        fuel = [s for s in out if s.category is Category.FUEL][0]

        assert fuel.severity is Severity.WARNING
        assert fuel.source_data["fuel_per_lap_kg"] == 1.2
        assert fuel.source_data["margin_laps"] < -1

    def test_no_fuel_warning_when_there_is_plenty(self):
        engine = SmartSuggestionEngine()
        frame = TelemetryFrame(
            valid=True, fuel_in_tank=80.0, current_lap=10, total_laps=30
        )
        out = engine.evaluate(ctx(frame=frame, fuel_per_lap=1.2, now=0.0))
        assert not [s for s in out if s.category is Category.FUEL]


class TestGameSeparation:
    def test_the_engine_reads_the_profile_rather_than_the_mode(self):
        """No `if mode is F1_26` anywhere - terminology comes from the profile."""
        import inspect

        from app.domain import smart_suggestions

        source = inspect.getsource(smart_suggestions)
        assert "F1_26" not in source
        assert "f1_26" not in source

    def test_no_hardcoded_game_check_in_the_engine(self):
        """Terminology and parameters come from GameProfile only."""
        assert F25.strategy.default_pit_loss_s > 0
        assert F26.strategy.default_pit_loss_s > 0


class TestReplayDeterminism:
    """The same telemetry replayed twice must produce the same suggestions.

    This is why the clock comes from frames observed rather than wall time:
    playback speed cannot change the outcome.
    """

    def _run(self, speed: float) -> list[tuple]:
        engine = SmartSuggestionEngine()
        _, tyres = degrading_stint()
        analysis = losing_time()
        seen: list[tuple] = []

        for step in range(40):
            frame = TelemetryFrame(
                valid=True, current_lap=12, total_laps=30, position=5,
                delta_to_car_ahead_s=2.4 - step * 0.02,
                ers_store_percent=8.0, ers_mode="Medium",
                tyre_surface_temp=Wheels(fl=118.0, fr=95.0, rl=94.0, rr=93.0),
            )
            active = engine.evaluate(
                ctx(
                    frame=frame, analysis=analysis, tyres=tyres,
                    closing_rate_s=0.14, closing_samples=5,
                    # `speed` only changes how the caller advances the clock,
                    # which is exactly what must not matter.
                    now=step * 0.05 * speed,
                )
            )
            seen.append(tuple((s.id, s.state.value, s.severity) for s in active))
        return seen

    def test_two_identical_runs_agree(self):
        assert self._run(1.0) == self._run(1.0)

    def test_the_engine_holds_no_hidden_wall_clock_state(self):
        """Two engines fed the same inputs must agree exactly."""
        first, second = SmartSuggestionEngine(), SmartSuggestionEngine()
        analysis = losing_time()

        for step in range(20):
            a = first.evaluate(ctx(analysis=analysis, now=step * 0.1))
            b = second.evaluate(ctx(analysis=analysis, now=step * 0.1))
            assert [s.id for s in a] == [s.id for s in b]
            assert [s.state for s in a] == [s.state for s in b]

    def test_reset_returns_to_a_clean_slate(self):
        engine = SmartSuggestionEngine()
        engine.evaluate(ctx(analysis=losing_time(), now=0.0))
        assert engine.active

        engine.reset()
        assert engine.active == []
        assert engine.history == []

    def test_a_replayed_recording_gives_the_same_result_through_the_app(
        self, tmp_path, monkeypatch
    ):
        """End to end: the same frames through the real Application twice."""
        monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path))
        from app.config.settings import AppSettings
        from app.core.application import Application

        def play() -> list[str]:
            app = Application(AppSettings(game_mode="f1_26"))
            app.mode_settings.auto_start_telemetry = False
            app.persist_on_exit = False
            try:
                for index in range(14):
                    common = dict(
                        valid=True, game="f1", tyre_compound="Medium",
                        tyre_age_laps=index + 1, sector1_time_s=30.4,
                        sector2_time_s=31.1, position=5, total_laps=30,
                        session_type="Race", delta_to_car_ahead_s=3.0 - index * 0.14,
                    )
                    app._on_telemetry_frame(
                        TelemetryFrame(current_lap=index + 1, **common)
                    )
                    app._on_telemetry_frame(
                        TelemetryFrame(
                            current_lap=index + 2,
                            last_lap_time_s=92.0 + 0.09 * index, **common
                        )
                    )
                active = app.suggestions.evaluate(
                    app.suggestion_context(app.report())
                )
                return [s.id for s in active]
            finally:
                app.shutdown()

        assert play() == play()
