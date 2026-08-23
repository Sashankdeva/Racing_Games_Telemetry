"""Smart Suggestions - the race-engineer layer.

Sits on top of the existing normalized telemetry and analysis. It owns no
telemetry, parses nothing, and recomputes nothing that lap analysis, the
stint model or the profiles already provide. Every rule is a pure function
of a `SuggestionContext`, which is why the whole thing replays
deterministically.

Three rules shape the design.

**Never invent.** A rule that cannot justify itself from real data returns
nothing. There is no "probably" here: no assumed degradation curves, no
guessed braking points, no safety-car logic while the safety-car field is
unparsed. Where a figure exists but is weakly supported, it travels with a
low confidence rather than being dressed up.

**Never spam.** Telemetry arrives 60 times a second and a condition like
"losing time in sector 2" holds for whole laps. Every suggestion therefore
has a key, a cooldown, hysteresis between its trigger and clear thresholds,
and a lifecycle - so it appears once, stays until the condition actually
goes away, and only returns if it genuinely recurs.

**Always explain.** Each suggestion carries `reason` and `source_data`: the
actual numbers the decision came from. That makes the system debuggable and
means the UI never has to guess at wording.

Determinism note: the clock is supplied by the caller and is derived from
the telemetry stream (frames observed), not from wall time. Replaying the
same recording produces identical suggestions regardless of playback speed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum

from app.core.models import TelemetryFrame
from app.domain.car_profiles import CarProfile
from app.domain.driver_coach import DrivingObservation
from app.domain.profile_intelligence import ProfileContext
from app.domain.lap_analysis import Confidence, LapAnalysis
from app.domain.race_intelligence import DrsState, GapTrend, RaceState
from app.domain.stints import Stint, TyreState
from app.domain.strategy import StrategyPlan
from app.domain.track_profiles import TrackProfile
from app.games.modes import GameProfile

#: Nominal telemetry rate, used to turn a frame count into seconds.
NOMINAL_HZ = 60.0


class Category(str, Enum):
    DRIVING = "DRIVING"
    PACE = "PACE"
    TYRE = "TYRE"
    STRATEGY = "STRATEGY"
    ERS = "ERS"
    DRS = "DRS"
    RACE = "RACE"
    FUEL = "FUEL"
    SAFETY = "SAFETY"


class Severity(IntEnum):
    INFO = 0
    ADVISORY = 1
    WARNING = 2
    CRITICAL = 3

    @property
    def label(self) -> str:
        return self.name


class Priority(IntEnum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2
    CRITICAL = 3

    @property
    def label(self) -> str:
        return self.name


class Lifecycle(str, Enum):
    TRIGGERED = "TRIGGERED"
    ACTIVE = "ACTIVE"
    UPDATED = "UPDATED"
    RESOLVED = "RESOLVED"
    EXPIRED = "EXPIRED"


#: Which priority a category carries. Safety outranks everything; general
#: information never crowds out an actionable message.
CATEGORY_PRIORITY: dict[Category, Priority] = {
    Category.SAFETY: Priority.CRITICAL,
    Category.STRATEGY: Priority.HIGH,
    Category.TYRE: Priority.HIGH,
    Category.FUEL: Priority.HIGH,
    Category.RACE: Priority.MEDIUM,
    Category.PACE: Priority.MEDIUM,
    Category.DRIVING: Priority.MEDIUM,
    Category.ERS: Priority.MEDIUM,
    Category.DRS: Priority.MEDIUM,
}

#: Seconds a suggestion is suppressed after it resolves, per category. Long
#: enough that a condition flickering around its threshold cannot produce a
#: stream of messages.
COOLDOWN_S: dict[Category, float] = {
    Category.SAFETY: 10.0,
    Category.STRATEGY: 120.0,
    Category.TYRE: 90.0,
    Category.FUEL: 90.0,
    Category.RACE: 45.0,
    Category.PACE: 60.0,
    Category.DRIVING: 60.0,
    Category.ERS: 30.0,
    Category.DRS: 20.0,
}

#: How long a suggestion stays up without being re-confirmed by its rule.
TTL_S: dict[Category, float] = {
    Category.SAFETY: 30.0,
    Category.STRATEGY: 300.0,
    Category.TYRE: 180.0,
    Category.FUEL: 180.0,
    Category.RACE: 60.0,
    Category.PACE: 120.0,
    Category.DRIVING: 120.0,
    Category.ERS: 30.0,
    Category.DRS: 15.0,
}

# --- thresholds, with hysteresis: trigger high, clear low ------------------
SECTOR_LOSS_TRIGGER_S = 0.15
SECTOR_LOSS_CLEAR_S = 0.08
TYRE_TEMP_TRIGGER_C = 110.0
TYRE_TEMP_CLEAR_C = 104.0
TYRE_WEAR_TRIGGER_PCT = 60.0
TYRE_WEAR_CLEAR_PCT = 55.0
DEGRADATION_TRIGGER_S = 0.06
ERS_LOW_TRIGGER_PCT = 12.0
ERS_LOW_CLEAR_PCT = 25.0
DRS_RANGE_S = 1.0
#: A gap this close is worth calling out as approaching DRS range.
DRS_APPROACH_S = 2.0
#: Minimum lap samples before a closing rate is anything but noise.
MIN_GAP_SAMPLES = 3
#: Laps of fuel margin below which it is worth mentioning.
FUEL_MARGIN_LAPS = 1.0


@dataclass(frozen=True, slots=True)
class Suggestion:
    """One thing worth telling the driver, and why."""

    id: str
    category: Category
    message: str
    reason: str
    severity: Severity
    confidence: Confidence
    priority: Priority
    #: Session clock when this was first raised.
    timestamp: float = 0.0
    cooldown: float = 0.0
    expires_at: float = 0.0
    state: Lifecycle = Lifecycle.TRIGGERED
    #: The actual numbers behind the decision, for display and debugging.
    source_data: dict = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.confidence.is_usable

    def describe(self) -> str:
        return f"[{self.severity.label}] {self.message} - {self.reason}"


@dataclass(slots=True)
class SuggestionContext:
    """Everything a rule may read. Assembled by the caller, never fetched
    here - which is what keeps the rules pure and replay deterministic."""

    frame: TelemetryFrame
    analysis: LapAnalysis
    tyres: TyreState
    stints: list[Stint] = field(default_factory=list)
    game: GameProfile | None = None
    car: CarProfile | None = None
    track: TrackProfile | None = None
    #: Session clock in seconds, derived from frames observed.
    now: float = 0.0
    live: bool = True
    #: Mean fuel burn per lap, measured. 0.0 when not yet known.
    fuel_per_lap: float = 0.0
    #: Facts from Race Intelligence. The engine reads these rather than
    #: measuring gaps itself - one calculation, one owner.
    race: RaceState = field(default_factory=RaceState)
    #: The ranked plan from the Strategy Engine. Same rule: the suggestion
    #: layer words it, it does not compute it.
    strategy: StrategyPlan = field(default_factory=StrategyPlan)
    #: Observations from the Driver Coach. Same rule again.
    coaching: list[DrivingObservation] = field(default_factory=list)
    #: Car/track context. Only derived signals are read from it - no
    #: database detail reaches the UI through here.
    profiles: ProfileContext | None = None

    @property
    def closing_rate_s(self) -> float:
        return self.race.ahead.rate_s_per_lap or 0.0

    @property
    def closing_samples(self) -> int:
        return self.race.ahead.samples

    @property
    def has_pace(self) -> bool:
        return self.analysis.has_pace and self.analysis.confidence.is_usable


# ---------------------------------------------------------------------------
# rules - each returns candidate suggestions, or nothing
# ---------------------------------------------------------------------------
def _make(
    key: str,
    category: Category,
    message: str,
    reason: str,
    severity: Severity,
    confidence: Confidence,
    source: dict,
) -> Suggestion:
    return Suggestion(
        id=key,
        category=category,
        message=message,
        reason=reason,
        severity=severity,
        confidence=confidence,
        priority=CATEGORY_PRIORITY[category],
        cooldown=COOLDOWN_S[category],
        source_data=source,
    )


def rule_pace(ctx: SuggestionContext) -> list[Suggestion]:
    """Sector losses and improvements, from measured lap analysis."""
    if not ctx.has_pace:
        return []

    out: list[Suggestion] = []
    analysis = ctx.analysis

    worst = analysis.worst_sector()
    if worst is not None and worst.delta_s >= SECTOR_LOSS_TRIGGER_S:
        out.append(
            _make(
                f"pace.sector{worst.sector}",
                Category.PACE,
                f"Sector {worst.sector} is currently "
                f"+{worst.delta_s:.3f}s from your best.",
                f"Best sector {worst.sector} is {worst.best_s:.3f}s; "
                f"the last lap was {worst.time_s:.3f}s.",
                Severity.WARNING if worst.delta_s > 0.4 else Severity.ADVISORY,
                analysis.confidence,
                {
                    "sector": worst.sector,
                    "best_s": round(worst.best_s, 3),
                    "last_s": round(worst.time_s, 3),
                    "delta_s": round(worst.delta_s, 3),
                    "valid_laps": analysis.valid_laps,
                },
            )
        )

    improvement = -analysis.delta_to_previous_s
    if improvement >= 0.10:
        out.append(
            _make(
                "pace.improving",
                Category.PACE,
                f"You're {improvement:.3f}s up on the previous lap.",
                f"Last lap {analysis.last_lap_s:.3f}s against "
                f"{analysis.previous_lap_s:.3f}s.",
                Severity.INFO,
                analysis.confidence,
                {
                    "last_s": round(analysis.last_lap_s, 3),
                    "previous_s": round(analysis.previous_lap_s, 3),
                },
            )
        )

    # Time the driver has already shown they can do, just not on one lap.
    if analysis.time_available_s >= 0.30:
        out.append(
            _make(
                "pace.theoretical",
                Category.PACE,
                f"{analysis.time_available_s:.3f}s available on a clean lap.",
                f"Your best sectors add up to "
                f"{analysis.theoretical_best_s:.3f}s against a best lap of "
                f"{analysis.best_lap_s:.3f}s.",
                Severity.INFO,
                analysis.confidence,
                {
                    "theoretical_s": round(analysis.theoretical_best_s, 3),
                    "best_lap_s": round(analysis.best_lap_s, 3),
                    "available_s": round(analysis.time_available_s, 3),
                },
            )
        )
    return out


def rule_driving(ctx: SuggestionContext) -> list[Suggestion]:
    """Word the Driver Coach's observations.

    No driving analysis here. The coach correlated the inputs and decided
    how much its evidence is worth; this turns the most valuable one into a
    sentence. Inferred evidence is worded as "potential" so a correlation
    is never presented as a cause.
    """
    if not ctx.coaching:
        return []

    out: list[Suggestion] = []
    for observation in ctx.coaching[:2]:
        if not observation.confidence.is_usable:
            continue

        qualifier = (
            "Potential loss" if observation.evidence_kind.value == "INFERRED"
            else "Loss"
        )
        source = dict(observation.source_data)
        source["evidence"] = observation.evidence_kind.value
        source["repeat_count"] = observation.repeat_count

        out.append(
            _make(
                f"coach.{observation.id}",
                Category.DRIVING,
                observation.observation,
                f"{observation.evidence} {qualifier} "
                f"{observation.time_loss_s:.3f}s.",
                Severity.WARNING
                if int(observation.severity) >= 2
                else Severity.ADVISORY,
                observation.confidence,
                source,
            )
        )
    return out


def rule_tyres(ctx: SuggestionContext) -> list[Suggestion]:
    """Temperature, wear and measured degradation."""
    out: list[Suggestion] = []
    frame, tyres = ctx.frame, ctx.tyres

    temps = frame.tyre_surface_temp
    corners = (
        ("Front-left", temps.fl), ("Front-right", temps.fr),
        ("Rear-left", temps.rl), ("Rear-right", temps.rr),
    )
    hot = [(name, value) for name, value in corners if value >= TYRE_TEMP_TRIGGER_C]
    if hot:
        name, value = max(hot, key=lambda pair: pair[1])
        out.append(
            _make(
                "tyre.temperature",
                Category.TYRE,
                f"{name} temperature is rising.",
                f"{name} surface at {value:.0f}C, above the "
                f"{TYRE_TEMP_TRIGGER_C:.0f}C threshold.",
                Severity.WARNING,
                Confidence.HIGH,
                {"corner": name, "temp_c": round(value, 1)},
            )
        )

    if tyres.wear_pct >= TYRE_WEAR_TRIGGER_PCT:
        out.append(
            _make(
                "tyre.wear",
                Category.TYRE,
                f"{tyres.compound or 'Current'} set is at "
                f"{tyres.wear_pct:.0f}% wear.",
                f"{tyres.stint_laps} laps on this set.",
                Severity.ADVISORY,
                Confidence.HIGH,
                {
                    "wear_pct": round(tyres.wear_pct, 1),
                    "stint_laps": tyres.stint_laps,
                    "compound": tyres.compound,
                },
            )
        )

    # Degradation only once the stint model says the number is real.
    if (
        tyres.degradation_confidence.is_usable
        and tyres.degradation_s_per_lap >= DEGRADATION_TRIGGER_S
    ):
        source = {
            "degradation_s_per_lap": round(tyres.degradation_s_per_lap, 4),
            "compound": tyres.compound,
            "stint_laps": tyres.stint_laps,
            "tyre_age": tyres.age_laps,
        }
        reason = (
            f"Measured {tyres.degradation_s_per_lap:.3f}s/lap over "
            f"{tyres.stint_laps} laps on this set."
        )
        # A previous stint gives something to compare against.
        if len(ctx.stints) >= 2:
            previous = ctx.stints[-2]
            if previous.has_degradation:
                delta = tyres.degradation_s_per_lap - previous.degradation_s_per_lap
                source["previous_stint_deg"] = round(
                    previous.degradation_s_per_lap, 4
                )
                if delta > 0.01:
                    reason += (
                        f" The previous stint on {previous.compound or 'the other set'} "
                        f"degraded at {previous.degradation_s_per_lap:.3f}s/lap."
                    )
        out.append(
            _make(
                "tyre.degradation",
                Category.TYRE,
                "Tyre degradation is increasing.",
                reason,
                Severity.ADVISORY,
                tyres.degradation_confidence,
                source,
            )
        )
    return out


def rule_strategy(ctx: SuggestionContext) -> list[Suggestion]:
    """Word the Strategy Engine's recommendation.

    No pit maths here. The engine ranked the candidates, scored them and
    recorded why; this turns that into one sentence a driver can act on.
    """
    plan = ctx.strategy
    best = plan.recommended
    if not plan.available or best is None:
        return []
    if not best.confidence.is_usable:
        return []

    window = ""
    if plan.pit_window and best.kind.value == "PIT":
        first, last = plan.pit_window
        window = f" Window L{first}-L{last}." if last > first else ""

    source = dict(best.source_data)
    source["risk"] = best.risk.value
    source["score"] = best.score
    if plan.unmodelled:
        source["unmodelled_compounds"] = ", ".join(plan.unmodelled)

    reason = best.reason
    if best.assumptions:
        reason += "  Assumptions: " + " ".join(best.assumptions)

    if best.kind.value == "PIT":
        return [
            _make(
                "strategy.recommendation",
                Category.STRATEGY,
                f"Pit lap {best.pit_lap} for {best.next_compound} "
                f"(+{best.time_delta_s:.1f}s).{window}",
                reason,
                Severity.ADVISORY,
                best.confidence,
                source,
            )
        ]
    return [
        _make(
            "strategy.recommendation",
            Category.STRATEGY,
            "Stay out - no stop is worth it yet.",
            reason,
            Severity.INFO,
            best.confidence,
            source,
        )
    ]


def rule_race(ctx: SuggestionContext) -> list[Suggestion]:
    """Closing rate on the car ahead - measured by Race Intelligence."""
    ahead = ctx.race.ahead
    if not ahead.available or ahead.trend is GapTrend.UNKNOWN:
        return []
    if ahead.trend is GapTrend.STABLE:
        return []

    gap = ahead.gap_s or 0.0
    rate = ahead.rate_s_per_lap or 0.0
    if gap <= 0 or not rate:
        return []

    position = ctx.frame.position
    opponent = f"P{position - 1}" if position > 1 else "the car ahead"
    confidence = ahead.confidence
    source = {
        "gap_s": round(gap, 3),
        "closing_rate_s_per_lap": round(rate, 3),
        "samples": ahead.samples,
        "trend": ahead.trend.value,
        "position": position,
        "attack_state": ctx.race.attack_state.value,
    }

    if rate > 0:
        laps_to_catch = gap / rate if rate else 0.0
        source["laps_to_catch"] = round(laps_to_catch, 1)
        return [
            _make(
                "race.catching",
                Category.RACE,
                f"You're catching {opponent} at {rate:.2f}s/lap.",
                f"Gap {gap:.3f}s, closing over the last "
                f"{ahead.samples} laps - roughly "
                f"{laps_to_catch:.0f} laps to DRS range.",
                Severity.INFO,
                confidence,
                source,
            )
        ]
    return [
        _make(
            "race.dropping",
            Category.RACE,
            f"{opponent} is pulling away at {abs(rate):.2f}s/lap.",
            f"Gap {gap:.3f}s and growing over the last "
            f"{ctx.closing_samples} laps.",
            Severity.INFO,
            confidence,
            source,
        )
    ]


def rule_drs(ctx: SuggestionContext) -> list[Suggestion]:
    """DRS / Manual Override, using the mode's own terminology.

    Only fires where the game actually reports the capability, so F1 26 -
    whose active-aero telemetry is UNCONFIRMED - does not get told about a
    system we cannot yet read.
    """
    game = ctx.game
    if game is None:
        return []

    term = game.term("drs")
    state = ctx.race.drs_state
    # UNAVAILABLE / UNCONFIRMED / UNKNOWN all mean "we cannot say".
    if state in (DrsState.UNAVAILABLE, DrsState.UNCONFIRMED, DrsState.UNKNOWN):
        return []

    gap = ctx.race.ahead.gap_s or 0.0
    if gap <= 0:
        return []

    if state in (DrsState.IN_RANGE, DrsState.ACTIVE):
        return [
            _make(
                "drs.in_range",
                Category.DRS,
                f"{term} range.",
                f"Gap to the car ahead is {gap:.3f}s, inside the "
                f"{game.drs.activation_gap_s:.1f}s threshold.",
                Severity.INFO,
                Confidence.HIGH,
                {"gap_s": round(gap, 3), "threshold_s": game.drs.activation_gap_s},
            )
        ]
    if state is DrsState.OPPORTUNITY and ctx.closing_rate_s > 0:
        laps = (gap - DRS_RANGE_S) / ctx.closing_rate_s
        return [
            _make(
                "drs.approaching",
                Category.DRS,
                f"{term} range approaching.",
                f"Gap {gap:.3f}s, closing at {ctx.closing_rate_s:.2f}s/lap - "
                f"roughly {max(1, round(laps))} lap(s) away.",
                Severity.INFO,
                Confidence.from_samples(ctx.closing_samples),
                {
                    "gap_s": round(gap, 3),
                    "closing_rate_s_per_lap": round(ctx.closing_rate_s, 3),
                    "laps_to_range": round(laps, 1),
                },
            )
        ]
    return []


def rule_ers(ctx: SuggestionContext) -> list[Suggestion]:
    """Energy, tied to the race situation rather than nagged in isolation."""
    frame = ctx.frame
    mode = (frame.ers_mode or "").strip()
    if not mode:
        return []  # the game is not reporting a deploy mode

    store = frame.ers_store_percent
    gap = frame.delta_to_car_ahead_s

    if store <= ERS_LOW_TRIGGER_PCT:
        return [
            _make(
                "ers.low",
                Category.ERS,
                "Energy store is low.",
                f"{store:.0f}% remaining in {mode} mode.",
                Severity.ADVISORY,
                Confidence.HIGH,
                {"store_pct": round(store, 1), "mode": mode},
            )
        ]

    # Only suggest deploying when there is something to deploy at.
    if store >= 60.0 and 0 < gap <= DRS_APPROACH_S and mode.lower() != "overtake":
        return [
            _make(
                "ers.deploy",
                Category.ERS,
                "Deployment opportunity.",
                f"{store:.0f}% store with the car ahead {gap:.3f}s away, "
                f"currently in {mode} mode.",
                Severity.INFO,
                Confidence.HIGH,
                {"store_pct": round(store, 1), "gap_s": round(gap, 3), "mode": mode},
            )
        ]
    return []


def rule_fuel(ctx: SuggestionContext) -> list[Suggestion]:
    """Fuel margin, from measured consumption only.

    `fuel_per_lap` is the mean of what the driver has actually burned. With
    no completed laps there is no consumption figure and therefore nothing
    to say.
    """
    frame = ctx.frame
    if not ctx.fuel_per_lap or not frame.fuel_in_tank or not frame.total_laps:
        return []

    remaining = frame.total_laps - frame.current_lap
    if remaining <= 0:
        return []

    laps_of_fuel = frame.fuel_in_tank / ctx.fuel_per_lap
    margin = laps_of_fuel - remaining
    source = {
        "fuel_kg": round(frame.fuel_in_tank, 2),
        "fuel_per_lap_kg": round(ctx.fuel_per_lap, 3),
        "laps_of_fuel": round(laps_of_fuel, 1),
        "remaining_laps": remaining,
        "margin_laps": round(margin, 2),
    }

    if margin < -FUEL_MARGIN_LAPS:
        return [
            _make(
                "fuel.short",
                Category.FUEL,
                f"Fuel short by about {abs(margin):.1f} laps.",
                f"{frame.fuel_in_tank:.1f}kg at a measured "
                f"{ctx.fuel_per_lap:.2f}kg/lap covers {laps_of_fuel:.1f} laps "
                f"against {remaining} remaining.",
                Severity.WARNING,
                Confidence.MEDIUM,
                source,
            )
        ]
    if margin < 0:
        return [
            _make(
                "fuel.margin",
                Category.FUEL,
                "Fuel margin is decreasing.",
                f"Measured {ctx.fuel_per_lap:.2f}kg/lap leaves "
                f"{margin:+.1f} laps of margin.",
                Severity.ADVISORY,
                Confidence.MEDIUM,
                source,
            )
        ]
    return []


def rule_safety(ctx: SuggestionContext) -> list[Suggestion]:
    """Safety car and VSC.

    The safety-car field is declared on the frame but no adapter populates
    it yet, so this rule cannot fire. It is wired rather than faked: the
    moment the field is parsed this begins working, and until then nothing
    claims to know the race is neutralised.
    """
    status = (ctx.frame.safety_car or "").strip()
    if not status:
        return []

    return [
        _make(
            "safety.deployed",
            Category.SAFETY,
            f"{status} deployed.",
            "Pit loss is reduced under a neutralised race; a stop now costs "
            "less track position than under green flag conditions.",
            Severity.CRITICAL,
            Confidence.HIGH,
            {"safety_car": status, "lap": ctx.frame.current_lap},
        )
    ]


#: Evaluated in order; order does not affect the result, only readability.
RULES = (
    rule_safety,
    rule_strategy,
    rule_tyres,
    rule_fuel,
    rule_race,
    rule_pace,
    rule_driving,
    rule_ers,
    rule_drs,
)


# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _Record:
    """Engine bookkeeping for one suggestion id."""

    suggestion: Suggestion
    first_seen: float
    last_seen: float
    state: Lifecycle


class SmartSuggestionEngine:
    """Runs the rules and manages the lifecycle.

    Never called per UDP packet: the caller evaluates on a controlled
    cadence and on lap completion.
    """

    def __init__(self) -> None:
        self._records: dict[str, _Record] = {}
        #: id -> session clock when it last resolved, for the cooldown.
        self._resolved_at: dict[str, float] = {}
        self._history: list[Suggestion] = []

    # ------------------------------------------------------------------
    def reset(self) -> None:
        self._records.clear()
        self._resolved_at.clear()
        self._history.clear()

    @property
    def active(self) -> list[Suggestion]:
        """Active suggestions, most important first."""
        return sorted(
            (record.suggestion for record in self._records.values()),
            key=lambda s: (int(s.priority), int(s.severity)),
            reverse=True,
        )

    @property
    def top(self) -> Suggestion | None:
        """The single most relevant suggestion, for the dashboard."""
        active = self.active
        return active[0] if active else None

    @property
    def history(self) -> list[Suggestion]:
        """Resolved and expired suggestions, newest first."""
        return list(reversed(self._history))

    def by_category(self, category: Category) -> list[Suggestion]:
        return [s for s in self.active if s.category is category]

    # ------------------------------------------------------------------
    def evaluate(self, ctx: SuggestionContext) -> list[Suggestion]:
        """Recompute and return the active set.

        Stale telemetry raises nothing new - advice about a car that is not
        running is worse than silence - but existing suggestions are left
        alone rather than being torn down by a dropout.
        """
        if not ctx.live:
            return self.active

        candidates: dict[str, Suggestion] = {}
        for rule in RULES:
            for suggestion in rule(ctx):
                # A rule that cannot support its claim never reaches the UI.
                if suggestion.confidence.is_usable:
                    candidates.setdefault(suggestion.id, suggestion)

        self._retire_missing(candidates, ctx.now)
        self._expire(ctx.now)

        for key, candidate in candidates.items():
            existing = self._records.get(key)
            if existing is None:
                if not self._cooled_down(key, candidate, ctx.now):
                    continue
                self._records[key] = _Record(
                    suggestion=self._stamp(candidate, ctx.now, Lifecycle.TRIGGERED),
                    first_seen=ctx.now,
                    last_seen=ctx.now,
                    state=Lifecycle.TRIGGERED,
                )
                continue

            # Already showing. Update only when the wording or severity
            # actually changed - re-rendering identical text is spam.
            changed = (
                candidate.message != existing.suggestion.message
                or candidate.severity != existing.suggestion.severity
            )
            existing.last_seen = ctx.now
            if changed:
                existing.suggestion = self._stamp(
                    candidate, existing.first_seen, Lifecycle.UPDATED
                )
                existing.state = Lifecycle.UPDATED
            elif existing.state is not Lifecycle.ACTIVE:
                existing.suggestion = self._stamp(
                    existing.suggestion, existing.first_seen, Lifecycle.ACTIVE
                )
                existing.state = Lifecycle.ACTIVE

        return self.active

    # ------------------------------------------------------------------
    def _stamp(self, s: Suggestion, first_seen: float, state: Lifecycle) -> Suggestion:
        from dataclasses import replace

        return replace(
            s,
            timestamp=first_seen,
            expires_at=first_seen + TTL_S[s.category],
            state=state,
        )

    def _cooled_down(self, key: str, candidate: Suggestion, now: float) -> bool:
        resolved = self._resolved_at.get(key)
        if resolved is None:
            return True
        return (now - resolved) >= candidate.cooldown

    def _retire_missing(self, candidates: dict, now: float) -> None:
        """A condition that has gone away is RESOLVED, not left on screen."""
        for key in [k for k in self._records if k not in candidates]:
            record = self._records.pop(key)
            self._resolved_at[key] = now
            self._history.append(
                self._stamp(record.suggestion, record.first_seen, Lifecycle.RESOLVED)
            )

    def _expire(self, now: float) -> None:
        """Anything that outlived its TTL without changing is stale advice."""
        for key in [
            k for k, r in self._records.items() if r.suggestion.expires_at <= now
        ]:
            record = self._records.pop(key)
            self._resolved_at[key] = now
            self._history.append(
                self._stamp(record.suggestion, record.first_seen, Lifecycle.EXPIRED)
            )
