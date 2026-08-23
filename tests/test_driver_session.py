"""Driver session data collection.

Collection only - these tests check that measurements are faithful and that
the module refuses to overclaim when it has too little data.
"""

from __future__ import annotations

import pytest

from app.core.models import TelemetryFrame
from app.domain.driver_session import DriverSession


def frame(lap=1, last_lap=0.0, **kw):
    base = dict(
        valid=True, session_type="Race", current_lap=lap,
        last_lap_time_s=last_lap, sector1_time_s=28.0, sector2_time_s=31.0,
        tyre_compound="Medium", tyre_age_laps=lap - 1,
        fuel_in_tank=100.0 - lap * 2.0, throttle=0.8, brake=0.0,
    )
    base.update(kw)
    return TelemetryFrame(**base)


class TestLapCollection:
    def test_no_laps_before_the_counter_advances(self):
        session = DriverSession()
        for _ in range(10):
            session.observe(frame(lap=1))
        assert session.laps == []

    def test_lap_closes_when_the_counter_advances(self):
        session = DriverSession()
        session.observe(frame(lap=1))
        record = session.observe(frame(lap=2, last_lap=91.5))

        assert record is not None
        assert record.lap_number == 1
        assert record.lap_time_s == pytest.approx(91.5)

    def test_lap_time_comes_from_the_game_not_our_clock(self):
        """We must never substitute our own stopwatch for the game's."""
        session = DriverSession()
        session.observe(frame(lap=1))
        record = session.observe(frame(lap=2, last_lap=88.123))
        assert record.lap_time_s == pytest.approx(88.123)

    def test_sector3_is_derived_from_the_reported_total(self):
        session = DriverSession()
        session.observe(frame(lap=1, sector1_time_s=28.0, sector2_time_s=31.0))
        record = session.observe(frame(lap=2, last_lap=90.0))
        assert record.sector3_s == pytest.approx(31.0)

    def test_fuel_used_needs_a_previous_lap(self):
        session = DriverSession()
        session.observe(frame(lap=1))
        first = session.observe(frame(lap=2, last_lap=90.0))
        assert first.fuel_used == 0.0  # nothing to compare against yet

        session.observe(frame(lap=2))
        second = session.observe(frame(lap=3, last_lap=90.0))
        assert second.fuel_used > 0

    def test_invalid_laps_are_excluded_from_pace(self):
        session = DriverSession()
        session.observe(frame(lap=1, lap_invalid=True))
        session.observe(frame(lap=2, last_lap=95.0))
        assert session.laps[0].invalid
        assert session.valid_laps == []

    def test_absurd_lap_times_are_excluded(self):
        session = DriverSession()
        session.observe(frame(lap=1))
        session.observe(frame(lap=2, last_lap=5.0))  # impossible
        assert session.valid_laps == []

    def test_session_change_resets(self):
        session = DriverSession()
        session.observe(frame(lap=1, session_type="Practice 1"))
        session.observe(frame(lap=2, last_lap=90.0, session_type="Practice 1"))
        assert len(session.laps) == 1

        session.observe(frame(lap=1, session_type="Race"))
        assert session.laps == []


class TestSummary:
    def _run(self, times):
        session = DriverSession()
        session.observe(frame(lap=1))
        for index, value in enumerate(times, start=2):
            session.observe(frame(lap=index, last_lap=value))
            session.observe(frame(lap=index))
        return session

    def test_best_and_average(self):
        summary = self._run([92.0, 90.0, 91.0]).summary()
        assert summary.best_lap_s == pytest.approx(90.0)
        assert summary.average_lap_s == pytest.approx(91.0)

    def test_consistency_needs_two_laps(self):
        """One lap has no spread; reporting 0.0 would imply perfection."""
        one = self._run([91.0]).summary()
        assert one.valid_laps == 1
        assert one.consistency_s == 0.0
        assert not one.confident

        many = self._run([90.0, 92.0, 91.0]).summary()
        assert many.consistency_s > 0

    def test_confidence_requires_enough_laps(self):
        assert not self._run([90.0]).summary().confident
        assert not self._run([90.0, 91.0]).summary().confident
        assert self._run([90.0, 91.0, 92.0]).summary().confident

    def test_confidence_note_is_honest(self):
        assert "no completed laps" in DriverSession().summary().confidence_note
        assert "not enough" in self._run([90.0]).summary().confidence_note

    def test_degradation_measured_not_assumed(self):
        """Lap time rising with tyre age gives a positive slope."""
        summary = self._run([90.0, 90.5, 91.0, 91.5]).summary()
        assert summary.degradation_s_per_lap > 0

    def test_degradation_zero_without_enough_laps(self):
        assert self._run([90.0]).summary().degradation_s_per_lap == 0.0

    def test_degradation_zero_when_pace_is_flat(self):
        summary = self._run([90.0, 90.0, 90.0, 90.0]).summary()
        assert summary.degradation_s_per_lap == pytest.approx(0.0, abs=1e-6)


class TestBehaviour:
    def test_pedal_ratios(self):
        session = DriverSession()
        for _ in range(10):
            session.observe(frame(throttle=1.0, brake=0.0))
        for _ in range(10):
            session.observe(frame(throttle=0.0, brake=0.9))

        behaviour = session.behaviour
        assert behaviour.samples == 20
        assert behaviour.full_throttle_ratio == pytest.approx(0.5)
        assert behaviour.braking_ratio == pytest.approx(0.5)
        assert behaviour.peak_brake == pytest.approx(0.9)

    def test_pedal_overlap_is_detected(self):
        session = DriverSession()
        for _ in range(5):
            session.observe(frame(throttle=0.4, brake=0.4))
        assert session.behaviour.overlap_ratio == pytest.approx(1.0)

    def test_invalid_frames_are_ignored(self):
        session = DriverSession()
        session.observe(TelemetryFrame(valid=False, throttle=1.0))
        assert session.behaviour.samples == 0
