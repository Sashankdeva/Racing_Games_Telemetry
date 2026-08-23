"""Phase 1 completion: laps that must not count towards pace.

A lap time on its own says nothing about whether it belongs in the pace
statistics. An in-lap is 20+ seconds slow, a safety-car lap slower still,
and a spin looks like neither. Letting any of them through corrupts the
average, the consistency figure and - worst of all - the degradation slope
that Phase 2 will fit through these points.

Pit laps stay in the record because they are real laps and the stint model
needs them; they are excluded from *pace* only.
"""

from __future__ import annotations

import pytest

from app.core.models import TelemetryFrame
from app.domain.driver_session import DriverSession, LapRecord
from app.domain.lap_analysis import (
    MIN_LAPS_FOR_OUTLIERS,
    LapCategory,
    analyse_laps,
    classify_laps,
)


def lap(number, time_s, **kw):
    s1 = kw.pop("s1", round(time_s * 0.33, 3))
    s2 = kw.pop("s2", round(time_s * 0.34, 3))
    s3 = kw.pop("s3", round(time_s - s1 - s2, 3))
    return LapRecord(
        lap_number=number, lap_time_s=time_s,
        sector1_s=s1, sector2_s=s2, sector3_s=s3, **kw
    )


class TestCategories:
    def test_a_normal_lap_is_clean(self):
        assert classify_laps([lap(1, 92.0)]) == [LapCategory.CLEAN]

    def test_invalid_lap(self):
        assert classify_laps([lap(1, 92.0, invalid=True)]) == [LapCategory.INVALID]

    def test_pit_lap(self):
        assert classify_laps([lap(1, 112.0, pit_lap=True)]) == [LapCategory.PIT]

    def test_safety_car_lap(self):
        assert classify_laps([lap(1, 120.0, safety_car_lap=True)]) == [
            LapCategory.SAFETY_CAR
        ]

    def test_formation_lap_is_lap_zero(self):
        assert classify_laps([lap(0, 130.0)]) == [LapCategory.FORMATION]

    def test_absurdly_short_lap_is_invalid(self):
        assert classify_laps([lap(1, 3.0)]) == [LapCategory.INVALID]

    def test_invalid_wins_over_pit(self):
        """The most disqualifying reason is the one worth reporting."""
        result = classify_laps([lap(1, 112.0, invalid=True, pit_lap=True)])
        assert result == [LapCategory.INVALID]

    def test_only_clean_counts_for_pace(self):
        assert LapCategory.CLEAN.counts_for_pace
        for category in (
            LapCategory.INVALID, LapCategory.PIT,
            LapCategory.SAFETY_CAR, LapCategory.FORMATION, LapCategory.OUTLIER,
        ):
            assert not category.counts_for_pace


class TestOutliers:
    def _steady(self, count, base=92.0):
        return [lap(i + 1, base + (i % 3) * 0.05) for i in range(count)]

    def test_needs_enough_laps_to_judge(self):
        """With few laps there is no median worth comparing against.

        The slow lap counts towards the total, so this builds one fewer
        steady lap than the threshold to stay below it.
        """
        laps = self._steady(MIN_LAPS_FOR_OUTLIERS - 2) + [lap(99, 130.0)]
        assert len(laps) < MIN_LAPS_FOR_OUTLIERS
        assert LapCategory.OUTLIER not in classify_laps(laps)

    def test_judges_as_soon_as_the_threshold_is_met(self):
        laps = self._steady(MIN_LAPS_FOR_OUTLIERS - 1) + [lap(99, 130.0)]
        assert len(laps) == MIN_LAPS_FOR_OUTLIERS
        assert classify_laps(laps)[-1] is LapCategory.OUTLIER

    def test_obvious_outlier_is_flagged(self):
        laps = self._steady(MIN_LAPS_FOR_OUTLIERS) + [lap(99, 130.0)]
        categories = classify_laps(laps)
        assert categories[-1] is LapCategory.OUTLIER

    def test_normal_variation_is_not_an_outlier(self):
        laps = self._steady(10)
        assert set(classify_laps(laps)) == {LapCategory.CLEAN}

    def test_a_fast_lap_is_never_an_outlier(self):
        """Only slow laps are excluded; a genuinely quick lap is the point."""
        laps = self._steady(MIN_LAPS_FOR_OUTLIERS) + [lap(99, 85.0)]
        assert classify_laps(laps)[-1] is LapCategory.CLEAN

    def test_outliers_are_measured_against_the_median(self):
        """A mean would be dragged up by the very laps being looked for."""
        laps = [lap(i + 1, 92.0) for i in range(6)]
        laps += [lap(7, 140.0), lap(8, 145.0)]  # two disasters
        categories = classify_laps(laps)
        assert categories[:6] == [LapCategory.CLEAN] * 6
        assert categories[6:] == [LapCategory.OUTLIER] * 2


class TestPaceExcludesNonRepresentativeLaps:
    def test_pit_lap_does_not_set_the_best(self):
        laps = [lap(1, 92.0), lap(2, 91.0, pit_lap=True)]
        result = analyse_laps(laps)
        assert result.best_lap_s == 92.0
        assert result.valid_laps == 1

    def test_safety_car_lap_does_not_pollute_the_average(self):
        laps = [lap(1, 92.0), lap(2, 92.0), lap(3, 140.0, safety_car_lap=True)]
        result = analyse_laps(laps)
        assert result.average_lap_s == pytest.approx(92.0)

    def test_pit_lap_does_not_set_a_sector_best(self):
        laps = [
            lap(1, 92.0, s1=31.0, s2=31.0, s3=30.0),
            lap(2, 112.0, s1=25.0, s2=31.0, s3=56.0, pit_lap=True),
        ]
        assert analyse_laps(laps).best_sectors[0].time_s == 31.0

    def test_excluded_reasons_are_reported(self):
        laps = [
            lap(1, 92.0),
            lap(2, 112.0, pit_lap=True),
            lap(3, 91.0, invalid=True),
        ]
        result = analyse_laps(laps)
        assert LapCategory.PIT in result.excluded
        assert LapCategory.INVALID in result.excluded
        assert result.laps_recorded == 3
        assert result.valid_laps == 1

    def test_categories_align_with_the_laps(self):
        laps = [lap(1, 92.0), lap(2, 112.0, pit_lap=True), lap(3, 92.5)]
        result = analyse_laps(laps)
        assert len(result.categories) == len(laps)
        assert result.categories[1] is LapCategory.PIT
        assert result.last_lap_category is LapCategory.CLEAN

    def test_all_laps_excluded_leaves_no_pace(self):
        laps = [lap(1, 112.0, pit_lap=True), lap(2, 91.0, invalid=True)]
        result = analyse_laps(laps)
        assert not result.has_pace
        assert result.best_lap_s == 0.0


class TestSessionCapturesConditions:
    """The flags must be latched across the lap, not sampled at the line."""

    def _frame(self, lap_num, **kw):
        return TelemetryFrame(valid=True, game="f1", current_lap=lap_num, **kw)

    def test_pit_lap_detected_mid_lap(self):
        """By the time the lap closes the car has left the pits."""
        session = DriverSession()
        session.observe(self._frame(1))
        session.observe(self._frame(1, in_pits=True))  # mid-lap pit visit
        session.observe(self._frame(1))
        record = session.observe(self._frame(2, last_lap_time_s=112.0))

        assert record is not None
        assert record.pit_lap
        assert not record.valid_for_pace

    def test_clean_lap_is_not_marked(self):
        session = DriverSession()
        session.observe(self._frame(1))
        record = session.observe(self._frame(2, last_lap_time_s=92.0))
        assert not record.pit_lap
        assert record.valid_for_pace

    def test_flags_do_not_leak_into_the_next_lap(self):
        session = DriverSession()
        session.observe(self._frame(1, in_pits=True))
        session.observe(self._frame(2, last_lap_time_s=112.0))
        session.observe(self._frame(2))
        second = session.observe(self._frame(3, last_lap_time_s=92.0))

        assert second is not None
        assert not second.pit_lap, "pit flag leaked into the following lap"

    def test_safety_car_latched_when_the_field_is_populated(self):
        session = DriverSession()
        session.observe(self._frame(1))
        session.observe(self._frame(1, safety_car="Safety Car"))
        record = session.observe(self._frame(2, last_lap_time_s=120.0))

        assert record.safety_car_lap
        assert not record.valid_for_pace

    def test_session_type_is_recorded(self):
        session = DriverSession()
        session.observe(self._frame(1, session_type="Race"))
        record = session.observe(self._frame(2, last_lap_time_s=92.0, session_type="Race"))
        assert record.session_type == "Race"

    def test_reset_clears_latched_conditions(self):
        session = DriverSession()
        session.observe(self._frame(1, in_pits=True))
        session.reset()
        session.observe(self._frame(1))
        record = session.observe(self._frame(2, last_lap_time_s=92.0))
        assert not record.pit_lap


class TestSafetyCarIsNotFabricated:
    """`safety_car` is declared on the frame but no parser populates it yet.

    The plumbing is in place, but until Phase 3 parses the field it must
    stay empty rather than being inferred from lap times.
    """

    def test_no_safety_car_reported_without_the_field(self):
        session = DriverSession()
        session.observe(TelemetryFrame(valid=True, game="f1", current_lap=1))
        record = session.observe(
            TelemetryFrame(valid=True, game="f1", current_lap=2, last_lap_time_s=140.0)
        )
        # Slow, but nothing claims to know why.
        assert not record.safety_car_lap
