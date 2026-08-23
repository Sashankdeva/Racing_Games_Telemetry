"""Car & Track Intelligence - the context layer.

    game mode -> car profile + track profile -> OBSERVED profile
                                                      |
                        race intelligence / strategy / driver coach
                                                      |
                                            smart suggestions -> UI

A data layer, not a decision layer. It holds three kinds of information and
never lets them blur together:

    PROFILE    shipped or user-edited ratings. Static, versioned, may be a
               low-confidence prior.
    OBSERVED   measured from the driver's own sessions. Carries its sample
               count, session count and when it was last updated.
    INFERENCE  a conclusion drawn from the two above. Always labelled, always
               carries confidence, and is never presented as fact.

`Attribute.source` says which of those a number is, so a consumer can never
accidentally treat an inference as a specification.

Observed data never overwrites a profile. They live in separate files and
separate directories, and a consumer that wants the best available answer
asks for it explicitly - at which point it is told which one it got.

Mode isolation
--------------

Learned data is written to `modes/<mode>/observed/`, beside the existing
per-mode `cars/` and `tracks/`. F1 25 Ferrari and F1 26 Ferrari are
different files in different directories; there is no shared path for them
to contaminate each other through.

Contamination
-------------

A degradation figure measured behind a safety car, in the wet, or on an
in-lap describes the conditions, not the car. `clean_laps()` filters those
out using the categories lap analysis already assigns, and anything learned
records how many laps survived that filter.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from app.core.logging import get_logger
from app.core.models import TelemetryFrame
from app.core.paths import data_dir, read_text, write_atomic
from app.domain.car_profiles import CarProfile
from app.domain.driver_session import LapRecord
from app.domain.lap_analysis import Confidence, LapCategory, classify_laps
from app.domain.stints import Stint
from app.domain.track_profiles import TrackProfile
from app.games.modes import GameMode

_log = get_logger(__name__)

#: Bump only on a breaking change to the exported format.
SCHEMA_VERSION = 1

#: How much an existing estimate is discounted when a new session arrives.
#: Below 1.0 so recent, clean observations count for more without a single
#: session being able to erase everything learned before it.
SESSION_DECAY = 0.7
#: Sample counts at which an observed value earns each confidence level.
SAMPLES_FOR_LOW = 3
SAMPLES_FOR_MEDIUM = 8
SAMPLES_FOR_HIGH = 15
#: Ratings at or above this are treated as a genuinely high demand.
HIGH_DEMAND = 65.0
#: Gap to the car ahead under which a lap counts as run in traffic.
#: A lap spent in someone's wake describes their pace, not this car's.
TRAFFIC_GAP_S = 1.5
#: Fuel burn this far from the session median marks a lap as unusual -
#: a fuel-save phase or a very different load is not baseline pace.
FUEL_OUTLIER_RATIO = 0.35


class Source(str, Enum):
    """Where a number came from. Never guessed at by a consumer."""

    PROFILE = "PROFILE"
    OBSERVED = "OBSERVED"
    INFERENCE = "INFERENCE"
    UNKNOWN = "UNKNOWN"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Attribute:
    """One value, and how much it deserves to be trusted."""

    name: str
    value: float | str | None = None
    source: Source = Source.UNKNOWN
    confidence: Confidence = Confidence.NO_DATA
    sample_count: int = 0

    @property
    def known(self) -> bool:
        return self.value is not None and self.source is not Source.UNKNOWN

    def describe(self) -> str:
        if not self.known:
            return "UNKNOWN"
        return f"{self.value} ({self.source.value}, {self.confidence.value})"


@dataclass(slots=True)
class ObservedValue:
    """A characteristic measured from real sessions."""

    metric: str
    value: float
    sample_count: int = 0
    session_count: int = 1
    last_updated: float = field(default_factory=time.time)

    @property
    def confidence(self) -> Confidence:
        if self.sample_count <= 0:
            return Confidence.NO_DATA
        if self.sample_count < SAMPLES_FOR_LOW:
            return Confidence.INSUFFICIENT
        if self.sample_count < SAMPLES_FOR_MEDIUM:
            return Confidence.LOW
        if self.sample_count < SAMPLES_FOR_HIGH:
            return Confidence.MEDIUM
        return Confidence.HIGH

    def merged_with(self, value: float, samples: int, now: float | None = None) -> "ObservedValue":
        """Fold a new session in, weighted towards the recent one.

        A straight replacement would throw away everything learned; a plain
        average would make the twentieth session unable to move the estimate
        at all. Decaying the old weight does neither.
        """
        if samples <= 0:
            return self
        old_weight = self.sample_count * SESSION_DECAY
        total = old_weight + samples
        blended = (self.value * old_weight + value * samples) / total if total else value
        return ObservedValue(
            metric=self.metric,
            value=round(blended, 5),
            sample_count=int(round(total)),
            session_count=self.session_count + 1,
            last_updated=time.time() if now is None else now,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ObservedValue":
        return cls(
            metric=str(data["metric"]),
            value=float(data["value"]),
            sample_count=int(data.get("sample_count", 0)),
            session_count=int(data.get("session_count", 1)),
            last_updated=float(data.get("last_updated", 0.0)),
        )


class ProfileError(ValueError):
    """Raised when profile data cannot be trusted to load."""


@dataclass(slots=True)
class ObservedProfile:
    """Everything learned about one car or one track, in one game mode."""

    subject_id: str
    kind: str  # "car" or "track"
    mode: str
    values: dict[str, ObservedValue] = field(default_factory=dict)

    def get(self, metric: str) -> ObservedValue | None:
        return self.values.get(metric)

    def attribute(self, metric: str) -> Attribute:
        observed = self.values.get(metric)
        if observed is None:
            return Attribute(name=metric)
        return Attribute(
            name=metric,
            value=observed.value,
            source=Source.OBSERVED,
            confidence=observed.confidence,
            sample_count=observed.sample_count,
        )

    def record(self, metric: str, value: float, samples: int) -> None:
        """Learn, or refine what is already known."""
        if samples <= 0:
            return
        existing = self.values.get(metric)
        if existing is None:
            self.values[metric] = ObservedValue(
                metric=metric, value=round(value, 5), sample_count=samples
            )
        else:
            self.values[metric] = existing.merged_with(value, samples)

    # --- serialisation -------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "type": f"{self.kind}_profile",
            "game_mode": self.mode,
            "id": self.subject_id,
            "observations": {
                name: value.to_dict() for name, value in self.values.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ObservedProfile":
        """Load, refusing anything that cannot be trusted.

        Invalid data is rejected loudly rather than being coerced into
        something plausible - a silently mangled profile would go on to
        influence strategy.
        """
        if not isinstance(data, dict):
            raise ProfileError("profile must be a JSON object")

        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ProfileError(
                f"unsupported schema_version {version!r}; expected {SCHEMA_VERSION}"
            )

        kind = str(data.get("type", "")).replace("_profile", "")
        if kind not in ("car", "track"):
            raise ProfileError(f"unknown profile type: {data.get('type')!r}")

        mode = str(data.get("game_mode", ""))
        if mode not in {m.value for m in GameMode}:
            raise ProfileError(f"unknown game_mode: {mode!r}")

        subject = str(data.get("id", ""))
        if not subject:
            raise ProfileError("profile has no id")

        values: dict[str, ObservedValue] = {}
        for name, raw in (data.get("observations") or {}).items():
            try:
                values[str(name)] = ObservedValue.from_dict(raw)
            except (KeyError, TypeError, ValueError) as exc:
                raise ProfileError(f"bad observation {name!r}: {exc}") from exc

        return cls(subject_id=subject, kind=kind, mode=mode, values=values)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, text: str) -> "ObservedProfile":
        try:
            return cls.from_dict(json.loads(text))
        except json.JSONDecodeError as exc:
            raise ProfileError(f"not valid JSON: {exc}") from exc


def observed_dir(mode: GameMode) -> Path:
    """Per-mode, beside the existing cars/ and tracks/ directories.

    Isolation is structural: F1 25 and F1 26 have no shared path.
    """
    return data_dir() / "modes" / mode.value / "observed"


class ObservedStore:
    """Loads and saves observed profiles, one file per subject."""

    def __init__(self, mode: GameMode) -> None:
        self.mode = mode
        self.directory = observed_dir(mode)

    def _path(self, kind: str, subject_id: str) -> Path:
        safe = "".join(c for c in subject_id if c.isalnum() or c in "-_") or "unknown"
        return self.directory / f"{kind}_{safe}.json"

    def load(self, kind: str, subject_id: str) -> ObservedProfile:
        path = self._path(kind, subject_id)
        if not path.exists():
            return ObservedProfile(
                subject_id=subject_id, kind=kind, mode=self.mode.value
            )
        try:
            profile = ObservedProfile.from_json(read_text(path))
        except (ProfileError, OSError) as exc:
            # A corrupt file must not take the application down, and must
            # not be silently treated as empty either.
            _log.warning("Ignoring unreadable observed profile %s: %s", path, exc)
            return ObservedProfile(
                subject_id=subject_id, kind=kind, mode=self.mode.value
            )

        if profile.mode != self.mode.value:
            # A file that claims another mode does not belong here.
            _log.warning(
                "Observed profile %s claims mode %s; ignoring under %s",
                path, profile.mode, self.mode.value,
            )
            return ObservedProfile(
                subject_id=subject_id, kind=kind, mode=self.mode.value
            )
        return profile

    def save(self, profile: ObservedProfile) -> bool:
        """Atomic write, so a crash mid-save cannot truncate a profile."""
        try:
            write_atomic(
                self._path(profile.kind, profile.subject_id), profile.to_json()
            )
            return True
        except OSError:
            _log.exception("Could not save observed profile %s", profile.subject_id)
            return False


# ---------------------------------------------------------------------------
# contamination
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SessionQuality:
    """Which laps may be used as a baseline, and why the rest may not."""

    total: int = 0
    clean: int = 0
    excluded: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.clean >= SAMPLES_FOR_LOW

    def describe(self) -> str:
        if not self.total:
            return "no laps"
        if not self.excluded:
            return f"{self.clean} of {self.total} laps clean"
        return (
            f"{self.clean} of {self.total} laps clean; "
            f"excluded {', '.join(self.excluded)}"
        )


def _fuel_outliers(laps: list[LapRecord]) -> set[int]:
    """Laps whose fuel burn is far from the session median.

    The median is the reference rather than the mean, so the outliers being
    looked for cannot drag the reference towards themselves.
    """
    burns = [lap.fuel_used for lap in laps if lap.fuel_used > 0]
    if len(burns) < 4:
        return set()  # too few to say what "normal" looks like
    median = statistics.median(burns)
    if median <= 0:
        return set()
    return {
        lap.lap_number
        for lap in laps
        if lap.fuel_used > 0
        and abs(lap.fuel_used - median) / median > FUEL_OUTLIER_RATIO
    }


def clean_laps(
    laps: list[LapRecord],
    *,
    wet: bool = False,
    damaged: bool = False,
    traffic_laps: set[int] | None = None,
) -> tuple[list[LapRecord], SessionQuality]:
    """Laps fit to describe the car rather than the circumstances.

    Safety-car, pit, invalid, formation and outlier laps are excluded using
    the categories lap analysis already assigns - this does not re-derive
    them. On top of those:

      * a lap run in traffic describes the car ahead's pace, not this one's
      * a lap with an unusual fuel burn is not baseline performance

    Wet running and damage exclude the whole session, because neither is a
    per-lap property in the telemetry.
    """
    if not laps:
        return [], SessionQuality()

    if wet or damaged:
        reasons = []
        if wet:
            reasons.append("wet conditions")
        if damaged:
            reasons.append("car damage")
        return [], SessionQuality(total=len(laps), clean=0, excluded=tuple(reasons))

    categories = classify_laps(laps)
    traffic = traffic_laps or set()
    fuel_odd = _fuel_outliers(laps)

    kept: list[LapRecord] = []
    excluded: set[str] = set()
    for lap, category in zip(laps, categories):
        if category is not LapCategory.CLEAN:
            excluded.add(category.value)
            continue
        if lap.lap_number in traffic:
            excluded.add("TRAFFIC")
            continue
        if lap.lap_number in fuel_odd:
            excluded.add("UNUSUAL FUEL")
            continue
        kept.append(lap)

    return kept, SessionQuality(
        total=len(laps), clean=len(kept), excluded=tuple(sorted(excluded))
    )


# ---------------------------------------------------------------------------
# the context handed to strategy / coach / suggestions
# ---------------------------------------------------------------------------
class SegmentKind(str, Enum):
    """What a piece of track is known to demand."""

    HEAVY_BRAKING = "heavy_braking"
    LOW_SPEED = "low_speed"
    MEDIUM_SPEED = "medium_speed"
    HIGH_SPEED = "high_speed"
    TRACTION_ZONE = "traction_zone"
    DRS_ZONE = "drs_zone"
    OVERTAKING_ZONE = "overtaking_zone"


@dataclass(frozen=True, slots=True)
class TrackSegment:
    """One named region of a circuit.

    A corner number appears only when a track profile actually supplies
    one. Nothing here derives a corner from telemetry: the F1 feed carries
    lap distance and a sector index, and neither identifies a corner. So
    `corner` stays None and consumers fall back to the sector, which is
    what they already do.
    """

    sector: int
    name: str = ""
    corner: int | None = None
    kinds: tuple[SegmentKind, ...] = ()

    @property
    def identified(self) -> bool:
        """True only when this came from real metadata."""
        return bool(self.name or self.corner is not None)

    def has(self, kind: SegmentKind) -> bool:
        return kind in self.kinds


#: Known segments per (game mode, track id). Deliberately EMPTY: no
#: verified corner metadata exists for either title, and inventing it is
#: exactly what this module must not do. Adding a circuit later is a data
#: operation - one entry here - rather than a code change.
TRACK_SEGMENTS: dict[tuple[str, str], tuple["TrackSegment", ...]] = {}


def register_segments(
    mode: GameMode, track_id: str, segments: tuple["TrackSegment", ...]
) -> None:
    """Publish verified segment metadata for a circuit.

    Mode-scoped like everything else: a layout change between titles must
    not silently apply to both.
    """
    TRACK_SEGMENTS[(mode.value, track_id)] = tuple(segments)


def track_segments(
    track: TrackProfile | None, mode: GameMode | None = None
) -> tuple[TrackSegment, ...]:
    """Segments for a circuit, when verified metadata exists for it.

    Returns empty today, so every consumer stays at sector level - which is
    the honest fallback given the F1 feed carries lap distance and a sector
    index, and neither identifies a corner.
    """
    if track is None or mode is None:
        return ()
    return TRACK_SEGMENTS.get((mode.value, track.track_id), ())


@dataclass(frozen=True, slots=True)
class RiskSignal:
    """An INFERENCE. Never a specification, and it says so."""

    name: str
    level: RiskLevel
    confidence: Confidence
    reason: str
    source: Source = Source.INFERENCE
    inputs: dict = field(default_factory=dict)

    @property
    def known(self) -> bool:
        return self.level is not RiskLevel.UNKNOWN


@dataclass(slots=True)
class ProfileContext:
    """What the analysis layers may ask about this car and track."""

    mode: GameMode
    car: CarProfile | None = None
    track: TrackProfile | None = None
    observed_car: ObservedProfile | None = None
    observed_track: ObservedProfile | None = None
    quality: SessionQuality = field(default_factory=SessionQuality)

    # --- lookups -------------------------------------------------------
    def observed(self, metric: str, *, of: str = "car") -> Attribute:
        profile = self.observed_car if of == "car" else self.observed_track
        if profile is None:
            return Attribute(name=metric)
        return profile.attribute(metric)

    def rating(self, field_name: str, *, of: str = "car") -> Attribute:
        """A shipped or user-edited rating.

        A prior is reported as PROFILE with LOW confidence rather than being
        dressed up: it is a starting assumption, not a measurement.
        """
        profile = self.car if of == "car" else self.track
        if profile is None:
            return Attribute(name=field_name)
        value = getattr(profile, field_name, None)
        if value is None:
            return Attribute(name=field_name)
        return Attribute(
            name=field_name,
            value=value,
            source=Source.PROFILE,
            confidence=(
                Confidence.LOW if profile.is_prior else Confidence.MEDIUM
            ),
        )

    def best_available(self, metric: str, rating_field: str, *, of: str = "car") -> Attribute:
        """Observed if it exists, otherwise the profile rating.

        The returned Attribute always says which one it is - the caller is
        never left guessing whether a number was measured or assumed.
        """
        observed = self.observed(metric, of=of)
        if observed.known and observed.confidence.is_usable:
            return observed
        return self.rating(rating_field, of=of)

    def segments(self) -> tuple[TrackSegment, ...]:
        """Verified segments for this circuit, or empty.

        Empty means "analyse by sector", which is what the coach already
        does - not "assume a layout".
        """
        return track_segments(self.track, self.mode)

    # --- inferences ----------------------------------------------------
    def risk_signals(self) -> dict[str, RiskSignal]:
        """Combined car+track signals. Only where evidence supports them."""
        return {
            "tyre_stress_risk": self._stress_risk(
                "tyre_stress_risk", "tyre_degradation", "tyre_stress",
                "tyre stress",
            ),
            "traction_risk": self._stress_risk(
                "traction_risk", "traction", "low_speed_balance",
                "traction demand", invert_car=True,
            ),
            "braking_risk": self._stress_risk(
                "braking_risk", "braking", "braking_severity",
                "braking demand", invert_car=True,
            ),
            "overtaking_difficulty": self._overtaking(),
        }

    def _stress_risk(
        self, name: str, car_field: str, track_field: str, label: str,
        *, invert_car: bool = False,
    ) -> RiskSignal:
        car = self.rating(car_field, of="car")
        track = self.rating(track_field, of="track")
        if not car.known and not track.known:
            return RiskSignal(
                name, RiskLevel.UNKNOWN, Confidence.NO_DATA,
                f"No car or track data for {label}.",
            )

        # A car rated highly for traction/braking is LESS at risk, so those
        # are inverted; tyre_degradation is already "higher = worse".
        car_value = float(car.value) if car.known else 50.0
        if invert_car:
            car_value = 100.0 - car_value
        track_value = float(track.value) if track.known else 50.0
        combined = (car_value + track_value) / 2.0

        level = (
            RiskLevel.HIGH if combined >= HIGH_DEMAND
            else RiskLevel.LOW if combined <= 35.0
            else RiskLevel.MEDIUM
        )
        # An inference is never more confident than its weakest input, and
        # a pair of priors can never be better than LOW.
        confidence = Confidence.LOW
        if car.confidence is Confidence.MEDIUM and track.confidence is Confidence.MEDIUM:
            confidence = Confidence.MEDIUM

        return RiskSignal(
            name,
            level,
            confidence,
            f"Combined {label}: car {car_value:.0f}, track {track_value:.0f}. "
            "Inferred from profile ratings, not measured.",
            inputs={
                "car": car.value,
                "car_source": car.source.value,
                "track": track.value,
                "track_source": track.source.value,
                "combined": round(combined, 1),
            },
        )

    def _overtaking(self) -> RiskSignal:
        track = self.rating("overtaking_difficulty", of="track")
        if not track.known:
            return RiskSignal(
                "overtaking_difficulty", RiskLevel.UNKNOWN, Confidence.NO_DATA,
                "No track data for overtaking difficulty.",
            )
        value = float(track.value)
        level = (
            RiskLevel.HIGH if value >= HIGH_DEMAND
            else RiskLevel.LOW if value <= 35.0
            else RiskLevel.MEDIUM
        )
        return RiskSignal(
            "overtaking_difficulty", level, track.confidence,
            f"{self.track.name if self.track else 'Track'} rated {value:.0f}/100 "
            "for overtaking difficulty.",
            inputs={"value": value, "source": track.source.value},
        )


# ---------------------------------------------------------------------------
class ProfileIntelligence:
    """Learns from sessions and hands out context. Decides nothing."""

    def __init__(self, mode: GameMode) -> None:
        self.mode = mode
        self._store = ObservedStore(mode)
        self._car_id = ""
        self._track_id = ""
        self._observed_car: ObservedProfile | None = None
        self._observed_track: ObservedProfile | None = None
        self._quality = SessionQuality()
        #: Highest speed seen this session, for the observed straight-line
        #: figure. Reset with the session, not with a dropout.
        self._top_speed = 0.0
        self._wet = False
        self._damaged = False
        #: Laps spent close behind another car - their pace, not ours.
        self._traffic_laps: set[int] = set()

    # ------------------------------------------------------------------
    def select(self, car_id: str, track_id: str) -> None:
        """Point at the car and track being driven, loading what is known."""
        if car_id != self._car_id:
            self._car_id = car_id
            self._observed_car = self._store.load("car", car_id) if car_id else None
        if track_id != self._track_id:
            self._track_id = track_id
            self._observed_track = (
                self._store.load("track", track_id) if track_id else None
            )

    def set_mode(self, mode: GameMode) -> None:
        """Switch game. Nothing crosses over - the store is rebuilt."""
        self.mode = mode
        self._store = ObservedStore(mode)
        self._car_id = ""
        self._track_id = ""
        self._observed_car = None
        self._observed_track = None

    def reset_session(self) -> None:
        """Clear per-session state. Learned profiles are untouched."""
        self._quality = SessionQuality()
        self._top_speed = 0.0
        self._wet = False
        self._damaged = False
        self._traffic_laps.clear()
        #: Laps spent close behind another car - their pace, not ours.
        self._traffic_laps: set[int] = set()

    # ------------------------------------------------------------------
    def observe_frame(self, frame: TelemetryFrame) -> None:
        """Cheap per-frame notes: top speed, and whether the session is
        contaminated by weather or damage."""
        if not frame.valid:
            return
        if frame.speed_kph > self._top_speed:
            self._top_speed = frame.speed_kph
        weather = (frame.weather or "").lower()
        if "rain" in weather or "storm" in weather:
            self._wet = True
        if frame.damage_summary():
            self._damaged = True
        # Within a second and a half of the car ahead is that car's
        # pace, not this one's.
        if frame.current_lap and 0 < frame.delta_to_car_ahead_s <= TRAFFIC_GAP_S:
            self._traffic_laps.add(frame.current_lap)

    def learn(self, laps: list[LapRecord], stints: list[Stint]) -> SessionQuality:
        """Fold this session's clean laps into the observed profiles."""
        usable, quality = clean_laps(
            laps,
            wet=self._wet,
            damaged=self._damaged,
            traffic_laps=self._traffic_laps,
        )
        self._quality = quality
        if not quality.usable:
            return quality

        car = self._observed_car
        if car is not None:
            fuel = [lap.fuel_used for lap in usable if lap.fuel_used > 0]
            if len(fuel) >= SAMPLES_FOR_LOW:
                car.record("fuel_per_lap_kg", sum(fuel) / len(fuel), len(fuel))
            if self._top_speed > 0:
                car.record("top_speed_kph", self._top_speed, quality.clean)
            # Consistency is the spread of the clean laps; a standard
            # deviation needs enough of them to mean anything.
            times = [lap.lap_time_s for lap in usable]
            if len(times) >= SAMPLES_FOR_LOW:
                car.record("consistency_s", statistics.stdev(times), len(times))

            # Degradation is per compound, and only from stints the tyre
            # model already judged measurable.
            for stint in stints:
                if not stint.compound or not stint.has_degradation:
                    continue
                car.record(
                    f"degradation_{stint.compound.lower()}",
                    stint.degradation_s_per_lap,
                    stint.clean_laps,
                )
            self._store.save(car)

        track = self._observed_track
        if track is not None:
            times = [lap.lap_time_s for lap in usable]
            if times:
                track.record("best_lap_s", min(times), len(times))
                track.record("average_lap_s", sum(times) / len(times), len(times))
            self._store.save(track)
        return quality

    # ------------------------------------------------------------------
    def context(
        self, car: CarProfile | None, track: TrackProfile | None
    ) -> ProfileContext:
        """The structured view every other module reads."""
        return ProfileContext(
            mode=self.mode,
            car=car,
            track=track,
            observed_car=self._observed_car,
            observed_track=self._observed_track,
            quality=self._quality,
        )

    # --- import / export ------------------------------------------------
    def export(self, kind: str) -> str:
        profile = self._observed_car if kind == "car" else self._observed_track
        if profile is None:
            raise ProfileError(f"no {kind} selected")
        return profile.to_json()

    def import_profile(self, text: str) -> ObservedProfile:
        """Load an exported profile, refusing anything invalid.

        A profile for another game mode is rejected rather than imported -
        that is exactly the contamination the mode split exists to prevent.
        """
        profile = ObservedProfile.from_json(text)
        if profile.mode != self.mode.value:
            raise ProfileError(
                f"profile is for {profile.mode}, not {self.mode.value}"
            )
        self._store.save(profile)
        if profile.kind == "car" and profile.subject_id == self._car_id:
            self._observed_car = profile
        elif profile.kind == "track" and profile.subject_id == self._track_id:
            self._observed_track = profile
        return profile
