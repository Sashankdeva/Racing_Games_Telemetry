"""Strategy Engine - deterministic, explainable race strategy.

    telemetry -> analysis -> race intelligence -> STRATEGY -> suggestions -> UI

Consumes what the other modules already measured and produces ranked
recommendations. It recomputes nothing: degradation comes from the stint
model, gaps and race phase from Race Intelligence, pit loss from the track
profile or the game profile. No wording decisions are made here - Smart
Suggestions turns a change into a driver-facing message.

The cost model
--------------

Every candidate is scored as *time lost against an imaginary car that runs
the whole remaining distance on a fresh tyre*. That baseline cancels out
when candidates are compared, which is what makes the numbers meaningful
against each other rather than in absolute terms.

    stay out          d_now * sum(age+1 .. age+R)
    pit after k laps  d_now * sum(age+1 .. age+k)
                      + pit_loss
                      + d_next * sum(1 .. R-k)

`d` is measured seconds-per-lap of degradation for that specific compound,
from this session. Nothing is assumed about a compound that has not run.

What cannot be modelled, and is not faked
-----------------------------------------

The parser decodes only the player's car, so there is no opponent tyre age,
no opponent compound and no gap to the car behind. That means:

  * an **undercut** cannot be projected properly - the fresh-tyre gain on
    our side is computable, the opponent's loss is not. The opportunity is
    reported with that stated as an explicit assumption and a capped
    confidence, never as a number pretending to be complete.
  * **positions lost** in a stop cannot be counted, because the cars behind
    are invisible. Track position enters the score through the track's
    overtaking difficulty and Race Intelligence's traffic state instead.
  * a compound never run this session has **no** degradation figure. It is
    listed as unmodellable rather than given a plausible-looking default.

Under a safety car the real pit loss drops, but we have no measured figure
for it. `SAFETY_CAR_PIT_LOSS_FACTOR` is therefore a stated modelling
assumption: it is applied, it is named in `assumptions`, it appears in
`source_data`, and any recommendation that depends on it is capped below
HIGH confidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from app.core.models import TelemetryFrame
from app.domain.car_profiles import CarProfile
from app.domain.lap_analysis import Confidence
from app.domain.profile_intelligence import ProfileContext, RiskLevel
from app.domain.race_intelligence import (
    NeutralisedState,
    RacePhase,
    RaceState,
    TrafficState,
)
from app.domain.stints import Stint, TyreState
from app.domain.track_profiles import TrackProfile
from app.games.modes import GameProfile

#: Pit loss under a neutralised race, as a fraction of the green-flag
#: figure. A MODELLING ASSUMPTION, not a measurement: the field is not in
#: the telemetry. Always surfaced in `assumptions` and caps confidence.
SAFETY_CAR_PIT_LOSS_FACTOR = 0.55

#: Candidates within this many seconds of the best are treated as equally
#: good, which is what makes a pit *window* rather than a single lap.
WINDOW_TOLERANCE_S = 0.75
#: A recommendation must beat the baseline by this much to be worth making.
#: Below it, the honest answer is "no change".
MEANINGFUL_GAIN_S = 0.5
#: Recommended pit lap must move by more than this before it counts as a
#: material change worth telling the driver about.
PIT_LAP_HYSTERESIS = 2
#: Gap to the car ahead under which an undercut is positionally relevant.
UNDERCUT_GAP_S = 2.5
#: Laps from the end where changing the plan stops being sensible.
FINAL_LAPS_LOCKOUT = 3

# --- score weights, documented so the decision is never hidden -----------
#: Projected race time dominates: it is the only term measured in seconds.
WEIGHT_RACE_TIME = 1.0
#: Penalty per unit of traffic risk, in seconds-equivalent. Deliberately
#: small - traffic is real but we can only see the car ahead.
WEIGHT_TRAFFIC_S = 1.5
#: Penalty for stopping at a track where places are hard to win back,
#: scaled by the track's overtaking difficulty (0-100).
WEIGHT_TRACK_POSITION_S = 4.0


class Risk(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class StrategyKind(str, Enum):
    STAY_OUT = "STAY_OUT"
    PIT = "PIT"


@dataclass(frozen=True, slots=True)
class CompoundModel:
    """What we know about one compound's degradation, and from where."""

    name: str
    degradation_s_per_lap: float | None = None
    confidence: Confidence = Confidence.NO_DATA
    #: "observed" (this session) or "unmodelled".
    source: str = "unmodelled"

    @property
    def usable(self) -> bool:
        return self.degradation_s_per_lap is not None and self.confidence.is_usable


@dataclass(frozen=True, slots=True)
class StrategyRecommendation:
    """One candidate plan, fully explained."""

    strategy_id: str
    kind: StrategyKind
    current_compound: str = ""
    next_compound: str = ""
    pit_lap: int | None = None
    number_of_stops: int = 0
    #: Projected time lost over the remaining distance, against a
    #: fresh-tyre baseline. Comparable between candidates, not absolute.
    expected_time_s: float = 0.0
    #: Against the current strategy. Positive = this plan is faster.
    time_delta_s: float = 0.0
    score: float = 0.0
    risk: Risk = Risk.UNKNOWN
    confidence: Confidence = Confidence.NO_DATA
    reason: str = ""
    #: Everything taken on trust rather than measured.
    assumptions: tuple[str, ...] = ()
    source_data: dict = field(default_factory=dict)

    @property
    def is_baseline(self) -> bool:
        return self.kind is StrategyKind.STAY_OUT

    def summary(self) -> str:
        if self.kind is StrategyKind.STAY_OUT:
            return "Stay out"
        return f"Pit L{self.pit_lap} -> {self.next_compound}"


@dataclass(frozen=True, slots=True)
class StrategyPlan:
    """The ranked answer, plus everything needed to justify it."""

    recommended: StrategyRecommendation | None = None
    alternative: StrategyRecommendation | None = None
    backup: StrategyRecommendation | None = None
    baseline: StrategyRecommendation | None = None
    #: Inclusive lap range where stopping costs about the same.
    pit_window: tuple[int, int] | None = None
    generated_lap: int = 0
    available: bool = False
    stale: bool = False
    #: Why no plan could be produced, when there is none.
    reason: str = ""
    #: Compounds with no session data, so they could not be evaluated.
    unmodelled: tuple[str, ...] = ()

    @property
    def candidates(self) -> list[StrategyRecommendation]:
        return [
            item
            for item in (self.recommended, self.alternative, self.backup)
            if item is not None
        ]


@dataclass(frozen=True, slots=True)
class StrategyChange:
    """A point at which the recommendation materially moved."""

    lap: int
    timestamp: float
    previous: str
    current: str
    reason: str


@dataclass(slots=True)
class StrategyContext:
    """Everything the engine may read. Assembled by the caller."""

    frame: TelemetryFrame
    race: RaceState
    tyres: TyreState
    stints: list[Stint] = field(default_factory=list)
    game: GameProfile | None = None
    car: CarProfile | None = None
    track: TrackProfile | None = None
    #: Measured mean fuel burn per lap. 0.0 when not yet known.
    fuel_per_lap: float = 0.0
    #: Car and track context. Queried, never duplicated here.
    profiles: ProfileContext | None = None
    now: float = 0.0
    live: bool = True


# ---------------------------------------------------------------------------
def compound_models(stints: list[Stint], game: GameProfile | None) -> dict[str, CompoundModel]:
    """What each compound has actually shown this session.

    Observed data only. A compound that has not run has no number, and gets
    none invented for it - which is why early-race plans are limited to the
    compounds already used.
    """
    names = list(game.strategy.dry_compounds) if game else []
    models: dict[str, CompoundModel] = {
        name: CompoundModel(name=name) for name in names
    }

    for stint in stints:
        if not stint.compound or not stint.has_degradation:
            continue
        existing = models.get(stint.compound)
        # A later, better-supported stint on the same compound wins.
        if existing is None or not existing.usable or (
            int(stint.degradation_confidence.name == "HIGH")
            >= int(existing.confidence.name == "HIGH")
        ):
            models[stint.compound] = CompoundModel(
                name=stint.compound,
                degradation_s_per_lap=stint.degradation_s_per_lap,
                confidence=stint.degradation_confidence,
                source="observed",
            )
    return models


def _triangular(count: int, offset: int = 0) -> int:
    """Sum of (offset+1 .. offset+count) - cumulative tyre age over a run."""
    if count <= 0:
        return 0
    return count * (2 * offset + count + 1) // 2


def _pit_loss(ctx: StrategyContext) -> tuple[float, str, bool]:
    """Pit loss in seconds, its source, and whether it was assumed.

    Track profile first - it is a measurable figure - then the mode default.
    """
    base = 0.0
    source = ""
    if ctx.track is not None and ctx.track.pit_loss_s > 0:
        base, source = ctx.track.pit_loss_s, f"{ctx.track.name} pit loss"
    elif ctx.game is not None:
        base, source = (
            ctx.game.strategy.default_pit_loss_s,
            f"{ctx.game.display_name} default pit loss",
        )
    if not base:
        return 0.0, "", False

    neutralised = ctx.race.neutralised in (
        NeutralisedState.SAFETY_CAR, NeutralisedState.VSC
    )
    if neutralised:
        return (
            base * SAFETY_CAR_PIT_LOSS_FACTOR,
            f"{source}, reduced for {ctx.race.neutralised.value}",
            True,
        )
    return base, source, False


class StrategyEngine:
    """Ranks candidate plans and reports when the answer materially changes."""

    def __init__(self) -> None:
        self._plan = StrategyPlan()
        self._history: list[StrategyChange] = []
        self._last_signature: tuple | None = None

    # ------------------------------------------------------------------
    def reset(self) -> None:
        self._plan = StrategyPlan()
        self._history.clear()
        self._last_signature = None

    @property
    def plan(self) -> StrategyPlan:
        return self._plan

    @property
    def history(self) -> list[StrategyChange]:
        """Newest first."""
        return list(reversed(self._history))

    # ------------------------------------------------------------------
    def evaluate(self, ctx: StrategyContext) -> StrategyPlan:
        """Recompute the plan. Called on lap completion, not per frame.

        A stale feed keeps the previous plan: strategy is a lap-scale
        decision and a dropout is not new information.
        """
        if not ctx.live:
            self._plan = _mark_stale(self._plan)
            return self._plan

        plan = self._build(ctx)
        self._detect_change(plan, ctx)
        self._plan = plan
        return plan

    # ------------------------------------------------------------------
    def _build(self, ctx: StrategyContext) -> StrategyPlan:
        frame, race, tyres = ctx.frame, ctx.race, ctx.tyres

        lap = frame.current_lap or 0
        remaining = race.laps_remaining
        if not lap or remaining is None or remaining <= 0:
            return StrategyPlan(
                reason="race distance is unavailable, so no plan can be projected",
                generated_lap=lap,
            )

        if race.race_phase is RacePhase.FINAL_LAPS or remaining <= FINAL_LAPS_LOCKOUT:
            return StrategyPlan(
                reason=(
                    f"{remaining} laps remain - too late for a strategy change "
                    "to pay back a pit stop"
                ),
                generated_lap=lap,
            )

        current = tyres.compound or ""
        if not tyres.degradation_confidence.is_usable:
            return StrategyPlan(
                reason=(
                    "degradation on the current set is not yet measurable - "
                    f"{tyres.stint_laps} lap(s) on it"
                ),
                generated_lap=lap,
            )

        pit_loss, loss_source, loss_assumed = _pit_loss(ctx)
        if not pit_loss:
            return StrategyPlan(
                reason="no pit-loss figure is available for this track",
                generated_lap=lap,
            )

        models = compound_models(ctx.stints, ctx.game)
        usable = {name: m for name, m in models.items() if m.usable}
        unmodelled = tuple(sorted(name for name, m in models.items() if not m.usable))

        d_now = tyres.degradation_s_per_lap
        age = max(0, tyres.age_laps)

        baseline = self._stay_out(ctx, d_now, age, remaining, current)
        candidates = [baseline]

        for name, model in usable.items():
            best = self._best_stop(
                ctx, d_now, age, remaining, lap, name, model,
                pit_loss, loss_source, loss_assumed, baseline,
            )
            if best is not None:
                candidates.append(best)

        ranked = sorted(candidates, key=lambda c: c.score)
        window = self._window(
            ctx, d_now, age, remaining, lap, ranked[0], pit_loss, usable
        )

        # Only displace the baseline when the gain is worth a pit stop.
        best = ranked[0]
        if best.kind is StrategyKind.PIT and best.time_delta_s < MEANINGFUL_GAIN_S:
            ranked = [baseline] + [c for c in ranked if c is not baseline]

        return StrategyPlan(
            recommended=ranked[0],
            alternative=ranked[1] if len(ranked) > 1 else None,
            backup=ranked[2] if len(ranked) > 2 else None,
            baseline=baseline,
            pit_window=window,
            generated_lap=lap,
            available=True,
            unmodelled=unmodelled,
        )

    # --- candidates ----------------------------------------------------
    def _stay_out(
        self, ctx: StrategyContext, d_now: float, age: int, remaining: int,
        current: str,
    ) -> StrategyRecommendation:
        cost = d_now * _triangular(remaining, age)
        return StrategyRecommendation(
            strategy_id="stay_out",
            kind=StrategyKind.STAY_OUT,
            current_compound=current,
            number_of_stops=0,
            expected_time_s=round(cost, 2),
            time_delta_s=0.0,
            score=round(cost, 3),
            risk=Risk.LOW if d_now < 0.1 else Risk.MEDIUM,
            confidence=ctx.tyres.degradation_confidence,
            reason=(
                f"Running to the end on this {current or 'set'} projects "
                f"{cost:.1f}s lost to degradation over {remaining} laps at "
                f"{d_now:.3f}s/lap."
            ),
            source_data={
                "remaining_laps": remaining,
                "tyre_age": age,
                "degradation_s_per_lap": round(d_now, 4),
                "projected_loss_s": round(cost, 1),
            },
        )

    def _stop_cost(
        self, d_now: float, age: int, remaining: int, laps_until_stop: int,
        d_next: float, pit_loss: float,
    ) -> float:
        before = d_now * _triangular(laps_until_stop, age)
        after = d_next * _triangular(remaining - laps_until_stop)
        return before + pit_loss + after

    def _best_stop(
        self, ctx: StrategyContext, d_now: float, age: int, remaining: int,
        lap: int, name: str, model: CompoundModel, pit_loss: float,
        loss_source: str, loss_assumed: bool, baseline: StrategyRecommendation,
    ) -> StrategyRecommendation | None:
        d_next = model.degradation_s_per_lap or 0.0

        best_k, best_cost = None, None
        for k in range(0, remaining):
            cost = self._stop_cost(d_now, age, remaining, k, d_next, pit_loss)
            if best_cost is None or cost < best_cost:
                best_k, best_cost = k, cost
        if best_k is None:
            return None

        pit_lap = lap + best_k
        delta = baseline.expected_time_s - best_cost
        score, penalties = self._score(ctx, best_cost)
        risk, confidence, assumptions = self._risk(
            ctx, model, loss_assumed, delta
        )

        reasons = [
            f"{name} measured at {d_next:.3f}s/lap over its stint this session.",
            f"Pit loss {pit_loss:.1f}s ({loss_source}).",
            f"Stopping on lap {pit_lap} projects {best_cost:.1f}s lost against "
            f"{baseline.expected_time_s:.1f}s for staying out.",
        ]
        if penalties:
            reasons.append(penalties)

        return StrategyRecommendation(
            strategy_id=f"pit_{name.lower()}",
            kind=StrategyKind.PIT,
            current_compound=ctx.tyres.compound or "",
            next_compound=name,
            pit_lap=pit_lap,
            number_of_stops=1,
            expected_time_s=round(best_cost, 2),
            time_delta_s=round(delta, 2),
            score=round(score, 3),
            risk=risk,
            confidence=confidence,
            reason=" ".join(reasons),
            assumptions=assumptions,
            source_data={
                "pit_lap": pit_lap,
                "laps_until_stop": best_k,
                "remaining_laps": remaining,
                "current_degradation": round(d_now, 4),
                "next_degradation": round(d_next, 4),
                "next_compound_source": model.source,
                "pit_loss_s": round(pit_loss, 1),
                "pit_loss_source": loss_source,
                "projected_cost_s": round(best_cost, 1),
                "projected_gain_s": round(delta, 1),
                "traffic": ctx.race.traffic_state.value,
                "race_phase": ctx.race.race_phase.value,
            },
        )

    # --- scoring --------------------------------------------------------
    def _score(self, ctx: StrategyContext, cost: float) -> tuple[float, str]:
        """Projected time plus explicitly weighted, named penalties.

        Every term is documented at module level; nothing is hidden behind
        an unexplained number.
        """
        score = cost * WEIGHT_RACE_TIME
        notes: list[str] = []

        if ctx.race.traffic_state is TrafficState.LIGHT_TRAFFIC:
            score += WEIGHT_TRAFFIC_S
            notes.append("traffic nearby")
        elif ctx.race.traffic_state is TrafficState.HEAVY_TRAFFIC:
            score += WEIGHT_TRAFFIC_S * 2
            notes.append("heavy traffic")

        if ctx.track is not None:
            difficulty = ctx.track.overtaking_difficulty / 100.0
            penalty = WEIGHT_TRACK_POSITION_S * difficulty
            score += penalty
            if difficulty > 0.6:
                notes.append(
                    f"places are hard to win back at {ctx.track.name}"
                )
        return score, (
            "Track position risk: " + ", ".join(notes) + "." if notes else ""
        )

    def _risk(
        self, ctx: StrategyContext, model: CompoundModel, loss_assumed: bool,
        delta: float,
    ) -> tuple[Risk, Confidence, tuple[str, ...]]:
        assumptions: list[str] = []
        confidence = model.confidence

        # The tyre model's confidence caps everything downstream.
        if ctx.tyres.degradation_confidence is Confidence.LOW:
            confidence = Confidence.LOW

        if loss_assumed:
            assumptions.append(
                f"Pit loss under a neutralised race is not measured; "
                f"{SAFETY_CAR_PIT_LOSS_FACTOR:.0%} of the green-flag figure "
                "is assumed."
            )
            # An assumed input can never produce a high-confidence answer.
            if confidence is Confidence.HIGH:
                confidence = Confidence.MEDIUM

        if not ctx.fuel_per_lap:
            assumptions.append(
                "Fuel consumption has not been measured yet, so fuel is not "
                "part of this comparison."
            )

        assumptions.append(
            "Opponent tyre age and compound are not in the telemetry, so no "
            "undercut or overcut projection is included."
        )

        # An inferred car+track tyre-stress signal raises the risk, and is
        # recorded as an assumption because it is an inference, not a fact.
        if ctx.profiles is not None:
            stress = ctx.profiles.risk_signals().get("tyre_stress_risk")
            if stress is not None and stress.level is RiskLevel.HIGH:
                assumptions.append(
                    f"Inferred high tyre stress for this car/track: {stress.reason}"
                )

        risk = Risk.LOW
        if ctx.race.traffic_state is TrafficState.HEAVY_TRAFFIC:
            risk = Risk.HIGH
        elif loss_assumed or ctx.race.traffic_state is TrafficState.LIGHT_TRAFFIC:
            risk = Risk.MEDIUM
        if delta < MEANINGFUL_GAIN_S:
            risk = Risk.HIGH if risk is Risk.MEDIUM else risk
        if not confidence.is_usable:
            risk = Risk.UNKNOWN
        return risk, confidence, tuple(assumptions)

    def _window(
        self, ctx: StrategyContext, d_now: float, age: int, remaining: int,
        lap: int, best: StrategyRecommendation, pit_loss: float,
        usable: dict[str, CompoundModel],
    ) -> tuple[int, int] | None:
        """Laps whose projected cost is within tolerance of the best."""
        if best.kind is not StrategyKind.PIT or not best.next_compound:
            return None
        model = usable.get(best.next_compound)
        if model is None or model.degradation_s_per_lap is None:
            return None

        d_next = model.degradation_s_per_lap
        costs = [
            (k, self._stop_cost(d_now, age, remaining, k, d_next, pit_loss))
            for k in range(0, remaining)
        ]
        cheapest = min(cost for _, cost in costs)
        inside = [k for k, cost in costs if cost <= cheapest + WINDOW_TOLERANCE_S]
        if not inside:
            return None
        return (lap + min(inside), lap + max(inside))

    # --- change detection ------------------------------------------------
    def _signature(self, plan: StrategyPlan) -> tuple:
        best = plan.recommended
        if best is None:
            return ("none",)
        return (best.strategy_id, best.next_compound, best.pit_lap or 0)

    def _detect_change(self, plan: StrategyPlan, ctx: StrategyContext) -> None:
        """Record only material changes.

        A pit lap drifting by a lap as degradation is refined is not news;
        a different compound or a different plan is.
        """
        signature = self._signature(plan)
        previous = self._last_signature
        if previous is None:
            self._last_signature = signature
            return
        if signature == previous:
            return

        same_plan = signature[0] == previous[0] and signature[1] == previous[1]
        if same_plan and abs(signature[2] - previous[2]) <= PIT_LAP_HYSTERESIS:
            # Within hysteresis: update quietly, do not announce.
            self._last_signature = signature
            return

        old = self._plan.recommended.summary() if self._plan.recommended else "none"
        new = plan.recommended.summary() if plan.recommended else "none"
        reason = plan.recommended.reason if plan.recommended else plan.reason
        if ctx.race.neutralised in (NeutralisedState.SAFETY_CAR, NeutralisedState.VSC):
            reason = f"{ctx.race.neutralised.value}: {reason}"

        self._history.append(
            StrategyChange(
                lap=ctx.frame.current_lap or 0,
                timestamp=ctx.now,
                previous=old,
                current=new,
                reason=reason,
            )
        )
        self._last_signature = signature

    @property
    def changed(self) -> bool:
        """True when the last evaluation moved the recommendation."""
        return bool(self._history)


def _mark_stale(plan: StrategyPlan) -> StrategyPlan:
    from dataclasses import replace

    return replace(plan, stale=True)


def undercut_assessment(ctx: StrategyContext) -> dict:
    """Whether an undercut is positionally on, and what we cannot know.

    The gap is real telemetry. The opponent's tyre age - the other half of
    an undercut - is not parsed, so this reports the situation and refuses
    to put a number on the outcome.
    """
    ahead = ctx.race.ahead
    if not ahead.available or ahead.gap_s is None:
        return {"available": False, "reason": "no gap to the car ahead"}

    positional = ahead.gap_s <= UNDERCUT_GAP_S
    return {
        "available": True,
        "in_range": positional,
        "gap_s": round(ahead.gap_s, 3),
        "our_tyre_age": ctx.tyres.age_laps,
        "opponent_tyre_age": None,
        "projection": None,
        "reason": (
            "Opponent tyre age and compound are not in the telemetry, so the "
            "gain from an undercut cannot be projected."
        ),
    }
