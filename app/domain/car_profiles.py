"""Car database - editable performance profiles for the F1 grid.

IMPORTANT: every rating here is a **prior**, not a fact. They exist so the
strategy engine has something reasonable to say before it has seen you
drive. Once enough real telemetry has been observed for a session, measured
values must take precedence - the priors are the starting point of a belief,
not a ceiling on it.

`Rating.confidence` carries that distinction explicitly: a prior starts at
low confidence and only rises as observations accumulate (Phase 7). Any
consumer that blends prior with observed data should weight by confidence
rather than treating both as equal.

Ratings are on a 0-100 scale where 50 is midfield. They are deliberately
coarse: the point is relative ordering between cars, not false precision.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from app.core.paths import data_dir
from app.games.modes import GameMode
from app.domain.store import RecordStore, dataclass_from_dict

#: Confidence assigned to a shipped prior with no observations behind it.
PRIOR_CONFIDENCE = 0.25
#: Confidence for a car whose performance is genuinely unknown - a new
#: regulation set nobody has data for yet. Deliberately near zero so any
#: consumer weighting by confidence effectively ignores it.
UNKNOWN_CONFIDENCE = 0.05


@dataclass(slots=True)
class CarProfile:
    car_id: str = "generic"
    name: str = "Generic Car"
    team: str = ""
    year: int = 2025

    # --- performance priors, 0-100 (50 = midfield) ---------------------
    overall: float = 50.0
    qualifying_pace: float = 50.0
    race_pace: float = 50.0
    straight_line: float = 50.0
    cornering: float = 50.0
    braking: float = 50.0
    traction: float = 50.0
    #: Higher = kinder to its tyres over a stint.
    tyre_management: float = 50.0
    #: Higher = degrades FASTER. Named to match the brief; read carefully.
    tyre_degradation: float = 50.0
    fuel_efficiency: float = 50.0
    ers_efficiency: float = 50.0

    #: How much to trust these numbers, 0-1. Shipped priors stay low until
    #: real observations back them up.
    confidence: float = PRIOR_CONFIDENCE
    notes: str = ""

    @property
    def is_prior(self) -> bool:
        return self.confidence <= PRIOR_CONFIDENCE

    def rating(self, name: str) -> float:
        return float(getattr(self, name, 50.0))

    def clamped(self) -> "CarProfile":
        for f in (
            "overall", "qualifying_pace", "race_pace", "straight_line",
            "cornering", "braking", "traction", "tyre_management",
            "tyre_degradation", "fuel_efficiency", "ers_efficiency",
        ):
            setattr(self, f, max(0.0, min(100.0, float(getattr(self, f)))))
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        return self


#: Rating fields, in the order the UI shows them.
RATING_FIELDS: tuple[tuple[str, str], ...] = (
    ("overall", "Overall"),
    ("qualifying_pace", "Qualifying pace"),
    ("race_pace", "Race pace"),
    ("straight_line", "Straight line"),
    ("cornering", "Cornering"),
    ("braking", "Braking"),
    ("traction", "Traction"),
    ("tyre_management", "Tyre management"),
    ("tyre_degradation", "Tyre degradation (higher = worse)"),
    ("fuel_efficiency", "Fuel efficiency"),
    ("ers_efficiency", "ERS efficiency"),
)


def _car(car_id, name, team, ratings: dict) -> CarProfile:
    profile = CarProfile(car_id=car_id, name=name, team=team)
    for key, value in ratings.items():
        setattr(profile, key, float(value))
    return profile.clamped()


def builtin_cars(mode: GameMode = GameMode.F1_25) -> list[CarProfile]:
    """Shipped car database for a game mode.

    The two grids are genuinely different - different teams, different
    regulations - not the same list relabelled.
    """
    return _f1_26_cars() if mode is GameMode.F1_26 else _f1_25_cars()


def _f1_25_cars() -> list[CarProfile]:
    """The 2025 grid.

    Informed starting estimates, not measured data - the whole reason
    `confidence` defaults low. Users are expected to edit them, and the
    learning layer will refine them from observed pace.
    """
    return [
        _car("mclaren", "MCL39", "McLaren", dict(
            overall=93, qualifying_pace=92, race_pace=94, straight_line=85,
            cornering=94, braking=90, traction=92, tyre_management=93,
            tyre_degradation=32, fuel_efficiency=78, ers_efficiency=82)),
        _car("red_bull", "RB21", "Red Bull Racing", dict(
            overall=88, qualifying_pace=90, race_pace=86, straight_line=90,
            cornering=88, braking=87, traction=85, tyre_management=76,
            tyre_degradation=48, fuel_efficiency=80, ers_efficiency=84)),
        _car("ferrari", "SF-25", "Ferrari", dict(
            overall=87, qualifying_pace=87, race_pace=87, straight_line=86,
            cornering=87, braking=86, traction=84, tyre_management=82,
            tyre_degradation=42, fuel_efficiency=76, ers_efficiency=80)),
        _car("mercedes", "W16", "Mercedes", dict(
            overall=85, qualifying_pace=86, race_pace=84, straight_line=87,
            cornering=83, braking=84, traction=82, tyre_management=74,
            tyre_degradation=52, fuel_efficiency=82, ers_efficiency=86)),
        _car("williams", "FW47", "Williams", dict(
            overall=72, qualifying_pace=74, race_pace=70, straight_line=84,
            cornering=68, braking=72, traction=70, tyre_management=68,
            tyre_degradation=56, fuel_efficiency=76, ers_efficiency=78)),
        _car("racing_bulls", "VCARB 02", "Racing Bulls", dict(
            overall=68, qualifying_pace=71, race_pace=66, straight_line=74,
            cornering=70, braking=68, traction=66, tyre_management=64,
            tyre_degradation=58, fuel_efficiency=74, ers_efficiency=76)),
        _car("aston_martin", "AMR25", "Aston Martin", dict(
            overall=64, qualifying_pace=63, race_pace=65, straight_line=70,
            cornering=64, braking=66, traction=63, tyre_management=68,
            tyre_degradation=54, fuel_efficiency=74, ers_efficiency=75)),
        _car("haas", "VF-25", "Haas", dict(
            overall=63, qualifying_pace=62, race_pace=64, straight_line=72,
            cornering=62, braking=64, traction=62, tyre_management=70,
            tyre_degradation=50, fuel_efficiency=73, ers_efficiency=74)),
        _car("alpine", "A525", "Alpine", dict(
            overall=58, qualifying_pace=58, race_pace=57, straight_line=68,
            cornering=57, braking=60, traction=56, tyre_management=58,
            tyre_degradation=62, fuel_efficiency=70, ers_efficiency=72)),
        _car("sauber", "C45", "Kick Sauber", dict(
            overall=55, qualifying_pace=54, race_pace=56, straight_line=66,
            cornering=54, braking=58, traction=54, tyre_management=60,
            tyre_degradation=60, fuel_efficiency=71, ers_efficiency=73)),
        _car("generic", "Generic / Unknown", "", {}),
    ]


def _f1_26_cars() -> list[CarProfile]:
    """The 2026 grid.

    A different roster: Audi replaces Sauber, Cadillac joins as an eleventh
    team, and every car is built to a new regulation set.

    Every rating is left at 50 (midfield) with near-zero confidence, and
    that is deliberate. Nobody has performance data for 2026 cars, so
    copying the 2025 ratings across would be inventing a pecking order that
    does not exist - exactly the fabrication the brief rules out. These are
    honest placeholders: edit them, or let the learning layer fill them in
    from real observed pace.
    """
    note = "2026 regulations - performance unknown until observed."
    teams = [
        ("mclaren_26", "McLaren 2026", "McLaren"),
        ("ferrari_26", "Ferrari 2026", "Ferrari"),
        ("red_bull_26", "Red Bull 2026", "Red Bull Racing"),
        ("mercedes_26", "Mercedes 2026", "Mercedes"),
        ("aston_martin_26", "Aston Martin 2026", "Aston Martin"),
        ("alpine_26", "Alpine 2026", "Alpine"),
        ("williams_26", "Williams 2026", "Williams"),
        ("racing_bulls_26", "Racing Bulls 2026", "Racing Bulls"),
        ("haas_26", "Haas 2026", "Haas"),
        ("audi_26", "Audi 2026", "Audi"),
        ("cadillac_26", "Cadillac 2026", "Cadillac"),
    ]
    cars = []
    for car_id, name, team in teams:
        profile = CarProfile(
            car_id=car_id, name=name, team=team, year=2026,
            confidence=UNKNOWN_CONFIDENCE, notes=note,
        )
        cars.append(profile.clamped())
    cars.append(
        CarProfile(
            car_id="generic", name="Generic / Unknown", year=2026,
            confidence=UNKNOWN_CONFIDENCE, notes=note,
        ).clamped()
    )
    return cars


def cars_dir(mode: GameMode = GameMode.F1_25):
    """Per-mode directory: the same team can be rated differently between
    games, so the databases are versioned rather than shared."""
    return data_dir() / "modes" / mode.value / "cars"


def create_car_store(
    directory=None, mode: GameMode = GameMode.F1_25
) -> RecordStore[CarProfile]:
    return RecordStore(
        directory=directory or cars_dir(mode),
        builtins=lambda: builtin_cars(mode),
        key_of=lambda c: c.car_id,
        from_dict=lambda d: dataclass_from_dict(CarProfile, d).clamped(),
        to_dict=asdict,
    )
