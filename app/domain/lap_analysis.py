"""Lap and sector analysis - measurement only, no advice.

Phase B of the race-engineer build. The brief is explicit that the
measurements must be reliable before anything recommends action, so this
module answers "what happened" and never "what should I do". The coach and
strategy engine consume it later.

Everything here is a pure function of the `LapRecord` list the
`DriverSession` already collects. No telemetry is re-derived and no clock
of our own is used: lap and sector times come from the game.

Two rules shape the whole module:

  * Only valid laps set records. An off-track lap is often the fastest
    thing you'll do all session, and letting it define the session best
    would poison every delta computed against it.
  * A missing sector is missing, not zero. Sector times arrive as the lap
    progresses, so a partially-reported lap has genuine gaps in it. Those
    are excluded rather than treated as a 0.000 best, which would make the
    theoretical best absurd and permanently unbeatable.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from enum import Enum

from app.domain.driver_session import LapRecord

#: A lap under this is not a real flying lap (pit exit, reset, telemetry
#: glitch). Kept generous - the shortest real F1 lap is a little over a
#: minute, but Time Trial on short layouts can go lower.
MIN_REAL_LAP_S = 20.0
#: Sector times below this are treated as not yet reported.
MIN_REAL_SECTOR_S = 1.0

#: Valid-lap counts at which each confidence level is reached.
LAPS_FOR_LOW = 2
LAPS_FOR_MEDIUM = 4
LAPS_FOR_HIGH = 8


#: A lap this much slower than the median of the clean laps is treated as
#: an outlier - a spin, traffic, a lift. Generous on purpose: the point is
#: to exclude laps that are obviously not representative, not to trim the
#: distribution until it looks tidy.
OUTLIER_FACTOR = 1.07
#: Outlier detection needs a median worth trusting.
MIN_LAPS_FOR_OUTLIERS = 5


class LapCategory(str, Enum):
    """Why a lap does or does not count towards pace.

    Kept separate from a simple valid/invalid flag because the reasons need
    different handling later: a pit lap is fine data for stint tracking but
    useless for pace, while an invalid lap is useless for both.
    """

    CLEAN = "CLEAN"
    INVALID = "INVALID"
    PIT = "PIT LAP"
    SAFETY_CAR = "SAFETY CAR"
    FORMATION = "FORMATION"
    OUTLIER = "OUTLIER"

    @property
    def counts_for_pace(self) -> bool:
        return self is LapCategory.CLEAN


class Confidence(str, Enum):
    """How much weight the numbers below deserve.

    Shared vocabulary across every model in the app, so a consumer never
    has to guess whether "0.064" came from three laps or thirty.
    """

    NO_DATA = "NO DATA"
    INSUFFICIENT = "INSUFFICIENT DATA"
    LOW = "LOW CONFIDENCE"
    MEDIUM = "MEDIUM CONFIDENCE"
    HIGH = "HIGH CONFIDENCE"

    @property
    def is_usable(self) -> bool:
        """True when a number may be shown as a figure rather than a dash."""
        return self in (Confidence.LOW, Confidence.MEDIUM, Confidence.HIGH)

    @classmethod
    def from_samples(cls, samples: int) -> "Confidence":
        if samples <= 0:
            return cls.NO_DATA
        if samples < LAPS_FOR_LOW:
            return cls.INSUFFICIENT
        if samples < LAPS_FOR_MEDIUM:
            return cls.LOW
        if samples < LAPS_FOR_HIGH:
            return cls.MEDIUM
        return cls.HIGH


def format_lap_time(seconds: float) -> str:
    """m:ss.mmm, or a dash when there is no time to show."""
    if seconds <= 0:
        return "-"
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    return f"{minutes}:{remainder:06.3f}" if minutes else f"{remainder:.3f}"


def format_delta(seconds: float) -> str:
    """Signed delta, where negative is an improvement."""
    return f"{seconds:+.3f}"


@dataclass(frozen=True, slots=True)
class SectorBest:
    """The best time seen in one sector, and which lap set it."""

    sector: int  # 1-based
    time_s: float = 0.0
    lap_number: int = 0

    @property
    def available(self) -> bool:
        return self.time_s >= MIN_REAL_SECTOR_S


@dataclass(frozen=True, slots=True)
class SectorDelta:
    """One sector of a lap measured against the session best for it."""

    sector: int
    time_s: float
    best_s: float
    delta_s: float
    is_personal_best: bool

    @property
    def available(self) -> bool:
        return self.time_s >= MIN_REAL_SECTOR_S and self.best_s >= MIN_REAL_SECTOR_S

    def describe(self) -> str:
        if not self.available:
            return f"Sector {self.sector}: no data"
        if self.is_personal_best:
            return f"Sector {self.sector}: session best"
        return f"Sector {self.sector}: {format_delta(self.delta_s)}s from your session best"


@dataclass(slots=True)
class LapAnalysis:
    """The measured picture of a session's pace."""

    laps_recorded: int = 0
    valid_laps: int = 0
    confidence: Confidence = Confidence.NO_DATA

    #: Category per recorded lap, in order.
    categories: tuple[LapCategory, ...] = ()
    last_lap_category: LapCategory = LapCategory.CLEAN
    #: Which categories were excluded from pace, for display. Says *why*
    #: the valid count is lower than the recorded count.
    excluded: tuple[LapCategory, ...] = ()

    best_lap_s: float = 0.0
    best_lap_number: int = 0
    last_lap_s: float = 0.0
    last_lap_number: int = 0
    previous_lap_s: float = 0.0

    average_lap_s: float = 0.0
    #: Standard deviation of valid lap times - lower is more repeatable.
    consistency_s: float = 0.0

    best_sectors: tuple[SectorBest, SectorBest, SectorBest] = field(
        default_factory=lambda: (SectorBest(1), SectorBest(2), SectorBest(3))
    )
    #: Sum of the best sectors. Only meaningful when all three exist.
    theoretical_best_s: float = 0.0

    #: Last lap measured sector by sector against the session bests.
    sector_deltas: tuple[SectorDelta, ...] = ()

    delta_to_best_s: float = 0.0
    delta_to_previous_s: float = 0.0
    delta_to_theoretical_s: float = 0.0

    @property
    def theoretical_available(self) -> bool:
        return self.theoretical_best_s >= MIN_REAL_LAP_S

    @property
    def has_pace(self) -> bool:
        return self.valid_laps > 0 and self.best_lap_s > 0

    @property
    def time_available_s(self) -> float:
        """How much is on the table: session best minus theoretical best.

        This is time the driver has already shown they can do, just not all
        on the same lap - which is why it is worth stating separately from
        any coaching.
        """
        if not (self.theoretical_available and self.best_lap_s > 0):
            return 0.0
        return max(0.0, self.best_lap_s - self.theoretical_best_s)

    def worst_sector(self) -> SectorDelta | None:
        """Where the last lap lost the most time, if anywhere."""
        losses = [d for d in self.sector_deltas if d.available and d.delta_s > 0]
        return max(losses, key=lambda d: d.delta_s) if losses else None

    def describe_losses(self) -> list[str]:
        """Per-sector statements about the last lap. Measurement, not advice."""
        return [delta.describe() for delta in self.sector_deltas if delta.available]


def _sector_times(lap: LapRecord) -> tuple[float, float, float]:
    return (lap.sector1_s, lap.sector2_s, lap.sector3_s)


def _base_category(lap: LapRecord) -> LapCategory:
    """Classify a lap from what the game reported about it.

    Order matters: a lap can be several of these at once, and the most
    disqualifying reason is the one worth showing. An invalid pit lap is
    reported as invalid because that is the reason it can never count.
    """
    if lap.lap_time_s <= 0 or lap.lap_time_s < MIN_REAL_LAP_S:
        return LapCategory.INVALID
    if lap.invalid:
        return LapCategory.INVALID
    if lap.pit_lap:
        return LapCategory.PIT
    if lap.safety_car_lap:
        return LapCategory.SAFETY_CAR
    # Formation laps are numbered 0 by the game when they are reported at
    # all. Most F1 sessions start the counter at 1 and never emit one, so
    # this is best-effort rather than a guarantee.
    if lap.lap_number == 0:
        return LapCategory.FORMATION
    return LapCategory.CLEAN


def classify_laps(laps: list[LapRecord]) -> list[LapCategory]:
    """Categorise every lap, including outliers.

    Outliers can only be judged against the others, so this is a whole-list
    operation rather than a property of a lap. The median of the otherwise
    clean laps is the reference: a mean would be dragged upwards by the
    very laps being looked for.
    """
    categories = [_base_category(lap) for lap in laps]

    clean_times = [
        lap.lap_time_s
        for lap, category in zip(laps, categories)
        if category is LapCategory.CLEAN
    ]
    if len(clean_times) < MIN_LAPS_FOR_OUTLIERS:
        return categories

    threshold = statistics.median(clean_times) * OUTLIER_FACTOR
    return [
        LapCategory.OUTLIER
        if category is LapCategory.CLEAN and lap.lap_time_s > threshold
        else category
        for lap, category in zip(laps, categories)
    ]


def analyse_laps(laps: list[LapRecord]) -> LapAnalysis:
    """Measure a session's pace from the laps recorded so far.

    Pure and cheap: called on lap completion, not per telemetry frame.
    """
    analysis = LapAnalysis(laps_recorded=len(laps))
    if not laps:
        return analysis

    analysis.last_lap_number = laps[-1].lap_number
    analysis.last_lap_s = laps[-1].lap_time_s

    categories = classify_laps(laps)
    analysis.categories = tuple(categories)
    analysis.last_lap_category = categories[-1]
    analysis.excluded = tuple(
        sorted(
            {c for c in categories if c is not LapCategory.CLEAN},
            key=lambda c: c.value,
        )
    )

    valid = [
        lap
        for lap, category in zip(laps, categories)
        if category.counts_for_pace
    ]
    analysis.valid_laps = len(valid)
    analysis.confidence = Confidence.from_samples(len(valid))
    if not valid:
        return analysis

    times = [lap.lap_time_s for lap in valid]
    best = min(valid, key=lambda lap: lap.lap_time_s)
    analysis.best_lap_s = best.lap_time_s
    analysis.best_lap_number = best.lap_number
    analysis.average_lap_s = sum(times) / len(times)
    # One lap has no spread; reporting 0.0 would imply perfect repeatability.
    analysis.consistency_s = statistics.stdev(times) if len(times) > 1 else 0.0

    # --- best sectors, from valid laps only --------------------------------
    bests: list[SectorBest] = []
    for index in range(3):
        candidates = [
            (_sector_times(lap)[index], lap.lap_number)
            for lap in valid
            if _sector_times(lap)[index] >= MIN_REAL_SECTOR_S
        ]
        if candidates:
            time_s, lap_number = min(candidates)
            bests.append(SectorBest(index + 1, time_s, lap_number))
        else:
            bests.append(SectorBest(index + 1))
    analysis.best_sectors = (bests[0], bests[1], bests[2])

    # Theoretical best needs all three: two-thirds of a lap is not a lap.
    if all(sector.available for sector in bests):
        analysis.theoretical_best_s = sum(sector.time_s for sector in bests)

    # --- the last lap, against those bests ---------------------------------
    last = laps[-1]
    deltas: list[SectorDelta] = []
    for index, sector_best in enumerate(bests):
        time_s = _sector_times(last)[index]
        deltas.append(
            SectorDelta(
                sector=index + 1,
                time_s=time_s,
                best_s=sector_best.time_s,
                delta_s=time_s - sector_best.time_s if sector_best.available else 0.0,
                is_personal_best=(
                    sector_best.available
                    and time_s >= MIN_REAL_SECTOR_S
                    and sector_best.lap_number == last.lap_number
                ),
            )
        )
    analysis.sector_deltas = tuple(deltas)

    if last.lap_time_s >= MIN_REAL_LAP_S:
        analysis.delta_to_best_s = last.lap_time_s - analysis.best_lap_s
        if analysis.theoretical_available:
            analysis.delta_to_theoretical_s = (
                last.lap_time_s - analysis.theoretical_best_s
            )

    # Previous lap: the one before the last, valid or not - the driver wants
    # to know what just changed, not what the record book says.
    if len(laps) >= 2 and laps[-2].lap_time_s >= MIN_REAL_LAP_S:
        analysis.previous_lap_s = laps[-2].lap_time_s
        if last.lap_time_s >= MIN_REAL_LAP_S:
            analysis.delta_to_previous_s = last.lap_time_s - analysis.previous_lap_s

    return analysis
