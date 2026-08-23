"""Phase 2: tyre and stint intelligence.

The failure this module exists to prevent is a confident-looking
degradation figure that describes nothing - fitted across a tyre change,
through an in-lap, or through four laps at the same tyre age.
"""

from __future__ import annotations

import pytest

from app.domain.driver_session import LapRecord
from app.domain.lap_analysis import Confidence
from app.domain.stints import (
    MIN_AGE_SPREAD,
    MIN_LAPS_FOR_DEGRADATION,
    build_stints,
    current_tyre_state,
)


def lap(number, time_s, compound="Medium", age=None, wear=0.0, **kw):
    age = number if age is None else age
    s1 = round(time_s * 0.33, 3)
    s2 = round(time_s * 0.34, 3)
    return LapRecord(
        lap_number=number, lap_time_s=time_s,
        sector1_s=s1, sector2_s=s2, sector3_s=round(time_s - s1 - s2, 3),
        compound=compound, tyre_age_laps=age, tyre_wear_pct=wear, **kw
    )


def run(count, start_lap=1, compound="Medium", base=92.0, deg=0.06, start_age=1, **kw):
    """A stint with a known degradation slope."""
    return [
        lap(
            start_lap + i, base + deg * i,
            compound=compound, age=start_age + i, wear=(start_age + i) * 2.0, **kw
        )
        for i in range(count)
    ]


class TestStintDetection:
    def test_no_laps_no_stints(self):
        assert build_stints([]) == []

    def test_a_single_stint(self):
        stints = build_stints(run(10))
        assert len(stints) == 1
        assert stints[0].compound == "Medium"
        assert stints[0].length == 10

    def test_compound_change_starts_a_new_stint(self):
        laps = run(8) + run(8, start_lap=9, compound="Hard", start_age=1)
        stints = build_stints(laps)

        assert len(stints) == 2
        assert stints[0].compound == "Medium"
        assert stints[1].compound == "Hard"
        assert stints[0].first_lap == 1 and stints[0].last_lap == 8
        assert stints[1].first_lap == 9 and stints[1].last_lap == 16

    def test_tyre_age_reset_starts_a_new_stint(self):
        """Same compound fitted again - only the age counter reveals it."""
        laps = run(8) + run(8, start_lap=9, compound="Medium", start_age=1)
        stints = build_stints(laps)

        assert len(stints) == 2
        assert stints[1].start_age_laps == 1

    def test_lap_times_never_infer_a_stop(self):
        """A slow lap is not a pit stop; only the game's signals count."""
        laps = run(5) + [lap(6, 135.0, compound="Medium", age=6)] + run(5, start_lap=7, start_age=7)
        assert len(build_stints(laps)) == 1

    def test_a_used_set_is_recognised(self):
        stints = build_stints(run(6, start_age=9))
        assert stints[0].started_used
        assert stints[0].start_age_laps == 9

    @pytest.mark.parametrize("start_age", [0, 1])
    def test_fresh_set_is_not_marked_used(self, start_age):
        """A new set has already done a lap by its first completed lap, so
        it reports age 1 - that must not read as scrubbed."""
        assert not build_stints(run(6, start_age=start_age))[0].started_used

    def test_stint_label(self):
        stints = build_stints(run(18))
        assert stints[0].label() == "Medium  L1-18"

    def test_three_stints_are_tracked_separately(self):
        laps = (
            run(18, start_lap=1, compound="Medium")
            + run(22, start_lap=19, compound="Hard", start_age=1)
            + run(10, start_lap=41, compound="Soft", start_age=1)
        )
        stints = build_stints(laps)
        assert [s.compound for s in stints] == ["Medium", "Hard", "Soft"]
        assert [s.length for s in stints] == [18, 22, 10]


class TestDegradation:
    def test_insufficient_data_below_the_threshold(self):
        stints = build_stints(run(MIN_LAPS_FOR_DEGRADATION - 1))
        stint = stints[0]

        assert not stint.has_degradation
        assert stint.describe_degradation() == "INSUFFICIENT DATA"
        assert stint.degradation_s_per_lap == 0.0

    def test_measures_a_known_slope(self):
        stints = build_stints(run(12, deg=0.061))
        stint = stints[0]

        assert stint.has_degradation
        assert stint.degradation_s_per_lap == pytest.approx(0.061, abs=1e-3)
        assert stint.describe_degradation() == "+0.061s/lap"

    def test_confidence_rises_with_clean_laps(self):
        seen = []
        for count in (4, 6, 10, 20):
            seen.append(build_stints(run(count))[0].degradation_confidence)
        assert seen == [
            Confidence.LOW, Confidence.MEDIUM, Confidence.HIGH, Confidence.HIGH
        ]

    def test_needs_spread_in_tyre_age(self):
        """Four laps all reported at the same age cannot describe a trend."""
        laps = [lap(i + 1, 92.0 + i * 0.1, age=5) for i in range(6)]
        stint = build_stints(laps)[0]

        assert not stint.has_degradation
        assert stint.degradation_confidence is Confidence.INSUFFICIENT

    def test_age_spread_threshold_is_respected(self):
        laps = [
            lap(i + 1, 92.0 + i * 0.1, age=5 + min(i, MIN_AGE_SPREAD - 1))
            for i in range(8)
        ]
        assert not build_stints(laps)[0].has_degradation

    def test_pit_lap_does_not_tilt_the_slope(self):
        clean = run(10, deg=0.05)
        with_pit = list(clean)
        with_pit[5] = lap(6, 118.0, age=6, pit_lap=True)  # in-lap

        clean_slope = build_stints(clean)[0].degradation_s_per_lap
        pit_slope = build_stints(with_pit)[0].degradation_s_per_lap

        assert pit_slope == pytest.approx(clean_slope, abs=0.01)

    def test_invalid_lap_does_not_tilt_the_slope(self):
        clean = run(10, deg=0.05)
        with_bad = list(clean)
        with_bad[7] = lap(8, 130.0, age=8, invalid=True)

        assert build_stints(with_bad)[0].degradation_s_per_lap == pytest.approx(
            build_stints(clean)[0].degradation_s_per_lap, abs=0.01
        )

    def test_degradation_is_per_stint_not_per_session(self):
        """The whole point: a fresh set resets the clock.

        Fitted across both stints the slope would be near zero, because the
        second stint's times drop back down. Per stint, each shows its own
        real degradation.
        """
        laps = (
            run(10, start_lap=1, compound="Medium", base=92.0, deg=0.06)
            + run(10, start_lap=11, compound="Hard", base=92.0, deg=0.03, start_age=1)
        )
        stints = build_stints(laps)

        assert stints[0].degradation_s_per_lap == pytest.approx(0.06, abs=0.005)
        assert stints[1].degradation_s_per_lap == pytest.approx(0.03, abs=0.005)

    def test_negative_slope_is_reported_honestly(self):
        """Tyres coming in, or a drying track - do not clamp it to zero."""
        stints = build_stints(run(10, deg=-0.04))
        assert stints[0].degradation_s_per_lap < 0
        assert stints[0].describe_degradation().startswith("-")

    def test_all_laps_excluded_gives_no_degradation(self):
        laps = run(10, pit_lap=True)
        stint = build_stints(laps)[0]
        assert stint.clean_laps == 0
        assert not stint.has_degradation


class TestStintPace:
    def test_best_and_average_use_clean_laps(self):
        laps = run(6, deg=0.1)
        laps.append(lap(7, 140.0, age=7, invalid=True))
        stint = build_stints(laps)[0]

        assert stint.best_lap_s == pytest.approx(92.0)
        assert stint.average_lap_s < 93.0
        assert stint.clean_laps == 6

    def test_wear_is_the_latest_reported(self):
        stint = build_stints(run(8))[0]
        assert stint.wear_pct == pytest.approx(16.0)

    def test_current_age_is_the_latest_reported(self):
        assert build_stints(run(8, start_age=3))[0].current_age_laps == 10


class TestCurrentTyreState:
    def test_empty_without_laps(self):
        state = current_tyre_state([])
        assert not state.available
        assert state.describe_degradation() == "INSUFFICIENT DATA"

    def test_reflects_the_stint_in_progress(self):
        laps = run(8, compound="Medium") + run(12, start_lap=9, compound="Hard", start_age=1)
        state = current_tyre_state(build_stints(laps))

        assert state.compound == "Hard"
        assert state.stint_number == 2
        assert state.stint_laps == 12
        assert state.available

    def test_degradation_carries_its_confidence(self):
        state = current_tyre_state(build_stints(run(12, deg=0.061)))
        assert state.degradation_confidence is Confidence.HIGH
        assert state.describe_degradation() == "+0.061s/lap"

    def test_short_stint_reports_insufficient(self):
        state = current_tyre_state(build_stints(run(2)))
        assert state.describe_degradation() == "INSUFFICIENT DATA"
