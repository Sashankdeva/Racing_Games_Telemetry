"""Race Intelligence - what is objectively happening around the driver.

Sits between the normalized telemetry and Smart Suggestions:

    telemetry -> normalized state -> RACE INTELLIGENCE -> RaceState
                                                            |
                                                  Smart Suggestions -> UI

This module states facts. It never decides what is worth saying and never
chooses wording - that is the suggestion engine's job, and keeping the two
apart is what stops race logic leaking into widgets.

What this module can and cannot see
-----------------------------------

The F1 UDP feed carries lap data for every car, but the parser currently
decodes only the player's slice. That leaves this module with `position`,
`delta_to_car_ahead_s` and `delta_to_leader_s` and nothing else about
anyone else. So, deliberately:

  * gaps and closing rate to the car AHEAD are real and computed
  * the car BEHIND, opponent identity, opponent tyres and grid position are
    reported UNAVAILABLE - not guessed, not defaulted to zero
  * defence state is therefore UNKNOWN, because a threat cannot be measured
    without knowing where the car behind is

Parsing every car's lap data would unlock those, and the model below has
the fields ready for it. Until then they stay honest.

Noise
-----

A single frame never changes a state. Gaps are sampled once per completed
lap - which is the only cadence at which a gap means anything - and rates
come from a least-squares fit across several laps. Every state also needs
its condition to persist before it flips, so one bad reading cannot raise
an attack, a threat or an overtake.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from app.core.models import TelemetryFrame
from app.domain.lap_analysis import Confidence, LapAnalysis
from app.domain.stints import TyreState
from app.games.modes import Capability, GameProfile

#: Gap samples needed before any rate is reported.
MIN_GAP_SAMPLES = 3
#: Laps of gap history kept for the fit.
GAP_WINDOW = 5
#: Seconds per lap beyond which a gap is genuinely closing or opening.
#: Below this the gap is stable - lap-to-lap scatter is easily 0.05s.
GAP_RATE_THRESHOLD = 0.05
#: Consecutive confirmations before a state change is accepted.
STATE_CONFIRMATIONS = 2

#: Gap at which the car ahead is close enough to be attacked.
ATTACK_RANGE_S = 1.0
#: Gap at which an attack is developing.
ATTACK_APPROACH_S = 2.5
#: Gap below which a car ahead is being actively raced.
ACTIVE_ATTACK_S = 0.6
#: Ahead-gap under this counts as being in traffic.
TRAFFIC_CLOSE_S = 3.0

#: Fractions of race distance that separate the phases.
EARLY_RACE_FRACTION = 0.25
LATE_RACE_FRACTION = 0.75
#: Laps from the end that count as the final laps.
FINAL_LAPS = 3


class Availability(str, Enum):
    """Why a field has no value, when it has none."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNCONFIRMED = "UNCONFIRMED"


class GapTrend(str, Enum):
    CLOSING = "CLOSING"
    STABLE = "STABLE"
    OPENING = "OPENING"
    UNKNOWN = "UNKNOWN"


class AttackState(str, Enum):
    NO_ATTACK = "NO_ATTACK"
    APPROACHING = "APPROACHING"
    ATTACK_RANGE = "ATTACK_RANGE"
    ACTIVE_ATTACK = "ACTIVE_ATTACK"
    ATTACK_COMPLETED = "ATTACK_COMPLETED"
    ATTACK_LOST = "ATTACK_LOST"


class DefenceState(str, Enum):
    NO_THREAT = "NO_THREAT"
    THREAT_APPROACHING = "THREAT_APPROACHING"
    DEFENCE_RANGE = "DEFENCE_RANGE"
    ACTIVE_DEFENCE = "ACTIVE_DEFENCE"
    THREAT_LOST = "THREAT_LOST"
    #: No car-behind telemetry is parsed, so a threat cannot be measured.
    UNKNOWN = "UNKNOWN"


class DrsState(str, Enum):
    UNKNOWN = "UNKNOWN"
    UNAVAILABLE = "UNAVAILABLE"
    UNCONFIRMED = "UNCONFIRMED"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    OPPORTUNITY = "OPPORTUNITY"
    IN_RANGE = "IN_RANGE"
    ACTIVE = "ACTIVE"


class TrafficState(str, Enum):
    CLEAR = "CLEAR"
    LIGHT_TRAFFIC = "LIGHT_TRAFFIC"
    HEAVY_TRAFFIC = "HEAVY_TRAFFIC"
    UNKNOWN = "UNKNOWN"


class RacePhase(str, Enum):
    START = "START"
    EARLY_RACE = "EARLY_RACE"
    MID_RACE = "MID_RACE"
    LATE_RACE = "LATE_RACE"
    FINAL_LAPS = "FINAL_LAPS"
    FINISHED = "FINISHED"
    UNKNOWN = "UNKNOWN"


class NeutralisedState(str, Enum):
    NORMAL = "NORMAL"
    VSC = "VSC"
    SAFETY_CAR = "SAFETY_CAR"
    RED_FLAG = "RED_FLAG"
    UNKNOWN = "UNKNOWN"


class Trend(str, Enum):
    IMPROVING = "IMPROVING"
    STABLE = "STABLE"
    DECLINING = "DECLINING"
    UNKNOWN = "UNKNOWN"


class EventType(str, Enum):
    OVERTAKE = "OVERTAKE"
    BEEN_OVERTAKEN = "BEEN_OVERTAKEN"
    PIT_ENTRY = "PIT_ENTRY"
    PIT_EXIT = "PIT_EXIT"
    PIT_POSITION_CHANGE = "PIT_POSITION_CHANGE"
    DRS_RANGE_ENTERED = "DRS_RANGE_ENTERED"
    DRS_RANGE_LEFT = "DRS_RANGE_LEFT"
    ATTACK_DETECTED = "ATTACK_DETECTED"
    THREAT_DETECTED = "THREAT_DETECTED"
    SAFETY_CAR = "SAFETY_CAR"
    VSC = "VSC"
    LAP_COMPLETED = "LAP_COMPLETED"


@dataclass(frozen=True, slots=True)
class RaceEvent:
    """Something that happened, with enough context to review it later."""

    type: EventType
    lap: int
    timestamp: float
    position_from: int = 0
    position_to: int = 0
    detail: str = ""
    #: The numbers behind it, for auditing.
    data: dict = field(default_factory=dict)

    def describe(self) -> str:
        if self.position_from and self.position_to:
            return (
                f"L{self.lap} {self.type.value} "
                f"P{self.position_from}->P{self.position_to}"
            )
        return f"L{self.lap} {self.type.value}{' ' + self.detail if self.detail else ''}"


@dataclass(frozen=True, slots=True)
class GapInfo:
    """One neighbour's gap and how it is moving."""

    available: bool = False
    gap_s: float | None = None
    #: Seconds per lap. Positive = closing.
    rate_s_per_lap: float | None = None
    trend: GapTrend = GapTrend.UNKNOWN
    samples: int = 0
    confidence: Confidence = Confidence.NO_DATA
    #: Why there is no value, when there is none.
    availability: Availability = Availability.UNAVAILABLE
    reason: str = ""

    @property
    def laps_to_contact(self) -> float | None:
        if not self.available or not self.gap_s or not self.rate_s_per_lap:
            return None
        if self.rate_s_per_lap <= 0:
            return None
        return self.gap_s / self.rate_s_per_lap


@dataclass(frozen=True, slots=True)
class RaceState:
    """The factual picture. No advice, no wording, no opinions."""

    # --- position -----------------------------------------------------
    position: int | None = None
    grid_position: int | None = None
    lap: int | None = None
    total_laps: int | None = None
    laps_remaining: int | None = None
    leader_gap_s: float | None = None

    # --- neighbours ---------------------------------------------------
    ahead: GapInfo = field(default_factory=GapInfo)
    behind: GapInfo = field(default_factory=GapInfo)

    # --- derived situation --------------------------------------------
    attack_state: AttackState = AttackState.NO_ATTACK
    defence_state: DefenceState = DefenceState.UNKNOWN
    drs_state: DrsState = DrsState.UNKNOWN
    drs_term: str = "DRS"
    traffic_state: TrafficState = TrafficState.UNKNOWN
    race_phase: RacePhase = RacePhase.UNKNOWN
    neutralised: NeutralisedState = NeutralisedState.UNKNOWN

    # --- trends --------------------------------------------------------
    pace_trend: Trend = Trend.UNKNOWN
    position_trend: Trend = Trend.UNKNOWN
    tyre_trend: Trend = Trend.UNKNOWN

    confidence: Confidence = Confidence.NO_DATA
    #: True when the state was built from telemetry that is no longer live.
    stale: bool = False

    @property
    def attacking(self) -> bool:
        return self.attack_state in (
            AttackState.ATTACK_RANGE, AttackState.ACTIVE_ATTACK
        )

    @property
    def has_position(self) -> bool:
        return bool(self.position)

    def summary(self) -> str:
        """One line, for logs and tests. Not for the UI."""
        if not self.has_position:
            return "no position data"
        parts = [f"P{self.position}"]
        if self.total_laps:
            parts.append(f"L{self.lap}/{self.total_laps}")
        if self.ahead.available and self.ahead.gap_s is not None:
            parts.append(f"ahead {self.ahead.gap_s:.2f}s {self.ahead.trend.value}")
        parts.append(self.attack_state.value)
        return "  ".join(parts)


class _GapHistory:
    """Per-lap gap samples and a least-squares rate over them."""

    def __init__(self, window: int = GAP_WINDOW) -> None:
        self._window = window
        self._samples: list[tuple[int, float]] = []
        self._last_lap = -1

    def reset(self) -> None:
        self._samples.clear()
        self._last_lap = -1

    def observe(self, lap: int, gap_s: float) -> None:
        """One reading per lap. A per-frame gap is far too noisy to fit."""
        if lap <= 0 or gap_s <= 0 or lap == self._last_lap:
            return
        self._last_lap = lap
        self._samples.append((lap, gap_s))
        if len(self._samples) > self._window:
            self._samples.pop(0)

    @property
    def samples(self) -> int:
        return len(self._samples)

    def rate(self) -> float | None:
        """Seconds per lap the gap is shrinking. Positive = closing."""
        if len(self._samples) < MIN_GAP_SAMPLES:
            return None
        laps = [float(lap) for lap, _ in self._samples]
        gaps = [gap for _, gap in self._samples]
        mean_lap = sum(laps) / len(laps)
        mean_gap = sum(gaps) / len(gaps)
        denominator = sum((lap - mean_lap) ** 2 for lap in laps)
        if not denominator:
            return None
        slope = sum(
            (lap - mean_lap) * (gap - mean_gap) for lap, gap in zip(laps, gaps)
        ) / denominator
        return -slope  # a shrinking gap has a negative slope

    def trend(self) -> GapTrend:
        rate = self.rate()
        if rate is None:
            return GapTrend.UNKNOWN
        if rate > GAP_RATE_THRESHOLD:
            return GapTrend.CLOSING
        if rate < -GAP_RATE_THRESHOLD:
            return GapTrend.OPENING
        return GapTrend.STABLE

    def confidence(self) -> Confidence:
        return Confidence.from_samples(len(self._samples))


class RaceIntelligence:
    """Builds RaceState from the frame stream. Facts only.

    `observe_frame` is cheap enough for telemetry rate; `observe_lap` does
    the per-lap sampling. `state()` assembles the picture on demand.
    """

    def __init__(self) -> None:
        self._ahead = _GapHistory()
        self._events: list[RaceEvent] = []

        self._position = 0
        self._grid_position = 0
        self._best_position = 0
        self._worst_position = 0
        self._position_history: list[tuple[int, int]] = []

        self._in_pits = False
        self._pit_laps: set[int] = set()
        self._lap = 0
        self._neutralised = NeutralisedState.UNKNOWN

        # Debounce: a state only changes once it has been seen repeatedly.
        self._pending_attack: tuple[AttackState, int] | None = None
        self._attack_state = AttackState.NO_ATTACK
        self._drs_in_range = False

    # ------------------------------------------------------------------
    def reset(self) -> None:
        """Only for a genuine new session - never on a telemetry dropout."""
        self.__init__()

    @property
    def events(self) -> list[RaceEvent]:
        """Newest first."""
        return list(reversed(self._events))

    def events_of(self, event_type: EventType) -> list[RaceEvent]:
        return [event for event in self._events if event.type is event_type]

    def _record(self, event: RaceEvent) -> None:
        self._events.append(event)

    # ------------------------------------------------------------------
    def observe_frame(self, frame: TelemetryFrame, now: float = 0.0) -> None:
        """Per-frame observation. Must stay cheap."""
        if not frame.valid:
            return

        self._lap = frame.current_lap or self._lap
        self._track_pits(frame, now)
        self._track_position(frame, now)
        self._track_neutralised(frame, now)

    def observe_lap(self, lap_number: int, frame: TelemetryFrame, now: float = 0.0) -> None:
        """Called on lap completion - the only cadence at which a gap
        reading is meaningful."""
        self._ahead.observe(lap_number, frame.delta_to_car_ahead_s)
        self._record(
            RaceEvent(
                EventType.LAP_COMPLETED,
                lap=lap_number,
                timestamp=now,
                data={"gap_ahead_s": round(frame.delta_to_car_ahead_s, 3)},
            )
        )

    # --- per-frame trackers -------------------------------------------
    def _track_pits(self, frame: TelemetryFrame, now: float) -> None:
        if frame.in_pits and not self._in_pits:
            self._in_pits = True
            self._pit_laps.add(frame.current_lap)
            self._record(
                RaceEvent(EventType.PIT_ENTRY, frame.current_lap, now)
            )
        elif not frame.in_pits and self._in_pits:
            self._in_pits = False
            self._pit_laps.add(frame.current_lap)
            self._record(RaceEvent(EventType.PIT_EXIT, frame.current_lap, now))

    def _track_position(self, frame: TelemetryFrame, now: float) -> None:
        position = frame.position
        if not position:
            return

        if not self._position:
            # First reading of the session is the starting position. The
            # game does not send a grid slot, so this is the closest honest
            # equivalent and is labelled as such.
            self._position = position
            self._grid_position = position
            self._best_position = position
            self._worst_position = position
            self._position_history.append((frame.current_lap, position))
            return

        if position == self._position:
            return

        previous = self._position
        self._position = position
        self._best_position = min(self._best_position, position)
        self._worst_position = max(self._worst_position, position)
        self._position_history.append((frame.current_lap, position))

        # A position change during a pit phase is not an overtake. Calling
        # it one would put a fictional pass in the race history.
        pit_related = self._in_pits or frame.current_lap in self._pit_laps
        if pit_related:
            event_type = EventType.PIT_POSITION_CHANGE
        elif position < previous:
            event_type = EventType.OVERTAKE
        else:
            event_type = EventType.BEEN_OVERTAKEN

        self._record(
            RaceEvent(
                event_type,
                lap=frame.current_lap,
                timestamp=now,
                position_from=previous,
                position_to=position,
                detail="during a pit phase" if pit_related else "",
                data={"in_pits": frame.in_pits},
            )
        )

    def _track_neutralised(self, frame: TelemetryFrame, now: float) -> None:
        """Safety car / VSC / red flag.

        No adapter populates `safety_car` yet, so this stays UNKNOWN rather
        than asserting the race is running normally - which would be a
        claim we cannot support.
        """
        status = (frame.safety_car or "").strip().lower()
        if not status:
            self._neutralised = NeutralisedState.UNKNOWN
            return

        if "virtual" in status or status == "vsc":
            new_state = NeutralisedState.VSC
        elif "red" in status:
            new_state = NeutralisedState.RED_FLAG
        elif "safety" in status:
            new_state = NeutralisedState.SAFETY_CAR
        else:
            new_state = NeutralisedState.NORMAL

        if new_state is not self._neutralised:
            self._neutralised = new_state
            if new_state is NeutralisedState.SAFETY_CAR:
                self._record(RaceEvent(EventType.SAFETY_CAR, self._lap, now))
            elif new_state is NeutralisedState.VSC:
                self._record(RaceEvent(EventType.VSC, self._lap, now))

    # --- derived states -------------------------------------------------
    def _ahead_info(self, frame: TelemetryFrame) -> GapInfo:
        gap = frame.delta_to_car_ahead_s
        if gap <= 0:
            return GapInfo(
                availability=Availability.UNAVAILABLE,
                reason="the game is not reporting a gap to the car ahead",
            )
        rate = self._ahead.rate()
        return GapInfo(
            available=True,
            gap_s=gap,
            rate_s_per_lap=rate,
            trend=self._ahead.trend(),
            samples=self._ahead.samples,
            confidence=self._ahead.confidence(),
            availability=Availability.AVAILABLE,
        )

    def _behind_info(self) -> GapInfo:
        """Always unavailable today - see the module docstring."""
        return GapInfo(
            availability=Availability.UNAVAILABLE,
            reason=(
                "only the player's lap data is parsed, so there is no gap to "
                "the car behind"
            ),
        )

    def _attack(self, ahead: GapInfo, now: float) -> AttackState:
        """Debounced: a state must be seen twice before it is accepted."""
        if not ahead.available or ahead.gap_s is None:
            candidate = AttackState.NO_ATTACK
        elif ahead.gap_s <= ACTIVE_ATTACK_S:
            candidate = AttackState.ACTIVE_ATTACK
        elif ahead.gap_s <= ATTACK_RANGE_S:
            candidate = AttackState.ATTACK_RANGE
        elif ahead.gap_s <= ATTACK_APPROACH_S and ahead.trend is GapTrend.CLOSING:
            candidate = AttackState.APPROACHING
        else:
            candidate = AttackState.NO_ATTACK

        if candidate is self._attack_state:
            self._pending_attack = None
            return self._attack_state

        pending, count = self._pending_attack or (candidate, 0)
        if pending is not candidate:
            self._pending_attack = (candidate, 1)
            return self._attack_state

        count += 1
        if count < STATE_CONFIRMATIONS:
            self._pending_attack = (candidate, count)
            return self._attack_state

        # Confirmed.
        self._pending_attack = None
        previous = self._attack_state
        self._attack_state = candidate
        if candidate in (AttackState.ATTACK_RANGE, AttackState.ACTIVE_ATTACK) and (
            previous not in (AttackState.ATTACK_RANGE, AttackState.ACTIVE_ATTACK)
        ):
            self._record(
                RaceEvent(
                    EventType.ATTACK_DETECTED,
                    lap=self._lap,
                    timestamp=now,
                    detail=candidate.value,
                    data={"gap_s": round(ahead.gap_s or 0.0, 3)},
                )
            )
        return self._attack_state

    def _drs(self, frame: TelemetryFrame, ahead: GapInfo, game: GameProfile | None,
             now: float) -> DrsState:
        """Only claims what the game actually supports."""
        if game is None:
            return DrsState.UNKNOWN

        capability = (
            Capability.ACTIVE_AERO if game.drs.has_active_aero else Capability.DRS
        )
        status = game.status(capability)
        if status == "unavailable":
            return DrsState.UNAVAILABLE
        if status == "unconfirmed":
            # F1 26's active aero telemetry is not verified - saying
            # "available" would be a claim we cannot back.
            return DrsState.UNCONFIRMED

        if frame.drs_active:
            state = DrsState.ACTIVE
        elif not ahead.available or ahead.gap_s is None:
            return DrsState.UNKNOWN
        elif ahead.gap_s <= game.drs.activation_gap_s:
            state = DrsState.IN_RANGE
        elif ahead.gap_s <= ATTACK_APPROACH_S and ahead.trend is GapTrend.CLOSING:
            state = DrsState.OPPORTUNITY
        else:
            state = DrsState.OUT_OF_RANGE

        in_range = state in (DrsState.IN_RANGE, DrsState.ACTIVE)
        if in_range != self._drs_in_range:
            self._drs_in_range = in_range
            self._record(
                RaceEvent(
                    EventType.DRS_RANGE_ENTERED if in_range
                    else EventType.DRS_RANGE_LEFT,
                    lap=self._lap,
                    timestamp=now,
                    data={"gap_s": round(ahead.gap_s or 0.0, 3)},
                )
            )
        return state

    def _traffic(self, ahead: GapInfo) -> TrafficState:
        """Conservative: HEAVY_TRAFFIC needs several cars, which we cannot see.

        With one gap the honest answer is only whether anyone is close.
        """
        if not ahead.available or ahead.gap_s is None:
            return TrafficState.UNKNOWN
        return (
            TrafficState.LIGHT_TRAFFIC
            if ahead.gap_s <= TRAFFIC_CLOSE_S
            else TrafficState.CLEAR
        )

    def _phase(self, frame: TelemetryFrame) -> RacePhase:
        lap, total = frame.current_lap, frame.total_laps
        if not lap:
            return RacePhase.UNKNOWN
        if not total:
            # Without a race distance any phase would be an assumption.
            return RacePhase.UNKNOWN
        if lap > total:
            return RacePhase.FINISHED
        if lap <= 1:
            return RacePhase.START
        if total - lap < FINAL_LAPS:
            return RacePhase.FINAL_LAPS

        progress = lap / total
        if progress <= EARLY_RACE_FRACTION:
            return RacePhase.EARLY_RACE
        if progress >= LATE_RACE_FRACTION:
            return RacePhase.LATE_RACE
        return RacePhase.MID_RACE

    def _position_trend(self) -> Trend:
        if len(self._position_history) < 2:
            return Trend.UNKNOWN
        first = self._position_history[0][1]
        latest = self._position_history[-1][1]
        if latest < first:
            return Trend.IMPROVING
        if latest > first:
            return Trend.DECLINING
        return Trend.STABLE

    def _pace_trend(self, analysis: LapAnalysis) -> Trend:
        """Reads the lap analysis; never recomputes lap times."""
        if not analysis.has_pace or analysis.valid_laps < 3:
            return Trend.UNKNOWN
        delta = analysis.delta_to_previous_s
        if delta < -0.05:
            return Trend.IMPROVING
        if delta > 0.05:
            return Trend.DECLINING
        return Trend.STABLE

    def _tyre_trend(self, tyres: TyreState) -> Trend:
        """Reads the stint model's degradation - does not recompute it."""
        if not tyres.degradation_confidence.is_usable:
            return Trend.UNKNOWN
        if tyres.degradation_s_per_lap > GAP_RATE_THRESHOLD:
            return Trend.DECLINING
        if tyres.degradation_s_per_lap < -GAP_RATE_THRESHOLD:
            return Trend.IMPROVING
        return Trend.STABLE

    # ------------------------------------------------------------------
    def state(
        self,
        frame: TelemetryFrame,
        analysis: LapAnalysis,
        tyres: TyreState,
        game: GameProfile | None = None,
        *,
        live: bool = True,
        now: float = 0.0,
    ) -> RaceState:
        """Assemble the current picture.

        Cheap enough to call at UI rate; all the accumulation happened in
        `observe_frame` / `observe_lap`.
        """
        ahead = self._ahead_info(frame)
        behind = self._behind_info()

        # State machines only advance on live data - a dropout must not be
        # read as the car ahead vanishing.
        if live:
            attack = self._attack(ahead, now)
            drs = self._drs(frame, ahead, game, now)
        else:
            attack = self._attack_state
            drs = DrsState.UNKNOWN

        total = frame.total_laps or None
        lap = frame.current_lap or None
        return RaceState(
            position=frame.position or None,
            grid_position=self._grid_position or None,
            lap=lap,
            total_laps=total,
            laps_remaining=(total - lap) if (total and lap) else None,
            leader_gap_s=frame.delta_to_leader_s or None,
            ahead=ahead,
            behind=behind,
            attack_state=attack,
            # Cannot be measured without the car behind.
            defence_state=DefenceState.UNKNOWN,
            drs_state=drs,
            drs_term=game.term("drs") if game else "DRS",
            traffic_state=self._traffic(ahead),
            race_phase=self._phase(frame),
            neutralised=self._neutralised,
            pace_trend=self._pace_trend(analysis),
            position_trend=self._position_trend(),
            tyre_trend=self._tyre_trend(tyres),
            confidence=ahead.confidence,
            stale=not live,
        )
