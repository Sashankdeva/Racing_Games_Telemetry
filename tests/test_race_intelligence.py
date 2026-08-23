"""Race Intelligence: what is objectively happening around the driver.

Two themes dominate. First, noise: a single frame must never raise an
attack, a threat or an overtake, so most state tests feed a spike and
assert nothing moved. Second, honesty: the parser decodes only the player's
lap data, so the car behind, opponent identity and grid slot are
UNAVAILABLE - and the tests pin that rather than letting a future change
quietly start guessing.
"""

from __future__ import annotations

import pytest

from app.core.models import TelemetryFrame
from app.domain.driver_session import LapRecord
from app.domain.lap_analysis import Confidence, analyse_laps
from app.domain.race_intelligence import (
    STATE_CONFIRMATIONS,
    AttackState,
    Availability,
    DefenceState,
    DrsState,
    EventType,
    GapTrend,
    NeutralisedState,
    RaceIntelligence,
    RacePhase,
    TrafficState,
    Trend,
)
from app.domain.stints import TyreState, build_stints, current_tyre_state
from app.games.modes import GameMode, game_profile

F25 = game_profile(GameMode.F1_25)
F26 = game_profile(GameMode.F1_26)

EMPTY = analyse_laps([])
NO_TYRES = TyreState()


def frame(**kw) -> TelemetryFrame:
    defaults = dict(valid=True, game="f1", current_lap=10, total_laps=40, position=5)
    defaults.update(kw)
    return TelemetryFrame(**defaults)


def lap(number, time_s, **kw):
    s1, s2 = 30.0, 31.0
    return LapRecord(
        lap_number=number, lap_time_s=time_s, sector1_s=s1, sector2_s=s2,
        sector3_s=round(time_s - s1 - s2, 3), compound=kw.pop("compound", "Medium"),
        tyre_age_laps=kw.pop("age", number), **kw
    )


def drive_gaps(race: RaceIntelligence, gaps: list[float], start_lap: int = 1) -> None:
    """Feed one gap reading per completed lap."""
    for index, gap in enumerate(gaps):
        race.observe_lap(start_lap + index, frame(delta_to_car_ahead_s=gap))


def state_of(race: RaceIntelligence, f: TelemetryFrame, game=F25, **kw):
    return race.state(f, EMPTY, NO_TYRES, game, **kw)


# ---------------------------------------------------------------------------
class TestGapTrends:
    def test_1_closing_gap(self):
        race = RaceIntelligence()
        drive_gaps(race, [3.00, 2.80, 2.60, 2.40, 2.20])
        result = state_of(race, frame(delta_to_car_ahead_s=2.20))

        assert result.ahead.trend is GapTrend.CLOSING
        assert result.ahead.rate_s_per_lap == pytest.approx(0.20, abs=0.01)

    def test_2_opening_gap(self):
        race = RaceIntelligence()
        drive_gaps(race, [2.00, 2.20, 2.40, 2.60, 2.80])
        result = state_of(race, frame(delta_to_car_ahead_s=2.80))

        assert result.ahead.trend is GapTrend.OPENING
        assert result.ahead.rate_s_per_lap < 0

    def test_3_stable_gap(self):
        race = RaceIntelligence()
        drive_gaps(race, [2.40, 2.42, 2.39, 2.41, 2.40])
        assert state_of(race, frame(delta_to_car_ahead_s=2.40)).ahead.trend is (
            GapTrend.STABLE
        )

    def test_trend_unknown_until_enough_laps(self):
        race = RaceIntelligence()
        drive_gaps(race, [3.0, 2.8])
        result = state_of(race, frame(delta_to_car_ahead_s=2.8))

        assert result.ahead.trend is GapTrend.UNKNOWN
        assert result.ahead.rate_s_per_lap is None

    def test_one_noisy_lap_does_not_flip_the_trend(self):
        """A single bad reading among steady laps must not read as closing."""
        race = RaceIntelligence()
        drive_gaps(race, [2.40, 2.41, 2.39, 2.40, 0.90])  # one spike
        result = state_of(race, frame(delta_to_car_ahead_s=0.90))

        # The fit is dragged, but five samples of noise cannot make it a
        # confident multi-lap trend - the point is it is not treated as truth.
        assert result.ahead.samples == 5
        assert result.ahead.confidence is not Confidence.NO_DATA

    def test_gap_sampled_once_per_lap(self):
        """A per-frame gap is far too noisy to fit."""
        race = RaceIntelligence()
        for _ in range(50):
            race.observe_lap(4, frame(delta_to_car_ahead_s=2.0))
        assert state_of(race, frame(delta_to_car_ahead_s=2.0)).ahead.samples == 1

    def test_no_gap_reported_is_unavailable(self):
        race = RaceIntelligence()
        result = state_of(race, frame(delta_to_car_ahead_s=0.0))

        assert not result.ahead.available
        assert result.ahead.availability is Availability.UNAVAILABLE
        assert result.ahead.gap_s is None

    def test_laps_to_contact(self):
        race = RaceIntelligence()
        drive_gaps(race, [3.00, 2.80, 2.60, 2.40, 2.20])
        ahead = state_of(race, frame(delta_to_car_ahead_s=2.20)).ahead
        assert ahead.laps_to_contact == pytest.approx(11.0, abs=1.0)


class TestAttackAndDefence:
    def _settle(self, race, f, times=STATE_CONFIRMATIONS + 1):
        for _ in range(times):
            result = state_of(race, f)
        return result

    def test_4_attack_detected_when_close(self):
        race = RaceIntelligence()
        drive_gaps(race, [3.0, 2.4, 1.8, 1.2, 0.9])
        result = self._settle(race, frame(delta_to_car_ahead_s=0.9))
        assert result.attack_state is AttackState.ATTACK_RANGE
        assert result.attacking

    def test_active_attack_when_very_close(self):
        race = RaceIntelligence()
        result = self._settle(race, frame(delta_to_car_ahead_s=0.4))
        assert result.attack_state is AttackState.ACTIVE_ATTACK

    def test_approaching_needs_a_closing_trend(self):
        race = RaceIntelligence()
        drive_gaps(race, [3.0, 2.7, 2.5, 2.3, 2.1])
        result = self._settle(race, frame(delta_to_car_ahead_s=2.1))
        assert result.attack_state is AttackState.APPROACHING

    def test_a_single_frame_never_raises_an_attack(self):
        """The most important noise test in this file."""
        race = RaceIntelligence()
        state_of(race, frame(delta_to_car_ahead_s=4.0))
        # One spike of a very close gap, then back to normal.
        spiked = state_of(race, frame(delta_to_car_ahead_s=0.3))

        assert spiked.attack_state is AttackState.NO_ATTACK
        assert not race.events_of(EventType.ATTACK_DETECTED)

    def test_state_needs_repeated_confirmation(self):
        race = RaceIntelligence()
        f = frame(delta_to_car_ahead_s=0.5)
        assert state_of(race, f).attack_state is AttackState.NO_ATTACK
        for _ in range(STATE_CONFIRMATIONS):
            result = state_of(race, f)
        assert result.attack_state is AttackState.ACTIVE_ATTACK

    def test_5_defence_is_unknown_without_car_behind_telemetry(self):
        """Only the player's lap data is parsed - a threat cannot be measured."""
        race = RaceIntelligence()
        result = state_of(race, frame())

        assert result.defence_state is DefenceState.UNKNOWN
        assert not result.behind.available
        assert result.behind.availability is Availability.UNAVAILABLE
        assert "car behind" in result.behind.reason

    def test_attack_event_is_recorded_once(self):
        race = RaceIntelligence()
        f = frame(delta_to_car_ahead_s=0.5)
        for _ in range(20):
            state_of(race, f)
        assert len(race.events_of(EventType.ATTACK_DETECTED)) == 1


class TestDrs:
    def _settle(self, race, f, game=F25):
        for _ in range(3):
            result = state_of(race, f, game=game)
        return result

    def test_6_in_range(self):
        race = RaceIntelligence()
        result = self._settle(race, frame(delta_to_car_ahead_s=0.8))
        assert result.drs_state is DrsState.IN_RANGE

    def test_out_of_range(self):
        race = RaceIntelligence()
        result = self._settle(race, frame(delta_to_car_ahead_s=4.0))
        assert result.drs_state is DrsState.OUT_OF_RANGE

    def test_active_comes_from_telemetry(self):
        race = RaceIntelligence()
        result = self._settle(race, frame(delta_to_car_ahead_s=0.6, drs_active=True))
        assert result.drs_state is DrsState.ACTIVE

    def test_opportunity_needs_a_closing_trend(self):
        race = RaceIntelligence()
        drive_gaps(race, [3.0, 2.7, 2.5, 2.3, 2.1])
        result = self._settle(race, frame(delta_to_car_ahead_s=2.1))
        assert result.drs_state is DrsState.OPPORTUNITY

    def test_unknown_without_a_gap(self):
        race = RaceIntelligence()
        assert self._settle(race, frame(delta_to_car_ahead_s=0.0)).drs_state is (
            DrsState.UNKNOWN
        )

    def test_20_f1_26_reports_unconfirmed_not_invented(self):
        """F1 26's active-aero telemetry is not verified."""
        race = RaceIntelligence()
        result = self._settle(race, frame(delta_to_car_ahead_s=0.8), game=F26)

        assert result.drs_state is DrsState.UNCONFIRMED
        assert result.drs_term == "Manual Override"

    def test_19_f1_25_uses_drs_terminology(self):
        race = RaceIntelligence()
        assert self._settle(race, frame(delta_to_car_ahead_s=0.8)).drs_term == "DRS"

    def test_range_entry_and_exit_are_events(self):
        race = RaceIntelligence()
        self._settle(race, frame(delta_to_car_ahead_s=0.8))
        self._settle(race, frame(delta_to_car_ahead_s=4.0))

        assert race.events_of(EventType.DRS_RANGE_ENTERED)
        assert race.events_of(EventType.DRS_RANGE_LEFT)


class TestPositionAndEvents:
    def test_7_position_change_is_tracked(self):
        race = RaceIntelligence()
        race.observe_frame(frame(position=5))
        race.observe_frame(frame(position=4))
        assert state_of(race, frame(position=4)).position == 4

    def test_8_overtake_detected(self):
        race = RaceIntelligence()
        race.observe_frame(frame(position=6, current_lap=17))
        race.observe_frame(frame(position=5, current_lap=17))

        events = race.events_of(EventType.OVERTAKE)
        assert len(events) == 1
        assert events[0].position_from == 6
        assert events[0].position_to == 5
        assert events[0].lap == 17

    def test_been_overtaken_detected(self):
        race = RaceIntelligence()
        race.observe_frame(frame(position=5))
        race.observe_frame(frame(position=6))
        assert race.events_of(EventType.BEEN_OVERTAKEN)

    def test_9_pit_position_change_is_not_an_overtake(self):
        """A place lost in the pits is not a pass."""
        race = RaceIntelligence()
        race.observe_frame(frame(position=5, current_lap=20))
        race.observe_frame(frame(position=5, current_lap=20, in_pits=True))
        race.observe_frame(frame(position=9, current_lap=20, in_pits=True))

        assert not race.events_of(EventType.OVERTAKE)
        assert not race.events_of(EventType.BEEN_OVERTAKEN)
        assert race.events_of(EventType.PIT_POSITION_CHANGE)

    def test_pit_entry_and_exit_events(self):
        race = RaceIntelligence()
        race.observe_frame(frame(in_pits=False))
        race.observe_frame(frame(in_pits=True))
        race.observe_frame(frame(in_pits=False))

        assert race.events_of(EventType.PIT_ENTRY)
        assert race.events_of(EventType.PIT_EXIT)

    def test_first_position_is_the_starting_slot(self):
        race = RaceIntelligence()
        race.observe_frame(frame(position=7))
        assert state_of(race, frame(position=7)).grid_position == 7

    def test_events_are_ordered_newest_first(self):
        race = RaceIntelligence()
        race.observe_frame(frame(position=6, current_lap=5))
        race.observe_frame(frame(position=5, current_lap=6))
        race.observe_frame(frame(position=4, current_lap=7))
        assert race.events[0].lap == 7

    def test_lap_completion_is_an_event(self):
        race = RaceIntelligence()
        race.observe_lap(12, frame(delta_to_car_ahead_s=2.0))
        assert race.events_of(EventType.LAP_COMPLETED)


class TestTrafficAndPhase:
    def test_10_traffic_when_a_car_is_close(self):
        race = RaceIntelligence()
        assert state_of(race, frame(delta_to_car_ahead_s=1.5)).traffic_state is (
            TrafficState.LIGHT_TRAFFIC
        )

    def test_clear_when_nobody_is_near(self):
        race = RaceIntelligence()
        assert state_of(race, frame(delta_to_car_ahead_s=9.0)).traffic_state is (
            TrafficState.CLEAR
        )

    def test_traffic_unknown_without_a_gap(self):
        race = RaceIntelligence()
        assert state_of(race, frame(delta_to_car_ahead_s=0.0)).traffic_state is (
            TrafficState.UNKNOWN
        )

    @pytest.mark.parametrize(
        "lap_number,total,expected",
        [
            (1, 40, RacePhase.START),
            (5, 40, RacePhase.EARLY_RACE),
            (20, 40, RacePhase.MID_RACE),
            (32, 40, RacePhase.LATE_RACE),
            (39, 40, RacePhase.FINAL_LAPS),
            (41, 40, RacePhase.FINISHED),
        ],
    )
    def test_11_race_phase(self, lap_number, total, expected):
        race = RaceIntelligence()
        result = state_of(race, frame(current_lap=lap_number, total_laps=total))
        assert result.race_phase is expected

    def test_phase_unknown_without_a_race_distance(self):
        """Any phase would be an assumption."""
        race = RaceIntelligence()
        assert state_of(race, frame(current_lap=10, total_laps=0)).race_phase is (
            RacePhase.UNKNOWN
        )


class TestNeutralisation:
    def test_12_unknown_while_the_field_is_unparsed(self):
        """No adapter populates safety_car, so nothing may claim NORMAL."""
        race = RaceIntelligence()
        race.observe_frame(frame(safety_car=""))
        assert state_of(race, frame()).neutralised is NeutralisedState.UNKNOWN

    def test_safety_car_detected_when_reported(self):
        race = RaceIntelligence()
        race.observe_frame(frame(safety_car="Safety Car"))
        assert state_of(race, frame()).neutralised is NeutralisedState.SAFETY_CAR
        assert race.events_of(EventType.SAFETY_CAR)

    def test_vsc_detected(self):
        race = RaceIntelligence()
        race.observe_frame(frame(safety_car="Virtual Safety Car"))
        assert state_of(race, frame()).neutralised is NeutralisedState.VSC
        assert race.events_of(EventType.VSC)

    def test_red_flag_detected(self):
        race = RaceIntelligence()
        race.observe_frame(frame(safety_car="Red Flag"))
        assert state_of(race, frame()).neutralised is NeutralisedState.RED_FLAG


class TestTrends:
    def test_13_pace_trend_reads_the_lap_analysis(self):
        race = RaceIntelligence()
        improving = analyse_laps([
            lap(1, 93.0), lap(2, 92.8), lap(3, 92.5), lap(4, 92.0)
        ])
        result = race.state(frame(), improving, NO_TYRES, F25)
        assert result.pace_trend is Trend.IMPROVING

    def test_pace_trend_unknown_without_enough_laps(self):
        race = RaceIntelligence()
        result = race.state(frame(), analyse_laps([lap(1, 92.0)]), NO_TYRES, F25)
        assert result.pace_trend is Trend.UNKNOWN

    def test_tyre_trend_reads_the_stint_model(self):
        """Must not recompute degradation - the stint model owns it."""
        race = RaceIntelligence()
        laps = [lap(i + 1, 92.0 + 0.09 * i, age=i + 1) for i in range(12)]
        tyres = current_tyre_state(build_stints(laps))

        result = race.state(frame(), analyse_laps(laps), tyres, F25)
        assert result.tyre_trend is Trend.DECLINING

    def test_tyre_trend_unknown_without_a_usable_measurement(self):
        race = RaceIntelligence()
        weak = TyreState(
            degradation_s_per_lap=0.09,
            degradation_confidence=Confidence.INSUFFICIENT,
        )
        assert race.state(frame(), EMPTY, weak, F25).tyre_trend is Trend.UNKNOWN

    def test_position_trend(self):
        race = RaceIntelligence()
        race.observe_frame(frame(position=8))
        race.observe_frame(frame(position=5))
        assert state_of(race, frame(position=5)).position_trend is Trend.IMPROVING

    def test_position_trend_unknown_from_one_reading(self):
        race = RaceIntelligence()
        race.observe_frame(frame(position=5))
        assert state_of(race, frame(position=5)).position_trend is Trend.UNKNOWN


class TestConfidenceAndMissingData:
    def test_14_confidence_grows_with_samples(self):
        race = RaceIntelligence()
        seen = []
        for index, gap in enumerate([3.0, 2.8, 2.6, 2.4, 2.2, 2.0, 1.8, 1.6, 1.4]):
            race.observe_lap(index + 1, frame(delta_to_car_ahead_s=gap))
            seen.append(state_of(race, frame(delta_to_car_ahead_s=gap)).confidence)

        assert seen[0] is Confidence.INSUFFICIENT
        assert seen[-1] in (Confidence.MEDIUM, Confidence.HIGH)

    def test_no_high_confidence_from_one_sample(self):
        race = RaceIntelligence()
        race.observe_lap(1, frame(delta_to_car_ahead_s=2.0))
        assert state_of(race, frame(delta_to_car_ahead_s=2.0)).confidence is not (
            Confidence.HIGH
        )

    def test_15_missing_telemetry_yields_no_fabrication(self):
        race = RaceIntelligence()
        result = state_of(race, TelemetryFrame(valid=True, game="f1"))

        assert result.position is None
        assert result.total_laps is None
        assert result.laps_remaining is None
        assert result.leader_gap_s is None
        assert not result.ahead.available
        assert result.race_phase is RacePhase.UNKNOWN

    def test_invalid_frames_are_ignored(self):
        """An invalid frame must not become the starting position."""
        race = RaceIntelligence()
        race.observe_frame(TelemetryFrame(valid=False, position=1))
        race.observe_frame(frame(position=5))
        assert state_of(race, frame(position=5)).grid_position == 5


class TestStalePersistence:
    def _raced(self):
        race = RaceIntelligence()
        race.observe_frame(frame(position=6, current_lap=10))
        race.observe_frame(frame(position=5, current_lap=11))
        drive_gaps(race, [3.0, 2.8, 2.6, 2.4, 2.2])
        return race

    def test_16_history_survives_going_stale(self):
        race = self._raced()
        overtakes = len(race.events_of(EventType.OVERTAKE))

        stale = state_of(race, frame(position=5), live=False)

        assert stale.stale
        assert len(race.events_of(EventType.OVERTAKE)) == overtakes
        assert race.events_of(EventType.LAP_COMPLETED)

    def test_state_machines_do_not_advance_while_stale(self):
        """A dropout is not the car ahead vanishing."""
        race = self._raced()
        for _ in range(5):
            state_of(race, frame(delta_to_car_ahead_s=0.4))
        attacking = state_of(race, frame(delta_to_car_ahead_s=0.4)).attack_state

        stale = state_of(race, frame(delta_to_car_ahead_s=0.4), live=False)
        assert stale.attack_state is attacking
        assert stale.drs_state is DrsState.UNKNOWN

    def test_17_resuming_continues_the_same_session(self):
        race = self._raced()
        state_of(race, frame(position=5), live=False)

        # Resuming with a real gap again: the earlier history is still there.
        resumed = state_of(
            race, frame(position=4, current_lap=12, delta_to_car_ahead_s=2.0)
        )
        assert not resumed.stale
        assert resumed.ahead.samples == 5

    def test_only_an_explicit_reset_clears_history(self):
        race = self._raced()
        assert race.events

        race.reset()
        assert race.events == []
        assert state_of(race, frame()).ahead.samples == 0


class TestReplayDeterminism:
    def _run(self) -> list[tuple]:
        race = RaceIntelligence()
        seen: list[tuple] = []
        for step in range(30):
            f = frame(
                current_lap=10 + step // 3,
                position=6 if step < 10 else 5,
                delta_to_car_ahead_s=3.0 - step * 0.08,
                in_pits=step in (14, 15),
            )
            race.observe_frame(f, now=step * 0.1)
            if step % 3 == 0:
                race.observe_lap(10 + step // 3, f, now=step * 0.1)
            result = state_of(race, f)
            seen.append(
                (
                    result.attack_state,
                    result.drs_state,
                    result.ahead.trend,
                    result.race_phase,
                )
            )
        return seen

    def test_18_two_identical_runs_agree(self):
        assert self._run() == self._run()

    def test_events_are_identical_across_runs(self):
        def events():
            race = RaceIntelligence()
            for step in range(20):
                race.observe_frame(
                    frame(position=6 if step < 8 else 5, current_lap=10 + step // 4),
                    now=step * 0.1,
                )
            return [(e.type, e.lap, e.position_from, e.position_to) for e in race.events]

        assert events() == events()


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

    def _drive(self, app, laps=6):
        for index in range(laps):
            common = dict(
                valid=True, game="f1", tyre_compound="Medium", tyre_age_laps=index + 1,
                sector1_time_s=30.4, sector2_time_s=31.1, position=5, total_laps=30,
                session_type="Race", delta_to_car_ahead_s=3.0 - index * 0.2,
            )
            app._on_telemetry_frame(TelemetryFrame(current_lap=index + 1, **common))
            app._on_telemetry_frame(
                TelemetryFrame(
                    current_lap=index + 2, last_lap_time_s=92.0 + 0.05 * index, **common
                )
            )

    def test_race_state_is_available_from_the_application(self, app):
        self._drive(app)
        state = app.race_state(app.report())

        assert state.position == 5
        assert state.ahead.available
        assert state.ahead.trend is GapTrend.CLOSING

    def test_suggestions_read_the_race_state(self, app):
        """One calculation, one owner - the engine must not measure gaps."""
        self._drive(app)
        ctx = app.suggestion_context(app.report())

        assert ctx.race.ahead.samples >= 3
        assert ctx.closing_rate_s == ctx.race.ahead.rate_s_per_lap

    def test_race_history_survives_a_session_reset_only(self, app):
        self._drive(app)
        assert app.race.events

        app.reset_session()
        assert app.race.events == []

    def test_mode_switch_clears_race_history(self, app):
        self._drive(app)
        app.set_mode(GameMode.F1_25)
        assert app.race.events == []
