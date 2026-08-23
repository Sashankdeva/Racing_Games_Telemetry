"""Track database - editable circuit characteristics.

Like the car ratings, these are **initial assumptions**, not measurements.
They give the strategy engine something to reason with on lap 1; observed
degradation and pace from the actual session should override them as
evidence accumulates.

`pit_loss_s` is the exception: it is a measurable, fairly stable property of
the circuit (pit lane length and speed limit), so it is a real estimate
rather than a subjective rating and is expressed in seconds.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from app.core.paths import data_dir
from app.games.modes import GameMode
from app.domain.store import RecordStore, dataclass_from_dict

PRIOR_CONFIDENCE = 0.3


@dataclass(slots=True)
class TrackProfile:
    track_id: str = "generic"
    name: str = "Generic Circuit"
    country: str = ""
    race_laps: int = 55

    # --- characteristics, 0-100 unless noted --------------------------
    #: Overall energy through the tyre across a lap.
    tyre_stress: float = 50.0
    #: Expected degradation tendency. Higher = tyres fall away faster.
    degradation: float = 50.0
    high_speed_balance: float = 50.0
    low_speed_balance: float = 50.0
    #: Higher = harder to overtake.
    overtaking_difficulty: float = 50.0
    drs_effectiveness: float = 50.0
    braking_severity: float = 50.0

    #: Seconds lost for a pit stop, pit entry to pit exit including the
    #: stationary time. A real measurable figure, not a rating.
    pit_loss_s: float = 21.0

    #: How well each compound suits this circuit, 0-100.
    soft_suitability: float = 50.0
    medium_suitability: float = 50.0
    hard_suitability: float = 50.0

    confidence: float = PRIOR_CONFIDENCE
    notes: str = ""

    @property
    def is_prior(self) -> bool:
        return self.confidence <= PRIOR_CONFIDENCE

    def clamped(self) -> "TrackProfile":
        for f in (
            "tyre_stress", "degradation", "high_speed_balance",
            "low_speed_balance", "overtaking_difficulty", "drs_effectiveness",
            "braking_severity", "soft_suitability", "medium_suitability",
            "hard_suitability",
        ):
            setattr(self, f, max(0.0, min(100.0, float(getattr(self, f)))))
        self.pit_loss_s = max(10.0, min(45.0, float(self.pit_loss_s)))
        self.race_laps = max(1, min(120, int(self.race_laps)))
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        return self


RATING_FIELDS: tuple[tuple[str, str], ...] = (
    ("tyre_stress", "Tyre stress"),
    ("degradation", "Degradation tendency"),
    ("high_speed_balance", "High-speed corner balance"),
    ("low_speed_balance", "Low-speed corner balance"),
    ("overtaking_difficulty", "Overtaking difficulty"),
    ("drs_effectiveness", "DRS effectiveness"),
    ("braking_severity", "Braking severity"),
    ("soft_suitability", "Soft suitability"),
    ("medium_suitability", "Medium suitability"),
    ("hard_suitability", "Hard suitability"),
)


def _track(track_id, name, country, laps, pit_loss, values: dict) -> TrackProfile:
    profile = TrackProfile(
        track_id=track_id, name=name, country=country,
        race_laps=laps, pit_loss_s=pit_loss,
    )
    for key, value in values.items():
        setattr(profile, key, float(value))
    return profile.clamped()


def builtin_tracks(mode: GameMode = GameMode.F1_25) -> list[TrackProfile]:
    """Shipped calendar for a game mode.

    The calendars genuinely differ: 2026 adds Madrid and drops Imola.
    Everything else carries the same circuit characteristics, because the
    corners do not change with the regulations - only the cars do.
    """
    tracks = _base_tracks()
    if mode is GameMode.F1_26:
        tracks = [t for t in tracks if t.track_id != "imola"]
        tracks.insert(4, _madrid())
    return tracks


def _madrid() -> TrackProfile:
    """New for 2026. Characteristics are estimates with low confidence -
    nobody has raced it yet, and saying otherwise would be invention."""
    track = _track("madrid", "Madring", "Spain", 57, 21.0, dict(
        tyre_stress=55, degradation=50, high_speed_balance=60,
        low_speed_balance=55, overtaking_difficulty=55, drs_effectiveness=60,
        braking_severity=60, soft_suitability=55, medium_suitability=55,
        hard_suitability=55))
    track.confidence = 0.05
    track.notes = "New circuit for 2026 - characteristics unknown until raced."
    return track


def _base_tracks() -> list[TrackProfile]:
    return [
        _track("bahrain", "Bahrain International", "Bahrain", 57, 22.5, dict(
            tyre_stress=82, degradation=85, high_speed_balance=55, low_speed_balance=70,
            overtaking_difficulty=25, drs_effectiveness=80, braking_severity=80,
            soft_suitability=40, medium_suitability=70, hard_suitability=85)),
        _track("jeddah", "Jeddah Corniche", "Saudi Arabia", 50, 20.0, dict(
            tyre_stress=60, degradation=45, high_speed_balance=88, low_speed_balance=35,
            overtaking_difficulty=45, drs_effectiveness=75, braking_severity=60,
            soft_suitability=70, medium_suitability=75, hard_suitability=55)),
        _track("melbourne", "Albert Park", "Australia", 58, 20.5, dict(
            tyre_stress=62, degradation=52, high_speed_balance=72, low_speed_balance=55,
            overtaking_difficulty=55, drs_effectiveness=65, braking_severity=65,
            soft_suitability=62, medium_suitability=72, hard_suitability=62)),
        _track("suzuka", "Suzuka", "Japan", 53, 22.0, dict(
            tyre_stress=88, degradation=72, high_speed_balance=92, low_speed_balance=45,
            overtaking_difficulty=70, drs_effectiveness=45, braking_severity=55,
            soft_suitability=45, medium_suitability=72, hard_suitability=80)),
        _track("imola", "Imola", "Italy", 63, 26.0, dict(
            tyre_stress=65, degradation=48, high_speed_balance=68, low_speed_balance=60,
            overtaking_difficulty=85, drs_effectiveness=40, braking_severity=70,
            soft_suitability=60, medium_suitability=75, hard_suitability=65)),
        _track("monaco", "Monaco", "Monaco", 78, 19.0, dict(
            tyre_stress=25, degradation=20, high_speed_balance=10, low_speed_balance=95,
            overtaking_difficulty=98, drs_effectiveness=15, braking_severity=75,
            soft_suitability=90, medium_suitability=60, hard_suitability=25)),
        _track("barcelona", "Barcelona-Catalunya", "Spain", 66, 21.0, dict(
            tyre_stress=85, degradation=78, high_speed_balance=80, low_speed_balance=55,
            overtaking_difficulty=70, drs_effectiveness=55, braking_severity=55,
            soft_suitability=45, medium_suitability=72, hard_suitability=82)),
        _track("montreal", "Circuit Gilles-Villeneuve", "Canada", 70, 18.5, dict(
            tyre_stress=45, degradation=42, high_speed_balance=55, low_speed_balance=70,
            overtaking_difficulty=35, drs_effectiveness=82, braking_severity=90,
            soft_suitability=75, medium_suitability=70, hard_suitability=50)),
        _track("silverstone", "Silverstone", "United Kingdom", 52, 21.5, dict(
            tyre_stress=90, degradation=75, high_speed_balance=95, low_speed_balance=35,
            overtaking_difficulty=40, drs_effectiveness=65, braking_severity=55,
            soft_suitability=45, medium_suitability=70, hard_suitability=82)),
        _track("spa", "Spa-Francorchamps", "Belgium", 44, 19.5, dict(
            tyre_stress=78, degradation=62, high_speed_balance=92, low_speed_balance=35,
            overtaking_difficulty=30, drs_effectiveness=85, braking_severity=60,
            soft_suitability=55, medium_suitability=75, hard_suitability=72)),
        _track("monza", "Monza", "Italy", 53, 20.0, dict(
            tyre_stress=48, degradation=40, high_speed_balance=95, low_speed_balance=25,
            overtaking_difficulty=25, drs_effectiveness=88, braking_severity=85,
            soft_suitability=72, medium_suitability=75, hard_suitability=55)),
        _track("singapore", "Marina Bay", "Singapore", 62, 27.0, dict(
            tyre_stress=55, degradation=50, high_speed_balance=25, low_speed_balance=92,
            overtaking_difficulty=88, drs_effectiveness=35, braking_severity=82,
            soft_suitability=78, medium_suitability=70, hard_suitability=42)),
        _track("cota", "Circuit of the Americas", "United States", 56, 21.0, dict(
            tyre_stress=75, degradation=68, high_speed_balance=78, low_speed_balance=62,
            overtaking_difficulty=40, drs_effectiveness=72, braking_severity=70,
            soft_suitability=55, medium_suitability=75, hard_suitability=70)),
        _track("mexico", "Autodromo Hermanos Rodriguez", "Mexico", 71, 22.0, dict(
            tyre_stress=45, degradation=45, high_speed_balance=60, low_speed_balance=70,
            overtaking_difficulty=45, drs_effectiveness=78, braking_severity=75,
            soft_suitability=70, medium_suitability=72, hard_suitability=55)),
        _track("interlagos", "Interlagos", "Brazil", 71, 20.0, dict(
            tyre_stress=70, degradation=65, high_speed_balance=70, low_speed_balance=62,
            overtaking_difficulty=35, drs_effectiveness=78, braking_severity=65,
            soft_suitability=60, medium_suitability=75, hard_suitability=68)),
        _track("vegas", "Las Vegas Strip", "United States", 50, 20.0, dict(
            tyre_stress=40, degradation=38, high_speed_balance=85, low_speed_balance=45,
            overtaking_difficulty=35, drs_effectiveness=85, braking_severity=70,
            soft_suitability=75, medium_suitability=70, hard_suitability=48)),
        _track("qatar", "Lusail", "Qatar", 57, 23.0, dict(
            tyre_stress=95, degradation=90, high_speed_balance=88, low_speed_balance=40,
            overtaking_difficulty=60, drs_effectiveness=60, braking_severity=50,
            soft_suitability=35, medium_suitability=68, hard_suitability=88)),
        _track("abu_dhabi", "Yas Marina", "UAE", 58, 21.0, dict(
            tyre_stress=55, degradation=48, high_speed_balance=58, low_speed_balance=68,
            overtaking_difficulty=65, drs_effectiveness=70, braking_severity=72,
            soft_suitability=68, medium_suitability=75, hard_suitability=60)),
        _track("generic", "Generic / Unknown", "", 55, 21.0, {}),
    ]


def tracks_dir(mode: GameMode = GameMode.F1_25):
    """Per-mode directory: the same team can be rated differently between
    games, so the databases are versioned rather than shared."""
    return data_dir() / "modes" / mode.value / "tracks"


def create_track_store(
    directory=None, mode: GameMode = GameMode.F1_25
) -> RecordStore[TrackProfile]:
    return RecordStore(
        directory=directory or tracks_dir(mode),
        builtins=lambda: builtin_tracks(mode),
        key_of=lambda t: t.track_id,
        from_dict=lambda d: dataclass_from_dict(TrackProfile, d).clamped(),
        to_dict=asdict,
    )
