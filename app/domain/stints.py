"""Tyre and stint intelligence.

Phase 2. A stint is the run between tyre changes, and it is the unit that
actually matters for strategy: degradation is a property of a set of tyres,
not of a session. A session-wide slope mixes compounds and fresh-tyre
resets together and produces a number that describes nothing.

Stint boundaries come from the game's own signals - the compound changing,
or the tyre age counter resetting - never from guessing at lap times.

Degradation is a least-squares slope of lap time against tyre age, fitted
over the clean laps of one stint. Three deliberate restrictions:

  * Only clean laps. An in-lap, a safety-car lap or a spin sits far off the
    line and would tilt the slope hard. Phase 1 already classifies these.
  * Enough laps, with enough spread in age. Two points always fit a line
    perfectly and say nothing.
  * A confidence level travels with every number, so a consumer can tell a
    slope from four laps apart from one from twenty.

When those are not met the answer is INSUFFICIENT DATA. It is never a
number with a shrug attached.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.driver_session import LapRecord
from app.domain.lap_analysis import Confidence, LapCategory, classify_laps

#: Clean laps needed before a degradation slope is reported at all.
MIN_LAPS_FOR_DEGRADATION = 4
#: The tyre must have aged by at least this much across the fitted laps.
#: Without spread there is nothing to regress against.
MIN_AGE_SPREAD = 3
#: Clean-lap counts at which degradation confidence rises.
LAPS_FOR_MEDIUM_DEG = 6
LAPS_FOR_HIGH_DEG = 10


@dataclass(slots=True)
class Stint:
    """One run on one set of tyres."""

    number: int
    compound: str = ""
    #: Tyre age at the first lap of the stint. Non-zero for a used set.
    start_age_laps: int = -1
    first_lap: int = 0
    last_lap: int = 0
    laps: list[LapRecord] = field(default_factory=list)
    #: Categories for `laps`, same order.
    categories: list[LapCategory] = field(default_factory=list)

    # --- measured, filled in by analyse_stint ---
    degradation_s_per_lap: float = 0.0
    degradation_confidence: Confidence = Confidence.NO_DATA
    best_lap_s: float = 0.0
    average_lap_s: float = 0.0
    clean_laps: int = 0

    @property
    def length(self) -> int:
        """Laps completed on this set, including pit and invalid laps."""
        return len(self.laps)

    @property
    def current_age_laps(self) -> int:
        """Tyre age at the end of the stint, as the game reported it."""
        for lap in reversed(self.laps):
            if lap.tyre_age_laps >= 0:
                return lap.tyre_age_laps
        return -1

    @property
    def wear_pct(self) -> float:
        for lap in reversed(self.laps):
            if lap.tyre_wear_pct > 0:
                return lap.tyre_wear_pct
        return 0.0

    @property
    def started_used(self) -> bool:
        """Whether this stint began on a scrubbed set.

        The threshold is deliberately above 1. `start_age_laps` is read from
        the stint's first *completed* lap, and a brand-new set has already
        done a lap by then - so age 1 is a fresh set, not a used one.
        Claiming otherwise would mislabel every new set fitted.
        """
        return self.start_age_laps > 1

    @property
    def has_degradation(self) -> bool:
        return self.degradation_confidence.is_usable

    def describe_degradation(self) -> str:
        """The figure, or an explicit statement that there isn't one."""
        if not self.has_degradation:
            return Confidence.INSUFFICIENT.value
        return f"{self.degradation_s_per_lap:+.3f}s/lap"

    def label(self) -> str:
        """e.g. 'Medium  L1-18'."""
        compound = self.compound or "Unknown"
        if self.first_lap and self.last_lap:
            return f"{compound}  L{self.first_lap}-{self.last_lap}"
        return compound


def _degradation_confidence(clean_laps: int) -> Confidence:
    if clean_laps < MIN_LAPS_FOR_DEGRADATION:
        return Confidence.INSUFFICIENT
    if clean_laps < LAPS_FOR_MEDIUM_DEG:
        return Confidence.LOW
    if clean_laps < LAPS_FOR_HIGH_DEG:
        return Confidence.MEDIUM
    return Confidence.HIGH


def _slope(points: list[tuple[float, float]]) -> float:
    """Least-squares slope of y against x."""
    mean_x = sum(x for x, _ in points) / len(points)
    mean_y = sum(y for _, y in points) / len(points)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in points)
    denominator = sum((x - mean_x) ** 2 for x, _ in points)
    return numerator / denominator if denominator else 0.0


def analyse_stint(stint: Stint) -> Stint:
    """Fill in the measured fields. Mutates and returns the stint."""
    clean = [
        lap
        for lap, category in zip(stint.laps, stint.categories)
        if category.counts_for_pace
    ]
    stint.clean_laps = len(clean)

    if clean:
        times = [lap.lap_time_s for lap in clean]
        stint.best_lap_s = min(times)
        stint.average_lap_s = sum(times) / len(times)

    stint.degradation_s_per_lap = 0.0
    stint.degradation_confidence = _degradation_confidence(len(clean))
    if not stint.degradation_confidence.is_usable:
        return stint

    # Age is the x axis, not lap number: a set that started used degrades
    # from where it already was, and only real age explains the pace.
    points = [
        (float(lap.tyre_age_laps), lap.lap_time_s)
        for lap in clean
        if lap.tyre_age_laps >= 0
    ]
    if len(points) < MIN_LAPS_FOR_DEGRADATION:
        stint.degradation_confidence = Confidence.INSUFFICIENT
        return stint

    ages = [age for age, _ in points]
    if max(ages) - min(ages) < MIN_AGE_SPREAD:
        # Fitting a line through a cluster of identical ages produces a
        # slope with no meaning.
        stint.degradation_confidence = Confidence.INSUFFICIENT
        return stint

    stint.degradation_s_per_lap = round(_slope(points), 4)
    return stint


def build_stints(laps: list[LapRecord]) -> list[Stint]:
    """Split a session's laps into stints and measure each.

    A new stint starts when the compound changes, or when the tyre age
    counter goes backwards - both are the game telling us the tyres were
    changed. Lap times are never used to infer a pit stop.
    """
    if not laps:
        return []

    categories = classify_laps(laps)
    stints: list[Stint] = []
    current: Stint | None = None
    previous_age = -1
    previous_compound = ""

    for lap, category in zip(laps, categories):
        fresh_compound = (
            bool(lap.compound) and bool(previous_compound)
            and lap.compound != previous_compound
        )
        # Age going backwards means a different set of tyres.
        fresh_age = (
            lap.tyre_age_laps >= 0
            and previous_age >= 0
            and lap.tyre_age_laps < previous_age
        )

        if current is None or fresh_compound or fresh_age:
            current = Stint(
                number=len(stints) + 1,
                compound=lap.compound,
                start_age_laps=lap.tyre_age_laps,
                first_lap=lap.lap_number,
            )
            stints.append(current)

        current.laps.append(lap)
        current.categories.append(category)
        current.last_lap = lap.lap_number
        if not current.compound and lap.compound:
            current.compound = lap.compound

        if lap.tyre_age_laps >= 0:
            previous_age = lap.tyre_age_laps
        if lap.compound:
            previous_compound = lap.compound

    return [analyse_stint(stint) for stint in stints]


@dataclass(slots=True)
class TyreState:
    """The live picture of the tyres, for the dashboard.

    Everything here is either straight from telemetry or measured from
    completed laps. Nothing is projected forward - that is the strategy
    engine's job, once it exists.
    """

    compound: str = ""
    age_laps: int = -1
    wear_pct: float = 0.0
    stint_number: int = 0
    stint_laps: int = 0
    degradation_s_per_lap: float = 0.0
    degradation_confidence: Confidence = Confidence.NO_DATA

    @property
    def available(self) -> bool:
        return bool(self.compound) or self.age_laps >= 0

    def describe_degradation(self) -> str:
        if not self.degradation_confidence.is_usable:
            return Confidence.INSUFFICIENT.value
        return f"{self.degradation_s_per_lap:+.3f}s/lap"


def current_tyre_state(stints: list[Stint]) -> TyreState:
    """Summarise the stint in progress."""
    if not stints:
        return TyreState()
    stint = stints[-1]
    return TyreState(
        compound=stint.compound,
        age_laps=stint.current_age_laps,
        wear_pct=stint.wear_pct,
        stint_number=stint.number,
        stint_laps=stint.length,
        degradation_s_per_lap=stint.degradation_s_per_lap,
        degradation_confidence=stint.degradation_confidence,
    )
