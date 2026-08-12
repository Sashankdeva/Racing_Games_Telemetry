"""Combines simultaneous effects into one drive level per motor.

Naive summing is the usual failure mode: once three effects are active
everything pins at 1.0 and the controller just buzzes uniformly - engine,
kerbs and a collision all feel identical. Naive max() is the other failure
mode: only the single loudest effect is ever felt, so the car stops feeling
like a car.

This mixer does neither. Effects are applied strongest-priority-first, and
each one consumes *headroom* proportional to its own amplitude and its
`dominance`. A full-strength collision (dominance 1.0) leaves no headroom
and momentarily owns the motor; a mid-strength engine rumble (dominance
~0.3) barely ducks anything, so kerbs and shifts still punch through it.
The result is soft-limited rather than clipped so contrast survives.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.haptics.signal import clamp, soft_limit


@dataclass(slots=True)
class Contribution:
    """One effect's request for this tick."""

    effect_id: str
    left: float = 0.0
    right: float = 0.0
    priority: int = 0
    #: How aggressively this effect suppresses lower-priority ones (0..1).
    dominance: float = 0.3
    #: Sidechain duck: extra suppression of everything below this effect,
    #: independent of this effect's own amplitude.
    #:
    #: Dominance alone cannot make a transient punch. With the engine bed
    #: already at 0.8, a gear shift can only add ~0.2 before saturating, so
    #: it reads as "slightly louder" rather than as a hit. A duck that
    #: OUTLASTS the hit drops the bed afterwards, leaving a brief hole - and
    #: that discontinuity is what the hand actually registers as an impact.
    #: Effects opt in; 0.0 leaves mixing exactly as it was.
    duck: float = 0.0

    @property
    def peak(self) -> float:
        return max(self.left, self.right)


@dataclass(slots=True)
class MixResult:
    left: float = 0.0
    right: float = 0.0
    #: Effect ids that actually reached the motors, strongest first.
    active: list[str] = field(default_factory=list)
    #: True when the soft limiter was engaged - shown in Diagnostics.
    limited: bool = False


class HapticMixer:
    """Stateless combiner. Ordering and ducking rules live here only."""

    #: Amplitude below which a contribution is treated as silence.
    AUDIBLE_FLOOR = 0.005

    #: Where soft limiting starts. Set high on purpose: the knee costs peak
    #: output, and on an ERM the difference between 0.96 and 1.00 drive is
    #: below the motor's own variance while the difference between "hardest
    #: possible hit" and "nearly hardest" is not. Priority ducking already
    #: stops most pile-up, so the limiter is a backstop, not the main
    #: mechanism - it should stay out of the way until genuinely needed.
    DEFAULT_KNEE = 0.85

    def __init__(self, knee: float = DEFAULT_KNEE) -> None:
        self.knee = clamp(knee, 0.1, 1.0)

    def mix(self, contributions: list[Contribution]) -> MixResult:
        result = MixResult()
        if not contributions:
            return result

        ordered = sorted(contributions, key=lambda c: c.priority, reverse=True)

        left_raw = self._mix_channel(ordered, channel_left=True)
        right_raw = self._mix_channel(ordered, channel_left=False)

        result.limited = left_raw > 1.0 or right_raw > 1.0
        result.left = clamp(soft_limit(left_raw, self.knee))
        result.right = clamp(soft_limit(right_raw, self.knee))

        result.active = [
            c.effect_id for c in ordered if c.peak > self.AUDIBLE_FLOOR
        ]
        return result

    def _mix_channel(self, ordered: list[Contribution], channel_left: bool) -> float:
        total = 0.0
        headroom = 1.0

        for contribution in ordered:
            value = contribution.left if channel_left else contribution.right
            duck = clamp(contribution.duck)

            # A duck applies even once the effect itself has gone quiet -
            # that trailing suppression is the whole point.
            if value <= self.AUDIBLE_FLOOR and duck <= 0.0:
                continue

            if value > self.AUDIBLE_FLOOR:
                total += value * headroom

            dominance = clamp(contribution.dominance)
            headroom *= (1.0 - clamp(value) * dominance) * (1.0 - duck)
            if headroom <= 0.001:
                break

        return total
