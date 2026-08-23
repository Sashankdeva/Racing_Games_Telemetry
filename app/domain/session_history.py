"""Session History & Performance Progression.

    live telemetry -> session collector -> session record -> history store
                                                                  |
                                                     performance analysis
                                                                  |
                              strategy / driver coach / smart suggestions

Stores facts and compares them. It is not another coaching or strategy
engine: it never decides what to do, and it never words a message.

The rule this module exists to enforce
--------------------------------------

Live state and history are different things. A lap that has been completed
is a fact; it does not stop being a fact because the next UDP packet failed
to arrive. So:

    LIVE      telemetry is arriving
    STALE     telemetry stopped - history is untouched and still valid
    FINISHED  the session ended and was written to disk

`STALE` is never treated as empty, and nothing here ever writes a None over
a value that was previously known. Completed laps are appended and never
rewritten.

Crash safety
------------

The record is written after every completed lap, not only at shutdown. A
game that vanishes, a power cut or a killed process therefore costs at most
the lap in progress rather than the whole session. Writes go through a
temporary file and an atomic replace.

Mode isolation
--------------

Sessions live in `modes/<mode>/sessions/`, beside the existing per-mode
`cars/`, `tracks/` and `observed/` directories. F1 25 and F1 26 have no
shared path, and comparisons additionally require the same car and track
before two sessions are considered compatible at all.
"""

from __future__ import annotations

import csv
import io
import json
import statistics
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from app.core.logging import get_logger
from app.core.models import TelemetryFrame
from app.core.paths import data_dir, read_text, write_atomic
from app.domain.driver_session import LapRecord
from app.domain.lap_analysis import Confidence
from app.games.modes import GameMode

_log = get_logger(__name__)

#: Bump only on a breaking change to the stored format.
SCHEMA_VERSION = 1

#: Sessions needed before a trend is anything but noise.
MIN_SESSIONS_FOR_TREND = 3
#: Sessions averaged on each side when judging progression.
TREND_WINDOW = 3
#: Improvement smaller than this is not a trend.
TREND_THRESHOLD_S = 0.05
#: Consistency change smaller than this is not a trend.
CONSISTENCY_THRESHOLD_S = 0.03
#: A session with fewer clean laps than this is kept but flagged.
MIN_LAPS_FOR_CONFIDENCE = 3
#: Seconds of unbroken silence after which a session is closed and saved.
#: Long enough to survive a pause or a menu, short enough that a crashed
#: game does not leave the session open forever.
STALE_CLOSE_S = 120.0


class SessionType(str, Enum):
    PRACTICE = "PRACTICE"
    QUALIFYING = "QUALIFYING"
    SPRINT = "SPRINT"
    RACE = "RACE"
    TIME_TRIAL = "TIME_TRIAL"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def parse(cls, value: str) -> "SessionType":
        """Map the game's wording. Never guesses - unrecognised is UNKNOWN."""
        text = (value or "").strip().lower()
        if not text:
            return cls.UNKNOWN
        if "sprint" in text:
            return cls.SPRINT
        if "qualif" in text or text.startswith("q"):
            return cls.QUALIFYING
        if "practice" in text or text.startswith("p"):
            return cls.PRACTICE
        if "time trial" in text:
            return cls.TIME_TRIAL
        if "race" in text:
            return cls.RACE
        return cls.UNKNOWN


class SessionState(str, Enum):
    LIVE = "LIVE"
    STALE = "STALE"
    FINISHED = "FINISHED"


class Trend(str, Enum):
    IMPROVING = "IMPROVING"
    STABLE = "STABLE"
    DECLINING = "DECLINING"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class HistoryError(ValueError):
    """Raised when stored or imported session data cannot be trusted."""


def _optional(value: float) -> float | None:
    """None where a field is genuinely absent - never a plausible zero."""
    return value if value else None


@dataclass(slots=True)
class StoredLap:
    """One completed lap, frozen at the moment it completed.

    Deliberately a copy rather than a reference: history must not change
    because something later recomputed a live figure.
    """

    lap_number: int
    lap_time_s: float
    sector1_s: float = 0.0
    sector2_s: float = 0.0
    sector3_s: float = 0.0
    valid: bool = True
    compound: str = ""
    tyre_age_laps: int = -1
    tyre_wear_pct: float = 0.0
    fuel_remaining: float = 0.0
    fuel_used: float = 0.0
    ers_deployed: float = 0.0
    ers_harvested: float = 0.0
    position: int = 0
    pit_lap: bool = False
    safety_car_lap: bool = False

    @classmethod
    def from_record(cls, record: LapRecord) -> "StoredLap":
        return cls(
            lap_number=record.lap_number,
            lap_time_s=record.lap_time_s,
            sector1_s=record.sector1_s,
            sector2_s=record.sector2_s,
            sector3_s=record.sector3_s,
            valid=record.valid_for_pace,
            compound=record.compound,
            tyre_age_laps=record.tyre_age_laps,
            tyre_wear_pct=record.tyre_wear_pct,
            fuel_remaining=record.fuel_remaining,
            fuel_used=record.fuel_used,
            ers_deployed=record.ers_deployed,
            ers_harvested=record.ers_harvested,
            position=record.position,
            pit_lap=record.pit_lap,
            safety_car_lap=record.safety_car_lap,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "StoredLap":
        return cls(
            lap_number=int(data["lap_number"]),
            lap_time_s=float(data["lap_time_s"]),
            sector1_s=float(data.get("sector1_s", 0.0)),
            sector2_s=float(data.get("sector2_s", 0.0)),
            sector3_s=float(data.get("sector3_s", 0.0)),
            valid=bool(data.get("valid", True)),
            compound=str(data.get("compound", "")),
            tyre_age_laps=int(data.get("tyre_age_laps", -1)),
            tyre_wear_pct=float(data.get("tyre_wear_pct", 0.0)),
            fuel_remaining=float(data.get("fuel_remaining", 0.0)),
            fuel_used=float(data.get("fuel_used", 0.0)),
            ers_deployed=float(data.get("ers_deployed", 0.0)),
            ers_harvested=float(data.get("ers_harvested", 0.0)),
            position=int(data.get("position", 0)),
            pit_lap=bool(data.get("pit_lap", False)),
            safety_car_lap=bool(data.get("safety_car_lap", False)),
        )


@dataclass(slots=True)
class StoredStint:
    number: int
    compound: str = ""
    first_lap: int = 0
    last_lap: int = 0
    laps: int = 0
    clean_laps: int = 0
    #: None when the tyre model judged it unmeasurable - never a guess.
    degradation_s_per_lap: float | None = None
    confidence: str = Confidence.NO_DATA.name

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "StoredStint":
        raw = data.get("degradation_s_per_lap")
        return cls(
            number=int(data.get("number", 0)),
            compound=str(data.get("compound", "")),
            first_lap=int(data.get("first_lap", 0)),
            last_lap=int(data.get("last_lap", 0)),
            laps=int(data.get("laps", 0)),
            clean_laps=int(data.get("clean_laps", 0)),
            degradation_s_per_lap=None if raw is None else float(raw),
            confidence=str(data.get("confidence", Confidence.NO_DATA.name)),
        )


@dataclass(slots=True)
class StoredObservation:
    """A Driver Coach problem, as it stood when the session ended."""

    id: str
    category: str
    sector: int
    first_detected_lap: int
    occurrences: int
    peak_loss_s: float
    current_loss_s: float
    status: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "StoredObservation":
        return cls(
            id=str(data.get("id", "")),
            category=str(data.get("category", "")),
            sector=int(data.get("sector", 0)),
            first_detected_lap=int(data.get("first_detected_lap", 0)),
            occurrences=int(data.get("occurrences", 0)),
            peak_loss_s=float(data.get("peak_loss_s", 0.0)),
            current_loss_s=float(data.get("current_loss_s", 0.0)),
            status=str(data.get("status", "")),
        )


@dataclass(slots=True)
class StoredStrategyChange:
    lap: int
    previous: str
    current: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "StoredStrategyChange":
        return cls(
            lap=int(data.get("lap", 0)),
            previous=str(data.get("previous", "")),
            current=str(data.get("current", "")),
            reason=str(data.get("reason", "")),
        )


@dataclass(slots=True)
class SessionRecord:
    """One session. Append-only while live, immutable once FINISHED."""

    session_id: str
    game_mode: str
    started_at: float
    car_id: str = ""
    track_id: str = ""
    session_type: str = SessionType.UNKNOWN.value
    weather: str = ""
    track_temperature: float = 0.0
    air_temperature: float = 0.0
    state: str = SessionState.LIVE.value
    ended_at: float = 0.0
    finish_position: int = 0

    laps: list[StoredLap] = field(default_factory=list)
    stints: list[StoredStint] = field(default_factory=list)
    observations: list[StoredObservation] = field(default_factory=list)
    strategy_changes: list[StoredStrategyChange] = field(default_factory=list)
    #: The last plan the strategy engine recommended, for later comparison
    #: with what the driver actually did. No judgement is recorded.
    recommended_strategy: str = ""

    # --- derived, never stored as a source of truth --------------------
    @property
    def valid_laps(self) -> list[StoredLap]:
        return [lap for lap in self.laps if lap.valid and lap.lap_time_s > 0]

    @property
    def invalid_laps(self) -> int:
        return len(self.laps) - len(self.valid_laps)

    @property
    def laps_completed(self) -> int:
        return len(self.laps)

    @property
    def best_lap_s(self) -> float | None:
        times = [lap.lap_time_s for lap in self.valid_laps]
        return min(times) if times else None

    @property
    def average_lap_s(self) -> float | None:
        times = [lap.lap_time_s for lap in self.valid_laps]
        return sum(times) / len(times) if times else None

    @property
    def median_lap_s(self) -> float | None:
        times = [lap.lap_time_s for lap in self.valid_laps]
        return statistics.median(times) if times else None

    @property
    def consistency_s(self) -> float | None:
        times = [lap.lap_time_s for lap in self.valid_laps]
        return statistics.stdev(times) if len(times) > 1 else None

    def best_sector(self, index: int) -> float | None:
        """Best sector 1/2/3 across valid laps, or None if never reported."""
        values = [
            (lap.sector1_s, lap.sector2_s, lap.sector3_s)[index - 1]
            for lap in self.valid_laps
        ]
        real = [value for value in values if value > 0]
        return min(real) if real else None

    @property
    def theoretical_best_s(self) -> float | None:
        """Sum of the best sectors. None unless all three exist.

        Kept distinct from `best_lap_s`: one is a lap that was actually
        driven, the other is a lap that was not.
        """
        sectors = [self.best_sector(index) for index in (1, 2, 3)]
        if any(value is None for value in sectors):
            return None
        return sum(sectors)

    @property
    def fuel_used(self) -> float | None:
        used = sum(lap.fuel_used for lap in self.laps if lap.fuel_used > 0)
        return used or None

    @property
    def confidence(self) -> Confidence:
        return Confidence.from_samples(len(self.valid_laps))

    @property
    def telemetry_quality(self) -> str:
        """How much of this session is worth comparing against."""
        if not self.laps:
            return "NO DATA"
        ratio = len(self.valid_laps) / len(self.laps)
        if ratio >= 0.8:
            return "GOOD"
        return "MIXED" if ratio >= 0.4 else "POOR"

    def fingerprint(self) -> str:
        """A stable hash of the driving, ignoring id and wall-clock time.

        Two replays of one recording produce different session ids and
        timestamps but identical driving - this is what makes them
        comparable.
        """
        import hashlib

        payload = json.dumps(
            {
                "laps": [lap.to_dict() for lap in self.laps],
                "stints": [stint.to_dict() for stint in self.stints],
            },
            sort_keys=True,
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def compatible_with(self, other: "SessionRecord") -> bool:
        """Whether two sessions may be compared at all.

        Same title, same car, same circuit. An F1 25 Ferrari lap and an F1
        26 Ferrari lap are different cars under different regulations, and
        comparing them would be meaningless.
        """
        return (
            self.game_mode == other.game_mode
            and self.car_id == other.car_id
            and self.track_id == other.track_id
        )

    # --- serialisation ---------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "game_mode": self.game_mode,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "car_id": self.car_id,
            "track_id": self.track_id,
            "session_type": self.session_type,
            "weather": self.weather,
            "track_temperature": self.track_temperature,
            "air_temperature": self.air_temperature,
            "state": self.state,
            "finish_position": self.finish_position,
            "recommended_strategy": self.recommended_strategy,
            "laps": [lap.to_dict() for lap in self.laps],
            "stints": [stint.to_dict() for stint in self.stints],
            "observations": [item.to_dict() for item in self.observations],
            "strategy_changes": [
                item.to_dict() for item in self.strategy_changes
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SessionRecord":
        if not isinstance(data, dict):
            raise HistoryError("session must be a JSON object")

        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise HistoryError(
                f"unsupported schema_version {version!r}; expected {SCHEMA_VERSION}"
            )

        mode = str(data.get("game_mode", ""))
        if mode not in {m.value for m in GameMode}:
            raise HistoryError(f"unknown game_mode: {mode!r}")

        session_id = str(data.get("session_id", ""))
        if not session_id:
            raise HistoryError("session has no session_id")

        try:
            laps = [StoredLap.from_dict(item) for item in data.get("laps", [])]
            stints = [StoredStint.from_dict(item) for item in data.get("stints", [])]
            observations = [
                StoredObservation.from_dict(item)
                for item in data.get("observations", [])
            ]
            changes = [
                StoredStrategyChange.from_dict(item)
                for item in data.get("strategy_changes", [])
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise HistoryError(f"corrupt session data: {exc}") from exc

        return cls(
            session_id=session_id,
            game_mode=mode,
            started_at=float(data.get("started_at", 0.0)),
            ended_at=float(data.get("ended_at", 0.0)),
            car_id=str(data.get("car_id", "")),
            track_id=str(data.get("track_id", "")),
            session_type=str(data.get("session_type", SessionType.UNKNOWN.value)),
            weather=str(data.get("weather", "")),
            track_temperature=float(data.get("track_temperature", 0.0)),
            air_temperature=float(data.get("air_temperature", 0.0)),
            state=str(data.get("state", SessionState.FINISHED.value)),
            finish_position=int(data.get("finish_position", 0)),
            recommended_strategy=str(data.get("recommended_strategy", "")),
            laps=laps,
            stints=stints,
            observations=observations,
            strategy_changes=changes,
        )

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, text: str) -> "SessionRecord":
        try:
            return cls.from_dict(json.loads(text))
        except json.JSONDecodeError as exc:
            raise HistoryError(f"not valid JSON: {exc}") from exc

    def laps_to_csv(self) -> str:
        """Lap data as CSV, for spreadsheets."""
        buffer = io.StringIO()
        columns = [
            "lap_number", "lap_time_s", "sector1_s", "sector2_s", "sector3_s",
            "valid", "compound", "tyre_age_laps", "tyre_wear_pct",
            "fuel_used", "position", "pit_lap", "safety_car_lap",
        ]
        writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for lap in self.laps:
            row = lap.to_dict()
            writer.writerow({key: row[key] for key in columns})
        return buffer.getvalue()


def sessions_dir(mode: GameMode) -> Path:
    """Per-mode, beside cars/, tracks/ and observed/."""
    return data_dir() / "modes" / mode.value / "sessions"


class SessionStore:
    """Reads and writes session records. One file per session."""

    def __init__(self, mode: GameMode) -> None:
        self.mode = mode
        self.directory = sessions_dir(mode)

    def _path(self, session_id: str) -> Path:
        safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
        return self.directory / f"{safe or 'unknown'}.json"

    def save(self, record: SessionRecord) -> bool:
        """Atomic write. Called after every lap, not only at shutdown."""
        try:
            write_atomic(self._path(record.session_id), record.to_json())
            return True
        except OSError:
            _log.exception("Could not save session %s", record.session_id)
            return False

    def load_all(self) -> list[SessionRecord]:
        """Every readable session, newest first.

        A corrupt file is skipped with a warning rather than taking the
        history down with it - one bad session must not cost the rest.
        """
        if not self.directory.exists():
            return []
        records: list[SessionRecord] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                record = SessionRecord.from_json(read_text(path))
            except (HistoryError, OSError) as exc:
                _log.warning("Skipping unreadable session %s: %s", path.name, exc)
                continue
            if record.game_mode != self.mode.value:
                _log.warning(
                    "Session %s claims mode %s; ignoring under %s",
                    path.name, record.game_mode, self.mode.value,
                )
                continue
            records.append(record)
        return sorted(records, key=lambda r: r.started_at, reverse=True)

    def delete(self, session_id: str) -> bool:
        """Explicit deletion only. Nothing here ever prunes automatically."""
        try:
            self._path(session_id).unlink(missing_ok=True)
            return True
        except OSError:
            _log.exception("Could not delete session %s", session_id)
            return False

    def import_session(self, text: str) -> SessionRecord:
        """Load an exported session, refusing anything incompatible."""
        record = SessionRecord.from_json(text)
        if record.game_mode != self.mode.value:
            raise HistoryError(
                f"session is for {record.game_mode}, not {self.mode.value}"
            )
        self.save(record)
        return record


# ---------------------------------------------------------------------------
# comparison and progression
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SectorDelta:
    sector: int
    previous_s: float | None
    current_s: float | None
    delta_s: float | None

    @property
    def available(self) -> bool:
        return self.delta_s is not None


@dataclass(frozen=True, slots=True)
class SessionComparison:
    """Two compatible sessions, measured against each other."""

    available: bool = False
    reason: str = ""
    previous_best_s: float | None = None
    current_best_s: float | None = None
    improvement_s: float | None = None
    sectors: tuple[SectorDelta, ...] = ()

    @property
    def largest_gain(self) -> SectorDelta | None:
        """The sector that actually moved most - a fact, not a cause.

        Attributing it to a driving change is the Driver Coach's job, and
        only where it has evidence.
        """
        gains = [s for s in self.sectors if s.available and s.delta_s < 0]
        return min(gains, key=lambda s: s.delta_s) if gains else None


@dataclass(frozen=True, slots=True)
class CompoundHistory:
    compound: str
    stints: int
    average_length: float
    average_degradation: float | None
    best_lap_s: float | None
    average_lap_s: float | None
    confidence: Confidence


@dataclass(frozen=True, slots=True)
class Progression:
    """Long-term movement across sessions."""

    sessions: int = 0
    pace: Trend = Trend.INSUFFICIENT_DATA
    pace_delta_s: float = 0.0
    consistency: Trend = Trend.INSUFFICIENT_DATA
    consistency_delta_s: float = 0.0
    sectors: tuple[Trend, Trend, Trend] = (
        Trend.INSUFFICIENT_DATA, Trend.INSUFFICIENT_DATA, Trend.INSUFFICIENT_DATA
    )
    tyre_management: Trend = Trend.INSUFFICIENT_DATA


def _trend(earlier: float | None, latest: float | None, threshold: float) -> tuple[Trend, float]:
    """Lower is better for every metric here."""
    if earlier is None or latest is None:
        return (Trend.INSUFFICIENT_DATA, 0.0)
    delta = earlier - latest
    if delta >= threshold:
        return (Trend.IMPROVING, round(delta, 3))
    if delta <= -threshold:
        return (Trend.DECLINING, round(delta, 3))
    return (Trend.STABLE, round(delta, 3))


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


class HistoryAnalysis:
    """Compares sessions. Holds no opinions and produces no messages."""

    def __init__(self, sessions: list[SessionRecord]) -> None:
        #: Newest first, as the store returns them.
        self.sessions = sessions

    # ------------------------------------------------------------------
    def compatible(
        self, *, car_id: str = "", track_id: str = "",
        session_type: str = "", since: float = 0.0,
    ) -> list[SessionRecord]:
        """Search. Every filter is optional; mode is already implicit."""
        out = self.sessions
        if car_id:
            out = [s for s in out if s.car_id == car_id]
        if track_id:
            out = [s for s in out if s.track_id == track_id]
        if session_type:
            out = [s for s in out if s.session_type == session_type]
        if since:
            out = [s for s in out if s.started_at >= since]
        return out

    def personal_best(self, sessions: list[SessionRecord] | None = None) -> float | None:
        pool = self.sessions if sessions is None else sessions
        bests = [s.best_lap_s for s in pool if s.best_lap_s is not None]
        return min(bests) if bests else None

    def personal_best_sector(
        self, index: int, sessions: list[SessionRecord] | None = None
    ) -> float | None:
        pool = self.sessions if sessions is None else sessions
        values = [s.best_sector(index) for s in pool]
        real = [value for value in values if value is not None]
        return min(real) if real else None

    def theoretical_best(
        self, sessions: list[SessionRecord] | None = None
    ) -> float | None:
        """Best sectors across sessions. Distinct from an actual lap."""
        sectors = [self.personal_best_sector(index, sessions) for index in (1, 2, 3)]
        if any(value is None for value in sectors):
            return None
        return sum(sectors)

    def compare(
        self, current: SessionRecord, previous: SessionRecord | None = None
    ) -> SessionComparison:
        """Current against the most recent compatible session."""
        if previous is None:
            candidates = [
                s for s in self.sessions
                if s.session_id != current.session_id and current.compatible_with(s)
            ]
            previous = candidates[0] if candidates else None

        if previous is None:
            return SessionComparison(
                reason="no earlier session on this car and track to compare with"
            )
        if not current.compatible_with(previous):
            return SessionComparison(
                reason="sessions are not on the same game, car and track"
            )
        if current.best_lap_s is None or previous.best_lap_s is None:
            return SessionComparison(reason="one of the sessions has no valid lap")

        sectors = []
        for index in (1, 2, 3):
            before = previous.best_sector(index)
            after = current.best_sector(index)
            delta = (
                round(after - before, 3)
                if before is not None and after is not None
                else None
            )
            sectors.append(SectorDelta(index, before, after, delta))

        return SessionComparison(
            available=True,
            previous_best_s=previous.best_lap_s,
            current_best_s=current.best_lap_s,
            improvement_s=round(current.best_lap_s - previous.best_lap_s, 3),
            sectors=tuple(sectors),
        )

    def progression(self, sessions: list[SessionRecord] | None = None) -> Progression:
        """Trends across sessions, oldest to newest.

        Refuses to report anything from a handful of sessions - a single
        good day is not progress.
        """
        pool = list(reversed(sessions if sessions is not None else self.sessions))
        usable = [s for s in pool if s.best_lap_s is not None]
        if len(usable) < MIN_SESSIONS_FOR_TREND:
            return Progression(sessions=len(usable))

        window = min(TREND_WINDOW, len(usable) // 2) or 1
        earlier, latest = usable[:window], usable[-window:]

        pace, pace_delta = _trend(
            _mean([s.average_lap_s for s in earlier if s.average_lap_s]),
            _mean([s.average_lap_s for s in latest if s.average_lap_s]),
            TREND_THRESHOLD_S,
        )
        consistency, consistency_delta = _trend(
            _mean([s.consistency_s for s in earlier if s.consistency_s]),
            _mean([s.consistency_s for s in latest if s.consistency_s]),
            CONSISTENCY_THRESHOLD_S,
        )

        sector_trends = []
        for index in (1, 2, 3):
            trend, _ = _trend(
                _mean([v for v in (s.best_sector(index) for s in earlier) if v]),
                _mean([v for v in (s.best_sector(index) for s in latest) if v]),
                TREND_THRESHOLD_S,
            )
            sector_trends.append(trend)

        def degradation(pool_slice: list[SessionRecord]) -> float | None:
            values = [
                stint.degradation_s_per_lap
                for session in pool_slice
                for stint in session.stints
                if stint.degradation_s_per_lap is not None
            ]
            return _mean(values)

        tyre, _ = _trend(
            degradation(earlier), degradation(latest), TREND_THRESHOLD_S / 5
        )

        return Progression(
            sessions=len(usable),
            pace=pace,
            pace_delta_s=pace_delta,
            consistency=consistency,
            consistency_delta_s=consistency_delta,
            sectors=(sector_trends[0], sector_trends[1], sector_trends[2]),
            tyre_management=tyre,
        )

    def tyre_history(
        self, sessions: list[SessionRecord] | None = None
    ) -> list[CompoundHistory]:
        """Per-compound history. Observed only - no profile data here."""
        pool = self.sessions if sessions is None else sessions
        by_compound: dict[str, list[StoredStint]] = {}
        laps_by_compound: dict[str, list[float]] = {}

        for session in pool:
            for stint in session.stints:
                if not stint.compound:
                    continue
                by_compound.setdefault(stint.compound, []).append(stint)
            for lap in session.valid_laps:
                if lap.compound:
                    laps_by_compound.setdefault(lap.compound, []).append(lap.lap_time_s)

        out = []
        for compound, stints in sorted(by_compound.items()):
            degradations = [
                stint.degradation_s_per_lap
                for stint in stints
                if stint.degradation_s_per_lap is not None
            ]
            times = laps_by_compound.get(compound, [])
            out.append(
                CompoundHistory(
                    compound=compound,
                    stints=len(stints),
                    average_length=_mean([float(s.laps) for s in stints]) or 0.0,
                    average_degradation=_mean(degradations),
                    best_lap_s=min(times) if times else None,
                    average_lap_s=_mean(times),
                    confidence=Confidence.from_samples(len(degradations)),
                )
            )
        return out


# ---------------------------------------------------------------------------
class SessionCollector:
    """Builds the record from the live session and keeps it safe on disk.

    Never mutates a completed lap, and never writes a None over a value
    that was previously known.
    """

    def __init__(self, mode: GameMode) -> None:
        self.mode = mode
        self._store = SessionStore(mode)
        self._record: SessionRecord | None = None
        self._state = SessionState.LIVE
        #: None until telemetry has been seen. Deliberately not 0.0:
        #: a clock legitimately reads 0.0, and testing it for
        #: truthiness would silently mean "never live".
        self._last_live: float | None = None

    # ------------------------------------------------------------------
    @property
    def record(self) -> SessionRecord | None:
        return self._record

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def store(self) -> SessionStore:
        return self._store

    def set_mode(self, mode: GameMode) -> None:
        """A different title ends the session and swaps the store."""
        self.finish()
        self.mode = mode
        self._store = SessionStore(mode)

    def _start(self, frame: TelemetryFrame, car_id: str, track_id: str) -> SessionRecord:
        record = SessionRecord(
            session_id=uuid.uuid4().hex,
            game_mode=self.mode.value,
            started_at=time.time(),
            car_id=car_id,
            track_id=track_id,
            session_type=SessionType.parse(frame.session_type).value,
            weather=frame.weather,
            track_temperature=frame.track_temperature,
            air_temperature=frame.air_temperature,
        )
        self._record = record
        self._state = SessionState.LIVE
        return record

    # ------------------------------------------------------------------
    def observe_frame(
        self, frame: TelemetryFrame, car_id: str = "", track_id: str = "",
        now: float | None = None,
    ) -> None:
        """Cheap per-frame notes. Starts a session on the first valid frame."""
        if not frame.valid:
            return
        now = time.monotonic() if now is None else now
        self._last_live = now

        if self._record is None or self._state is SessionState.FINISHED:
            self._start(frame, car_id, track_id)
        self._state = SessionState.LIVE

        record = self._record
        # Fill in details only where the game actually supplies them. A
        # blank field must never overwrite something already known.
        if frame.session_type:
            parsed = SessionType.parse(frame.session_type).value
            if record.session_type == SessionType.UNKNOWN.value:
                record.session_type = parsed
        if frame.weather:
            record.weather = frame.weather
        if frame.track_temperature:
            record.track_temperature = frame.track_temperature
        if frame.air_temperature:
            record.air_temperature = frame.air_temperature
        if frame.position:
            record.finish_position = frame.position

    def observe_lap(self, lap: LapRecord) -> None:
        """Append a completed lap and write the session to disk.

        Saving here rather than at shutdown is what makes an unexpected
        loss cost one lap instead of the session.
        """
        record = self._record
        if record is None:
            return
        if any(existing.lap_number == lap.lap_number for existing in record.laps):
            return  # already recorded; history is append-only
        record.laps.append(StoredLap.from_record(lap))
        self._store.save(record)

    def update_context(self, stints, observations, strategy_changes, recommended: str = "") -> None:
        """Refresh the derived sections. Called on lap completion."""
        record = self._record
        if record is None:
            return
        record.stints = [
            StoredStint(
                number=stint.number,
                compound=stint.compound,
                first_lap=stint.first_lap,
                last_lap=stint.last_lap,
                laps=stint.length,
                clean_laps=stint.clean_laps,
                degradation_s_per_lap=(
                    stint.degradation_s_per_lap if stint.has_degradation else None
                ),
                confidence=stint.degradation_confidence.name,
            )
            for stint in stints
        ]
        record.observations = [
            StoredObservation(
                id=item.id,
                category=item.category.value,
                sector=item.sector,
                first_detected_lap=item.first_detected_lap,
                occurrences=item.occurrences,
                peak_loss_s=item.peak_loss_s,
                current_loss_s=item.current_loss_s,
                status=item.status.value,
            )
            for item in observations
        ]
        record.strategy_changes = [
            StoredStrategyChange(
                lap=change.lap,
                previous=change.previous,
                current=change.current,
                reason=change.reason,
            )
            for change in strategy_changes
        ]
        if recommended:
            record.recommended_strategy = recommended

    def tick(self, live: bool, now: float | None = None) -> None:
        """Track LIVE/STALE, and close a session that has gone quiet.

        Going stale changes the *state* and nothing else. Every lap already
        recorded stays exactly as it was.
        """
        if self._record is None or self._state is SessionState.FINISHED:
            return
        now = time.monotonic() if now is None else now
        if live:
            self._last_live = now
            self._state = SessionState.LIVE
            return

        self._state = SessionState.STALE
        if self._last_live is not None and (now - self._last_live) >= STALE_CLOSE_S:
            # The game is not coming back; save rather than wait forever.
            self.finish()

    def finish(self) -> SessionRecord | None:
        """End the session and write it. Safe to call more than once."""
        record = self._record
        if record is None:
            return None
        if self._state is SessionState.FINISHED:
            return record
        record.state = SessionState.FINISHED.value
        record.ended_at = time.time()
        self._state = SessionState.FINISHED
        # A session with no laps is not worth keeping, but one with laps is
        # kept even if it ended badly.
        if record.laps:
            self._store.save(record)
        self._record = None
        return record

    # ------------------------------------------------------------------
    def history(self) -> HistoryAnalysis:
        """Everything stored for this mode, plus the session in progress."""
        stored = self._store.load_all()
        if self._record is not None and self._record.laps:
            stored = [self._record] + [
                s for s in stored if s.session_id != self._record.session_id
            ]
        return HistoryAnalysis(stored)
