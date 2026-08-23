"""Driver session data - collection only, no inference.

This is the foundation the coach and strategy engine will eventually sit
on, and the brief is explicit: collect clean data first, no clever AI yet.
So this module measures and records. It does not predict, score, or advise.

Two rules it holds to:

  * Nothing is derived that the game already reports. Lap times, sectors
    and tyre wear come from telemetry, not from integrating our own
    stopwatch.
  * Sample counts travel with every aggregate. A "consistency" figure from
    two laps is not the same claim as one from twenty, and any consumer
    must be able to tell the difference - hence `LapSample.count` and
    `confident`. Nothing here pretends to have enough data.

Laps are closed on the lap counter changing, which is the game telling us a
lap completed - not on our own distance arithmetic.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass

from app.core.models import TelemetryFrame

#: Below this many completed laps, aggregates are reported but flagged as
#: low confidence. Three is the minimum for a meaningful spread.
MIN_LAPS_FOR_CONFIDENCE = 3


@dataclass(slots=True)
class LapRecord:
    """One completed lap, as reported by the game."""

    lap_number: int
    lap_time_s: float = 0.0
    sector1_s: float = 0.0
    sector2_s: float = 0.0
    sector3_s: float = 0.0
    compound: str = ""
    tyre_age_laps: int = -1
    #: Tyre wear at the end of the lap, mean across four corners.
    tyre_wear_pct: float = 0.0
    fuel_remaining: float = 0.0
    #: Fuel burned during this lap, if the previous lap's figure is known.
    fuel_used: float = 0.0
    ers_deployed: float = 0.0
    ers_harvested: float = 0.0
    invalid: bool = False
    position: int = 0
    #: The car was in the pit lane at some point during this lap, so it is
    #: an in-lap or out-lap. Observed across the whole lap, not sampled at
    #: the line - by the time the lap closes the car is back on track.
    pit_lap: bool = False
    #: A safety car or VSC was deployed at some point during this lap.
    #: Depends on the safety-car field being parsed; until then it stays
    #: False rather than being guessed at.
    safety_car_lap: bool = False
    #: Practice / Qualifying / Race ... as the game reported it.
    session_type: str = ""

    @property
    def valid_for_pace(self) -> bool:
        """Whether this lap may set records and define pace.

        An in-lap or out-lap is 20+ seconds off the pace and a safety-car
        lap is slower still; letting either into the pace statistics makes
        the average meaningless and the degradation slope nonsense.
        """
        return (
            not self.invalid
            and not self.pit_lap
            and not self.safety_car_lap
            and 20.0 < self.lap_time_s < 600.0
        )


@dataclass(slots=True)
class DrivingBehaviour:
    """Aggregated pedal and steering behaviour, sampled per frame.

    Descriptive only: how much time was spent on the brakes, how often
    inputs overlapped. No judgement is attached here - that belongs to the
    coach, which does not exist yet.
    """

    samples: int = 0
    throttle_sum: float = 0.0
    brake_sum: float = 0.0
    full_throttle_samples: int = 0
    braking_samples: int = 0
    #: Both pedals at once - trail braking or a mistake, depending.
    overlap_samples: int = 0
    peak_brake: float = 0.0
    steering_abs_sum: float = 0.0

    def observe(self, frame: TelemetryFrame) -> None:
        self.samples += 1
        self.throttle_sum += frame.throttle
        self.brake_sum += frame.brake
        self.steering_abs_sum += abs(frame.steering)
        if frame.throttle >= 0.98:
            self.full_throttle_samples += 1
        if frame.brake > 0.05:
            self.braking_samples += 1
            self.peak_brake = max(self.peak_brake, frame.brake)
        if frame.brake > 0.05 and frame.throttle > 0.05:
            self.overlap_samples += 1

    def _ratio(self, count: int) -> float:
        return count / self.samples if self.samples else 0.0

    @property
    def mean_throttle(self) -> float:
        return self.throttle_sum / self.samples if self.samples else 0.0

    @property
    def mean_brake(self) -> float:
        return self.brake_sum / self.samples if self.samples else 0.0

    @property
    def full_throttle_ratio(self) -> float:
        return self._ratio(self.full_throttle_samples)

    @property
    def braking_ratio(self) -> float:
        return self._ratio(self.braking_samples)

    @property
    def overlap_ratio(self) -> float:
        return self._ratio(self.overlap_samples)

    @property
    def mean_steering(self) -> float:
        return self.steering_abs_sum / self.samples if self.samples else 0.0


@dataclass(slots=True)
class SessionSummary:
    """What we can honestly say about the session so far."""

    laps_completed: int = 0
    valid_laps: int = 0
    best_lap_s: float = 0.0
    average_lap_s: float = 0.0
    #: Standard deviation of valid lap times. Lower = more consistent.
    consistency_s: float = 0.0
    #: Mean lap-time increase per lap of tyre age, from observed laps only.
    degradation_s_per_lap: float = 0.0
    mean_fuel_per_lap: float = 0.0
    compound: str = ""
    tyre_age_laps: int = -1
    tyre_wear_pct: float = 0.0
    duration_s: float = 0.0

    @property
    def confident(self) -> bool:
        """Whether there is enough data to lean on these numbers."""
        return self.valid_laps >= MIN_LAPS_FOR_CONFIDENCE

    @property
    def confidence_note(self) -> str:
        if self.valid_laps == 0:
            return "no completed laps yet"
        if not self.confident:
            return f"only {self.valid_laps} valid lap(s) - not enough to rely on"
        return f"{self.valid_laps} valid laps"


class DriverSession:
    """Collects lap and behaviour data from the normalized frame stream.

    Fed one frame at a time; cheap enough to call at telemetry rate.
    """

    def __init__(self) -> None:
        self.laps: list[LapRecord] = []
        self.behaviour = DrivingBehaviour()
        self.started = time.monotonic()

        self._current_lap: int = 0
        self._last_frame: TelemetryFrame | None = None
        self._last_fuel: float = 0.0
        self._session_type: str = ""
        # Sticky per-lap conditions. Sampling these when the lap closes
        # would miss both: by then the car has left the pits and the safety
        # car may already be in.
        self._lap_saw_pits = False
        self._lap_saw_safety_car = False

    # ------------------------------------------------------------------
    def observe(self, frame: TelemetryFrame) -> LapRecord | None:
        """Consume a frame. Returns a LapRecord when a lap just completed."""
        if not frame.valid:
            return None

        # A new session type means a different session; start clean rather
        # than mixing practice and race data.
        #
        # Only a *named* different session counts. After a telemetry dropout
        # the first frames can arrive before the next Session packet, with
        # session_type still empty - treating that as a new session would
        # throw away every completed lap precisely when telemetry resumes.
        if (
            self._session_type
            and frame.session_type
            and frame.session_type != self._session_type
        ):
            self.reset()
        if frame.session_type:
            self._session_type = frame.session_type

        self.behaviour.observe(frame)

        # Latch conditions as they happen, anywhere in the lap.
        if frame.in_pits:
            self._lap_saw_pits = True
        if frame.safety_car:
            self._lap_saw_safety_car = True

        completed: LapRecord | None = None
        if self._current_lap and frame.current_lap > self._current_lap:
            completed = self._close_lap(self._last_frame or frame, frame)
            self._lap_saw_pits = frame.in_pits
            self._lap_saw_safety_car = bool(frame.safety_car)

        self._current_lap = frame.current_lap or self._current_lap
        self._last_frame = frame
        return completed

    def _close_lap(
        self, last_frame: TelemetryFrame, new_frame: TelemetryFrame
    ) -> LapRecord | None:
        """Record the lap that just finished.

        `last_lap_time_s` on the new frame is the game's own figure for the
        lap just completed, which is why it is preferred over anything we
        could time ourselves.
        """
        lap_time = new_frame.last_lap_time_s or last_frame.last_lap_time_s
        if lap_time <= 0:
            return None

        sector3 = max(0.0, lap_time - last_frame.sector1_time_s - last_frame.sector2_time_s)
        fuel_used = 0.0
        if self._last_fuel and last_frame.fuel_in_tank:
            fuel_used = max(0.0, self._last_fuel - last_frame.fuel_in_tank)

        record = LapRecord(
            lap_number=self._current_lap,
            lap_time_s=lap_time,
            sector1_s=last_frame.sector1_time_s,
            sector2_s=last_frame.sector2_time_s,
            sector3_s=sector3,
            compound=last_frame.tyre_compound,
            tyre_age_laps=last_frame.tyre_age_laps,
            tyre_wear_pct=round(last_frame.tyre_wear.avg, 2),
            fuel_remaining=last_frame.fuel_in_tank,
            fuel_used=fuel_used,
            ers_deployed=last_frame.ers_deployed_lap,
            ers_harvested=last_frame.ers_harvested_lap,
            invalid=last_frame.lap_invalid,
            position=last_frame.position,
            pit_lap=self._lap_saw_pits,
            safety_car_lap=self._lap_saw_safety_car,
            session_type=last_frame.session_type,
        )
        self.laps.append(record)
        self._last_fuel = last_frame.fuel_in_tank
        return record

    def reset(self) -> None:
        self.laps.clear()
        self.behaviour = DrivingBehaviour()
        self.started = time.monotonic()
        self._current_lap = 0
        self._last_frame = None
        self._last_fuel = 0.0
        self._lap_saw_pits = False
        self._lap_saw_safety_car = False

    # ------------------------------------------------------------------
    @property
    def valid_laps(self) -> list[LapRecord]:
        return [lap for lap in self.laps if lap.valid_for_pace]

    def summary(self) -> SessionSummary:
        valid = self.valid_laps
        times = [lap.lap_time_s for lap in valid]
        last = self._last_frame

        summary = SessionSummary(
            laps_completed=len(self.laps),
            valid_laps=len(valid),
            duration_s=time.monotonic() - self.started,
            compound=last.tyre_compound if last else "",
            tyre_age_laps=last.tyre_age_laps if last else -1,
            tyre_wear_pct=round(last.tyre_wear.avg, 2) if last else 0.0,
        )
        if not times:
            return summary

        summary.best_lap_s = min(times)
        summary.average_lap_s = sum(times) / len(times)
        # Standard deviation needs at least two samples; one lap has no
        # spread to speak of and reporting 0.0 would imply perfection.
        summary.consistency_s = statistics.stdev(times) if len(times) > 1 else 0.0

        fuel = [lap.fuel_used for lap in valid if lap.fuel_used > 0]
        summary.mean_fuel_per_lap = sum(fuel) / len(fuel) if fuel else 0.0
        summary.degradation_s_per_lap = self._observed_degradation(valid)
        return summary

    @staticmethod
    def _observed_degradation(valid: list[LapRecord]) -> float:
        """Lap-time trend against tyre age, measured not assumed.

        A plain least-squares slope over (tyre age, lap time). Needs at
        least three laps with real age data and some spread in age, or it
        returns 0.0 rather than a number invented from one point.
        """
        points = [
            (float(lap.tyre_age_laps), lap.lap_time_s)
            for lap in valid
            if lap.tyre_age_laps >= 0
        ]
        if len(points) < MIN_LAPS_FOR_CONFIDENCE:
            return 0.0

        ages = [age for age, _ in points]
        if max(ages) == min(ages):
            return 0.0  # no spread: nothing to regress against

        mean_age = sum(ages) / len(ages)
        mean_time = sum(t for _, t in points) / len(points)
        numerator = sum((a - mean_age) * (t - mean_time) for a, t in points)
        denominator = sum((a - mean_age) ** 2 for a in ages)
        return round(numerator / denominator, 4) if denominator else 0.0
