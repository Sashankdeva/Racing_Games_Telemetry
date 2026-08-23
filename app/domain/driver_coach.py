"""Driver Coach - how the driver is performing.

    telemetry -> lap/sector analysis -> DRIVER COACH -> observations
                                                            |
                                                  Smart Suggestions -> UI

Produces structured observations about driving. It does not decide when to
say them and never touches the UI - that separation is what keeps coaching
out of widgets and wording out of analysis.

What it may and may not claim
-----------------------------

There is no corner metadata and no reference lap in the telemetry, so this
module can never say "brake at 100m" or "Turn 8". The unit of analysis is
the **sector**, because `frame.sector` is a real field the game sends.
Regions within a sector are described in plain terms ("entry", "exit") and
are explicitly proxies, defined below.

Every observation is tagged with how much it is worth:

    OBSERVED   measured directly - a sector time is slower than the
               driver's own best for that sector
    INFERRED   a correlation between an input pattern and lap time. Real
               evidence, but not proof of cause, so the wording says
               "potential" and never asserts why
    UNKNOWN    the telemetry needed is absent; nothing is said at all

Entry and exit are proxies, and are labelled as such:

    entry  what the brakes did through the sector - how long, how hard
    exit   how much of the sector was spent at full throttle

Those are honest summaries of real inputs. They are not corner-resolved and
this module never pretends otherwise.

Noise
-----

Observations are produced on lap completion only, never per frame, and a
single slow lap is never coached. A problem must repeat across a window of
laps before it is raised, which is what stops a lock-up on one lap becoming
a lecture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum

from app.core.models import TelemetryFrame
from app.domain.driver_session import LapRecord
from app.domain.lap_analysis import Confidence, LapAnalysis
from app.domain.profile_intelligence import ProfileContext

#: Laps needed before anything is coached. Below this there is no personal
#: best worth comparing against.
MIN_LAPS_FOR_COACHING = 4
#: Sector loss under this is not worth a driver's attention.
SECTOR_LOSS_THRESHOLD_S = 0.10
#: How many recent laps a repeat is judged over, and how many of them must
#: show the problem before it is raised.
REPEAT_WINDOW = 6
REPEAT_MIN = 3
#: Laps either side used to judge whether a sector is improving.
IMPROVEMENT_WINDOW = 3
#: Improvement smaller than this is noise, not progress.
IMPROVEMENT_THRESHOLD_S = 0.08
#: A problem is resolved once the loss falls below this.
RESOLVED_THRESHOLD_S = 0.05
#: Minimum fast and slow laps needed before an input is correlated.
MIN_CORRELATION_SAMPLES = 2
#: Input differences below these are not worth reporting.
MIN_THROTTLE_DIFF = 0.04
MIN_BRAKE_DIFF = 0.04
MIN_STEER_DIFF = 0.03
#: Steering movement above this counts as a correction.
STEER_REVERSAL_DEADZONE = 0.06


class Category(str, Enum):
    BRAKING = "BRAKING"
    THROTTLE = "THROTTLE"
    STEERING = "STEERING"
    CORNER_ENTRY = "CORNER_ENTRY"
    CORNER_EXIT = "CORNER_EXIT"
    ACCELERATION = "ACCELERATION"
    CONSISTENCY = "CONSISTENCY"
    PACE = "PACE"


class EvidenceKind(str, Enum):
    OBSERVED = "OBSERVED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"


class Severity(IntEnum):
    INFO = 0
    ADVISORY = 1
    WARNING = 2

    @property
    def label(self) -> str:
        return self.name


class Status(str, Enum):
    ACTIVE = "ACTIVE"
    IMPROVING = "IMPROVING"
    RESOLVED = "RESOLVED"


@dataclass(slots=True)
class SectorInputs:
    """Driver inputs aggregated over one sector of one lap.

    Accumulated per frame, which is cheap; nothing is analysed here.
    """

    samples: int = 0
    throttle_sum: float = 0.0
    brake_sum: float = 0.0
    steer_abs_sum: float = 0.0
    full_throttle_samples: int = 0
    braking_samples: int = 0
    peak_brake: float = 0.0
    #: Steering direction changes above the deadzone - "corrections".
    reversals: int = 0
    _last_steer: float = 0.0

    def observe(self, frame: TelemetryFrame) -> None:
        self.samples += 1
        self.throttle_sum += frame.throttle
        self.brake_sum += frame.brake
        self.steer_abs_sum += abs(frame.steering)
        if frame.throttle >= 0.98:
            self.full_throttle_samples += 1
        if frame.brake > 0.05:
            self.braking_samples += 1
            self.peak_brake = max(self.peak_brake, frame.brake)

        if (
            abs(frame.steering - self._last_steer) > STEER_REVERSAL_DEADZONE
            and frame.steering * self._last_steer < 0
        ):
            self.reversals += 1
        self._last_steer = frame.steering

    def _ratio(self, count: int) -> float:
        return count / self.samples if self.samples else 0.0

    @property
    def valid(self) -> bool:
        # A handful of frames is not a sector.
        return self.samples >= 10

    @property
    def full_throttle_ratio(self) -> float:
        return self._ratio(self.full_throttle_samples)

    @property
    def braking_ratio(self) -> float:
        return self._ratio(self.braking_samples)

    @property
    def mean_throttle(self) -> float:
        return self.throttle_sum / self.samples if self.samples else 0.0

    @property
    def mean_brake(self) -> float:
        return self.brake_sum / self.samples if self.samples else 0.0

    @property
    def mean_abs_steer(self) -> float:
        return self.steer_abs_sum / self.samples if self.samples else 0.0


@dataclass(slots=True)
class LapInputs:
    """One lap's inputs, split by the game's own sector field."""

    lap_number: int = 0
    sectors: dict[int, SectorInputs] = field(default_factory=dict)

    def sector(self, index: int) -> SectorInputs:
        return self.sectors.setdefault(index, SectorInputs())


@dataclass(frozen=True, slots=True)
class DrivingObservation:
    """One thing noticed about the driving, and how much it is worth."""

    id: str
    lap: int
    sector: int
    region: str
    category: Category
    observation: str
    evidence: str
    evidence_kind: EvidenceKind
    severity: Severity
    confidence: Confidence
    #: Seconds. "Potential" unless the evidence is OBSERVED.
    time_loss_s: float = 0.0
    repeat_count: int = 0
    timestamp: float = 0.0
    status: Status = Status.ACTIVE
    source_data: dict = field(default_factory=dict)

    @property
    def corner_or_region(self) -> str:
        """Sectors, never corner numbers - there is no corner metadata."""
        return f"Sector {self.sector} {self.region}".strip()

    def describe(self) -> str:
        return f"{self.corner_or_region}: {self.observation}"


@dataclass(slots=True)
class ProblemRecord:
    """A problem tracked across the session, for progression."""

    id: str
    sector: int
    category: Category
    first_detected_lap: int
    last_seen_lap: int
    peak_loss_s: float
    current_loss_s: float
    occurrences: int = 0
    status: Status = Status.ACTIVE

    @property
    def improvement_s(self) -> float:
        return max(0.0, self.peak_loss_s - self.current_loss_s)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


class DriverCoach:
    """Analyses driving. Produces observations; says nothing itself."""

    def __init__(self) -> None:
        self._current = LapInputs()
        self._laps: list[LapInputs] = []
        self._records: list[LapRecord] = []
        self._observations: list[DrivingObservation] = []
        self._problems: dict[str, ProblemRecord] = {}
        self._last_lap_seen = 0

    # ------------------------------------------------------------------
    def reset(self) -> None:
        """Only for a genuine new session - never on a dropout."""
        self.__init__()

    @property
    def observations(self) -> list[DrivingObservation]:
        """Active observations, most serious first."""
        return sorted(
            (o for o in self._observations if o.status is not Status.RESOLVED),
            key=lambda o: (int(o.severity), o.time_loss_s),
            reverse=True,
        )

    @property
    def focus(self) -> DrivingObservation | None:
        """The one thing most worth working on."""
        active = self.observations
        return active[0] if active else None

    @property
    def problems(self) -> list[ProblemRecord]:
        """Progression across the session, newest problem first."""
        return sorted(
            self._problems.values(), key=lambda p: p.last_seen_lap, reverse=True
        )

    @property
    def improvements(self) -> list[ProblemRecord]:
        return [
            p for p in self.problems
            if p.status in (Status.IMPROVING, Status.RESOLVED)
        ]

    # ------------------------------------------------------------------
    def observe_frame(self, frame: TelemetryFrame) -> None:
        """Accumulate inputs. Cheap enough for telemetry rate."""
        if not frame.valid:
            return
        lap = frame.current_lap or 0
        if lap and lap != self._current.lap_number:
            # The lap counter moved; start a fresh accumulator. The finished
            # one is banked by observe_lap when its time arrives.
            self._current = LapInputs(lap_number=lap)
        # `sector` is the game's own field - not derived from distance.
        self._current.sector(frame.sector).observe(frame)

    def observe_lap(
        self, record: LapRecord, analysis: LapAnalysis, now: float = 0.0,
        context: ProfileContext | None = None,
    ) -> list[DrivingObservation]:
        """Analyse a completed lap. Returns the observations it produced."""
        banked = self._current
        banked.lap_number = record.lap_number
        self._laps.append(banked)
        self._records.append(record)
        self._current = LapInputs()

        if len(self._records) < MIN_LAPS_FOR_COACHING:
            return []

        produced: list[DrivingObservation] = []
        for sector in (1, 2, 3):
            produced.extend(self._analyse_sector(sector, analysis, record, now))

        if context is not None:
            produced = [self._apply_context(o, context) for o in produced]

        consistency = self._analyse_consistency(analysis, record, now)
        if consistency is not None:
            produced.append(consistency)

        self._merge(produced)
        return produced

    # --- sector analysis ------------------------------------------------
    def _sector_times(self, sector: int) -> list[tuple[int, float]]:
        """(lap number, sector time) for laps that count towards pace."""
        index = sector - 1
        out = []
        for record in self._records:
            if not record.valid_for_pace:
                continue
            value = (record.sector1_s, record.sector2_s, record.sector3_s)[index]
            if value > 0:
                out.append((record.lap_number, value))
        return out

    def _inputs_for(self, lap_number: int, sector: int) -> SectorInputs | None:
        for lap in self._laps:
            if lap.lap_number == lap_number:
                inputs = lap.sectors.get(sector - 1)
                return inputs if inputs is not None and inputs.valid else None
        return None

    def _analyse_sector(
        self, sector: int, analysis: LapAnalysis, record: LapRecord, now: float
    ) -> list[DrivingObservation]:
        times = self._sector_times(sector)
        if len(times) < MIN_LAPS_FOR_COACHING:
            return []

        best = min(value for _, value in times)
        recent = times[-REPEAT_WINDOW:]
        slow = [(lap, value) for lap, value in recent if value - best > SECTOR_LOSS_THRESHOLD_S]
        if len(slow) < REPEAT_MIN:
            return []

        latest_lap, latest_time = times[-1]
        # The reported loss is the MEAN across the slow laps, not the last
        # lap's. Using the latest lap makes a recovering driver read
        # "repeatedly slower - loss 0.000s", which is both confusing and a
        # worse summary of what is actually costing time.
        loss = _mean([value - best for _, value in slow])
        latest_loss = latest_time - best
        confidence = Confidence.from_samples(len(times))
        repeat = len(slow)

        source = {
            "sector": sector,
            "best_s": round(best, 3),
            "latest_s": round(latest_time, 3),
            "latest_loss_s": round(latest_loss, 3),
            "mean_loss_s": round(loss, 3),
            "slow_laps": repeat,
            "window": len(recent),
            "samples": len(times),
        }

        observations = [
            DrivingObservation(
                id=f"pace.s{sector}",
                lap=latest_lap,
                sector=sector,
                region="",
                category=Category.PACE,
                observation=(
                    f"Sector {sector} is repeatedly slower than your best."
                ),
                evidence=(
                    f"Slower than your {best:.3f}s best on {repeat} of the last "
                    f"{len(recent)} laps, by {loss:.3f}s on average; latest "
                    f"{latest_time:.3f}s ({latest_loss:+.3f}s)."
                ),
                # A sector time against the driver's own best is measured.
                evidence_kind=EvidenceKind.OBSERVED,
                severity=Severity.WARNING if loss > 0.30 else Severity.ADVISORY,
                confidence=confidence,
                time_loss_s=round(loss, 3),
                repeat_count=repeat,
                timestamp=now,
                source_data=source,
            )
        ]

        correlated = self._correlate_inputs(
            sector, times, best, latest_lap, loss, confidence, now
        )
        observations.extend(correlated)
        return observations

    def _correlate_inputs(
        self, sector: int, times: list[tuple[int, float]], best: float,
        latest_lap: int, loss: float, confidence: Confidence, now: float,
    ) -> list[DrivingObservation]:
        """Compare inputs on the slow laps against the quick ones.

        This is the only place an *inference* is made, and it is labelled
        as one. A difference in inputs that coincides with lost time is
        evidence worth showing; it is not proof of cause, so nothing here
        asserts why - only what differed.
        """
        fast_laps = [lap for lap, value in times if value - best <= SECTOR_LOSS_THRESHOLD_S]
        slow_laps = [lap for lap, value in times if value - best > SECTOR_LOSS_THRESHOLD_S]

        fast = [i for i in (self._inputs_for(lap, sector) for lap in fast_laps) if i]
        slow = [i for i in (self._inputs_for(lap, sector) for lap in slow_laps) if i]
        if len(fast) < MIN_CORRELATION_SAMPLES or len(slow) < MIN_CORRELATION_SAMPLES:
            # Not enough of each to compare - say nothing rather than guess.
            return []

        out: list[DrivingObservation] = []

        # --- corner exit proxy: time spent at full throttle ---------------
        fast_wot = _mean([i.full_throttle_ratio for i in fast])
        slow_wot = _mean([i.full_throttle_ratio for i in slow])
        if fast_wot - slow_wot >= MIN_THROTTLE_DIFF:
            out.append(
                DrivingObservation(
                    id=f"exit.s{sector}",
                    lap=latest_lap,
                    sector=sector,
                    region="exit",
                    category=Category.CORNER_EXIT,
                    observation=(
                        f"Throttle is applied later in sector {sector} on your "
                        "slower laps."
                    ),
                    evidence=(
                        f"Full throttle for {slow_wot:.0%} of the sector on the "
                        f"slower laps against {fast_wot:.0%} on the quicker ones."
                    ),
                    evidence_kind=EvidenceKind.INFERRED,
                    severity=Severity.ADVISORY,
                    confidence=confidence,
                    time_loss_s=round(loss, 3),
                    repeat_count=len(slow),
                    timestamp=now,
                    source_data={
                        "sector": sector,
                        "full_throttle_fast": round(fast_wot, 3),
                        "full_throttle_slow": round(slow_wot, 3),
                        "fast_laps": len(fast),
                        "slow_laps": len(slow),
                    },
                )
            )

        # --- corner entry proxy: what the brakes did ----------------------
        fast_brake = _mean([i.braking_ratio for i in fast])
        slow_brake = _mean([i.braking_ratio for i in slow])
        if abs(slow_brake - fast_brake) >= MIN_BRAKE_DIFF:
            longer = slow_brake > fast_brake
            out.append(
                DrivingObservation(
                    id=f"entry.s{sector}",
                    lap=latest_lap,
                    sector=sector,
                    region="entry",
                    category=Category.BRAKING,
                    observation=(
                        f"You spend {'more' if longer else 'less'} of sector "
                        f"{sector} on the brakes when the sector is slower."
                    ),
                    evidence=(
                        f"On the brakes for {slow_brake:.0%} of the sector on the "
                        f"slower laps against {fast_brake:.0%} on the quicker ones. "
                        "Whether that is cause or consequence is not measurable "
                        "from this telemetry."
                    ),
                    evidence_kind=EvidenceKind.INFERRED,
                    severity=Severity.ADVISORY,
                    confidence=confidence,
                    time_loss_s=round(loss, 3),
                    repeat_count=len(slow),
                    timestamp=now,
                    source_data={
                        "sector": sector,
                        "braking_ratio_fast": round(fast_brake, 3),
                        "braking_ratio_slow": round(slow_brake, 3),
                    },
                )
            )

        # --- steering corrections ----------------------------------------
        fast_steer = _mean([float(i.reversals) for i in fast])
        slow_steer = _mean([float(i.reversals) for i in slow])
        if slow_steer - fast_steer >= 1.0:
            out.append(
                DrivingObservation(
                    id=f"steer.s{sector}",
                    lap=latest_lap,
                    sector=sector,
                    region="",
                    category=Category.STEERING,
                    observation=(
                        f"More steering corrections in sector {sector} on your "
                        "slower laps."
                    ),
                    evidence=(
                        f"{slow_steer:.1f} direction changes per lap against "
                        f"{fast_steer:.1f} on the quicker laps."
                    ),
                    evidence_kind=EvidenceKind.INFERRED,
                    severity=Severity.INFO,
                    confidence=confidence,
                    time_loss_s=round(loss, 3),
                    repeat_count=len(slow),
                    timestamp=now,
                    source_data={
                        "sector": sector,
                        "reversals_fast": round(fast_steer, 2),
                        "reversals_slow": round(slow_steer, 2),
                    },
                )
            )
        return out

    def _apply_context(
        self, observation: DrivingObservation, context: ProfileContext
    ) -> DrivingObservation:
        """Let a KNOWN track characteristic reinforce a braking observation.

        Only when the track profile is a real, user-verified figure - a
        shipped prior is a starting assumption and must not be used to make
        a coaching claim sound better supported than it is.
        """
        if observation.category is not Category.BRAKING:
            return observation
        severity = context.rating("braking_severity", of="track")
        if not severity.known or severity.confidence is Confidence.LOW:
            return observation
        if float(severity.value) < 65.0:
            return observation

        from dataclasses import replace

        name = context.track.name if context.track else "this circuit"
        return replace(
            observation,
            evidence=(
                f"{observation.evidence} {name} is a heavy-braking circuit "
                f"({severity.value:.0f}/100), which supports this reading."
            ),
            source_data={
                **observation.source_data,
                "track_braking_severity": severity.value,
                "track_source": severity.source.value,
            },
        )

    # --- consistency -----------------------------------------------------
    def _analyse_consistency(
        self, analysis: LapAnalysis, record: LapRecord, now: float
    ) -> DrivingObservation | None:
        if analysis.valid_laps < MIN_LAPS_FOR_COACHING or analysis.consistency_s <= 0:
            return None
        # Only worth raising when the spread is large relative to the pace.
        if analysis.best_lap_s <= 0:
            return None
        spread_ratio = analysis.consistency_s / analysis.best_lap_s
        if spread_ratio < 0.004:  # under ~0.4% of a lap is tidy driving
            return None

        return DrivingObservation(
            id="consistency",
            lap=record.lap_number,
            sector=0,
            region="",
            category=Category.CONSISTENCY,
            observation="Lap times are varying more than usual.",
            evidence=(
                f"Standard deviation {analysis.consistency_s:.3f}s across "
                f"{analysis.valid_laps} valid laps."
            ),
            evidence_kind=EvidenceKind.OBSERVED,
            severity=Severity.INFO,
            confidence=analysis.confidence,
            time_loss_s=round(analysis.consistency_s, 3),
            repeat_count=analysis.valid_laps,
            timestamp=now,
            source_data={
                "consistency_s": round(analysis.consistency_s, 3),
                "valid_laps": analysis.valid_laps,
                "best_lap_s": round(analysis.best_lap_s, 3),
            },
        )

    # --- improvement and lifecycle ---------------------------------------
    def sector_trend(self, sector: int) -> tuple[str, float, Confidence]:
        """Whether a sector is improving, from two windows of laps."""
        times = [value for _, value in self._sector_times(sector)]
        if len(times) < IMPROVEMENT_WINDOW * 2:
            return ("UNKNOWN", 0.0, Confidence.INSUFFICIENT)

        earlier = _mean(times[-IMPROVEMENT_WINDOW * 2 : -IMPROVEMENT_WINDOW])
        latest = _mean(times[-IMPROVEMENT_WINDOW:])
        delta = earlier - latest  # positive = quicker now
        confidence = Confidence.from_samples(len(times))

        if delta >= IMPROVEMENT_THRESHOLD_S:
            return ("IMPROVING", round(delta, 3), confidence)
        if delta <= -IMPROVEMENT_THRESHOLD_S:
            return ("DECLINING", round(delta, 3), confidence)
        return ("STABLE", round(delta, 3), confidence)

    def lap_comparison(self, analysis: LapAnalysis) -> list[dict]:
        """Per-sector delta of the last lap against the driver's bests."""
        out = []
        for index, delta in enumerate(analysis.sector_deltas):
            if not delta.available:
                continue
            out.append(
                {
                    "sector": delta.sector,
                    "time_s": round(delta.time_s, 3),
                    "best_s": round(delta.best_s, 3),
                    "delta_s": round(delta.delta_s, 3),
                    "is_best": delta.is_personal_best,
                }
            )
        return out

    def _merge(self, produced: list[DrivingObservation]) -> None:
        """Fold new observations into the tracked problems.

        A problem that stops appearing is marked RESOLVED rather than left
        on screen, and one that is shrinking is marked IMPROVING - both of
        which stop the same message being repeated forever.
        """
        seen = {obs.id for obs in produced}

        for obs in produced:
            record = self._problems.get(obs.id)
            if record is None:
                self._problems[obs.id] = ProblemRecord(
                    id=obs.id,
                    sector=obs.sector,
                    category=obs.category,
                    first_detected_lap=obs.lap,
                    last_seen_lap=obs.lap,
                    peak_loss_s=obs.time_loss_s,
                    current_loss_s=obs.time_loss_s,
                    occurrences=1,
                )
            else:
                record.last_seen_lap = obs.lap
                record.peak_loss_s = max(record.peak_loss_s, obs.time_loss_s)
                record.current_loss_s = obs.time_loss_s
                record.occurrences += 1
                record.status = (
                    Status.IMPROVING
                    if obs.time_loss_s < record.peak_loss_s - IMPROVEMENT_THRESHOLD_S
                    else Status.ACTIVE
                )

        # Anything no longer produced has gone away.
        for key, record in self._problems.items():
            if key not in seen and record.status is not Status.RESOLVED:
                record.status = Status.RESOLVED
                record.current_loss_s = 0.0

        statuses = {key: record.status for key, record in self._problems.items()}
        self._observations = [
            _with_status(obs, statuses.get(obs.id, Status.ACTIVE))
            for obs in produced
        ]


def _with_status(obs: DrivingObservation, status: Status) -> DrivingObservation:
    from dataclasses import replace

    return replace(obs, status=status)
