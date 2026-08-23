"""Phase B: lap and sector analysis.

The brief's rule for this phase is "first make the measurements reliable",
so these tests are about correctness of the numbers - especially the ways a
naive implementation quietly produces a wrong one:

  * an invalid (off-track) lap setting the session best
  * a missing sector counting as a 0.000 best
  * a theoretical best built from two sectors out of three
  * standard deviation reported from a single lap
"""

from __future__ import annotations

import pytest

from app.domain.driver_session import LapRecord
from app.domain.lap_analysis import (
    Confidence,
    analyse_laps,
    format_delta,
    format_lap_time,
)


def lap(number, time_s, s1=0.0, s2=0.0, s3=0.0, invalid=False, **kw):
    """A lap record; sectors default to a consistent split of the lap time."""
    if s1 == s2 == s3 == 0.0 and time_s > 0:
        s1, s2 = round(time_s * 0.33, 3), round(time_s * 0.34, 3)
        s3 = round(time_s - s1 - s2, 3)
    return LapRecord(
        lap_number=number, lap_time_s=time_s,
        sector1_s=s1, sector2_s=s2, sector3_s=s3, invalid=invalid, **kw
    )


class TestEmptyAndInsufficient:
    def test_no_laps_reports_no_data(self):
        result = analyse_laps([])
        assert result.confidence is Confidence.NO_DATA
        assert not result.has_pace
        assert not result.theoretical_available

    def test_one_lap_is_insufficient_but_still_measured(self):
        result = analyse_laps([lap(1, 92.0)])
        assert result.confidence is Confidence.INSUFFICIENT
        assert result.best_lap_s == 92.0

    def test_consistency_needs_two_laps(self):
        """One lap has no spread; 0.0 would imply perfect repeatability."""
        assert analyse_laps([lap(1, 92.0)]).consistency_s == 0.0
        assert analyse_laps([lap(1, 92.0), lap(2, 93.0)]).consistency_s > 0

    def test_confidence_rises_with_valid_laps(self):
        laps = []
        seen = []
        for index in range(1, 10):
            laps.append(lap(index, 92.0 + index * 0.01))
            seen.append(analyse_laps(laps).confidence)
        assert seen[0] is Confidence.INSUFFICIENT
        assert seen[1] is Confidence.LOW
        assert seen[4] is Confidence.MEDIUM
        assert seen[-1] is Confidence.HIGH

    def test_only_usable_confidences_are_flagged_usable(self):
        assert not Confidence.NO_DATA.is_usable
        assert not Confidence.INSUFFICIENT.is_usable
        assert Confidence.LOW.is_usable
        assert Confidence.HIGH.is_usable


class TestInvalidLaps:
    def test_invalid_lap_cannot_set_the_session_best(self):
        """An off-track lap is often the fastest of the session."""
        laps = [lap(1, 92.0), lap(2, 88.0, invalid=True), lap(3, 91.5)]
        result = analyse_laps(laps)

        assert result.best_lap_s == 91.5
        assert result.best_lap_number == 3
        assert result.valid_laps == 2

    def test_invalid_lap_cannot_set_a_sector_best(self):
        laps = [
            lap(1, 92.0, s1=31.0, s2=31.0, s3=30.0),
            lap(2, 88.0, s1=27.0, s2=31.0, s3=30.0, invalid=True),
        ]
        result = analyse_laps(laps)
        assert result.best_sectors[0].time_s == 31.0

    def test_absurd_lap_time_is_excluded(self):
        laps = [lap(1, 92.0), lap(2, 3.0)]
        assert analyse_laps(laps).valid_laps == 1

    def test_last_lap_is_still_reported_when_invalid(self):
        """The driver still wants to see what they just did."""
        result = analyse_laps([lap(1, 92.0), lap(2, 88.0, invalid=True)])
        assert result.last_lap_s == 88.0
        assert result.last_lap_number == 2


class TestTheoreticalBest:
    def test_sums_the_best_sectors(self):
        """The brief's worked example."""
        laps = [
            lap(1, 91.000, s1=30.421, s2=31.500, s3=29.079),
            lap(2, 90.900, s1=30.800, s2=31.102, s3=28.998),
            lap(3, 91.200, s1=30.900, s2=31.400, s3=28.932),
        ]
        result = analyse_laps(laps)

        assert result.best_sectors[0].time_s == pytest.approx(30.421)
        assert result.best_sectors[1].time_s == pytest.approx(31.102)
        assert result.best_sectors[2].time_s == pytest.approx(28.932)
        assert result.theoretical_best_s == pytest.approx(90.455, abs=1e-6)
        assert format_lap_time(result.theoretical_best_s) == "1:30.455"

    def test_needs_all_three_sectors(self):
        """Two sectors out of three is not a lap time."""
        laps = [lap(1, 92.0, s1=31.0, s2=31.0, s3=0.0)]
        result = analyse_laps(laps)

        assert not result.theoretical_available
        assert result.theoretical_best_s == 0.0

    def test_missing_sector_is_not_a_zero_best(self):
        """A 0.000 sector best makes the theoretical best unbeatable."""
        laps = [
            lap(1, 92.0, s1=31.0, s2=31.0, s3=30.0),
            lap(2, 92.0, s1=31.5, s2=0.0, s3=30.5),
        ]
        result = analyse_laps(laps)

        assert result.best_sectors[1].time_s == 31.0
        assert result.theoretical_best_s == pytest.approx(92.0)

    def test_theoretical_is_never_slower_than_the_best_lap(self):
        laps = [
            lap(1, 91.0, s1=30.4, s2=31.5, s3=29.1),
            lap(2, 90.9, s1=30.8, s2=31.1, s3=29.0),
        ]
        result = analyse_laps(laps)
        assert result.theoretical_best_s <= result.best_lap_s

    def test_time_available_is_best_minus_theoretical(self):
        laps = [
            lap(1, 91.000, s1=30.421, s2=31.500, s3=29.079),
            lap(2, 90.900, s1=30.800, s2=31.102, s3=28.998),
        ]
        result = analyse_laps(laps)
        assert result.time_available_s == pytest.approx(
            result.best_lap_s - result.theoretical_best_s
        )
        assert result.time_available_s > 0

    def test_time_available_is_zero_without_a_theoretical(self):
        assert analyse_laps([lap(1, 92.0, s1=31.0, s2=31.0, s3=0.0)]).time_available_s == 0.0


class TestDeltas:
    def test_delta_to_previous_and_best(self):
        laps = [lap(1, 92.000), lap(2, 91.000), lap(3, 91.400)]
        result = analyse_laps(laps)

        assert result.delta_to_previous_s == pytest.approx(0.400)
        assert result.delta_to_best_s == pytest.approx(0.400)
        assert result.previous_lap_s == pytest.approx(91.000)

    def test_improvement_is_negative(self):
        result = analyse_laps([lap(1, 92.000), lap(2, 91.500)])
        assert result.delta_to_previous_s < 0
        assert format_delta(result.delta_to_previous_s) == "-0.500"

    def test_no_previous_delta_on_the_first_lap(self):
        assert analyse_laps([lap(1, 92.0)]).delta_to_previous_s == 0.0

    def test_sector_delta_wording_matches_the_brief(self):
        laps = [
            lap(1, 92.000, s1=30.000, s2=31.000, s3=31.000),
            lap(2, 92.214, s1=30.000, s2=31.214, s3=31.000),
        ]
        result = analyse_laps(laps)

        assert (
            result.sector_deltas[1].describe()
            == "Sector 2: +0.214s from your session best"
        )

    def test_worst_sector_identifies_the_biggest_loss(self):
        laps = [
            lap(1, 92.000, s1=30.000, s2=31.000, s3=31.000),
            lap(2, 92.500, s1=30.100, s2=31.400, s3=31.000),
        ]
        result = analyse_laps(laps)

        worst = result.worst_sector()
        assert worst is not None
        assert worst.sector == 2
        assert worst.delta_s == pytest.approx(0.400)

    def test_no_worst_sector_on_a_perfect_lap(self):
        laps = [
            lap(1, 92.000, s1=31.000, s2=31.000, s3=30.000),
            lap(2, 91.000, s1=30.500, s2=30.500, s3=30.000),
        ]
        assert analyse_laps(laps).worst_sector() is None

    def test_sector_best_on_the_last_lap_is_flagged(self):
        laps = [
            lap(1, 92.000, s1=31.000, s2=31.000, s3=30.000),
            lap(2, 91.500, s1=30.500, s2=31.000, s3=30.000),
        ]
        result = analyse_laps(laps)

        assert result.sector_deltas[0].is_personal_best
        assert result.sector_deltas[0].describe() == "Sector 1: session best"

    def test_describe_losses_skips_unavailable_sectors(self):
        laps = [lap(1, 92.0, s1=31.0, s2=31.0, s3=0.0)]
        described = analyse_laps(laps).describe_losses()
        assert len(described) == 2


class TestFormatting:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0.0, "-"),
            (-1.0, "-"),
            (28.932, "28.932"),
            (90.455, "1:30.455"),
            (106.821, "1:46.821"),
        ],
    )
    def test_lap_time(self, seconds, expected):
        assert format_lap_time(seconds) == expected

    def test_delta_always_signed(self):
        assert format_delta(0.829) == "+0.829"
        assert format_delta(-0.829) == "-0.829"


class TestAveragePace:
    def test_average_uses_valid_laps_only(self):
        laps = [lap(1, 92.0), lap(2, 60.0, invalid=True), lap(3, 94.0)]
        assert analyse_laps(laps).average_lap_s == pytest.approx(93.0)

    def test_consistency_is_the_spread_of_valid_laps(self):
        import statistics

        times = [92.0, 92.5, 93.0, 91.5]
        laps = [lap(i + 1, t) for i, t in enumerate(times)]
        assert analyse_laps(laps).consistency_s == pytest.approx(statistics.stdev(times))
