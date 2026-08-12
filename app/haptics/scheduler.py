"""Scheduling for haptics that are not driven by telemetry.

Telemetry effects are continuous and evaluated every tick. Manual test
patterns and one-shot cues are different: they have a start, an optional
delay, a lifetime, and they must be cancellable. Keeping them in their own
scheduler means the Test Lab cannot interfere with effect state, and gives
one clean place to enforce the hard duration ceiling.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from app.haptics.mixer import Contribution
from app.haptics.patterns import MAX_TEST_DURATION, PatternPlayer, PatternSpec


@dataclass(slots=True)
class Cue:
    """A scheduled, time-bounded manual haptic."""

    cue_id: str
    player: PatternPlayer
    priority: int = 70
    dominance: float = 0.8
    delay: float = 0.0
    #: Absolute safety ceiling regardless of the pattern's own duration.
    max_lifetime: float = MAX_TEST_DURATION
    _age: float = field(default=0.0, init=False)

    @property
    def expired(self) -> bool:
        return self.player.finished or self._age >= self.max_lifetime

    def update(self, dt: float) -> tuple[float, float]:
        self._age += dt
        if self._age < self.delay:
            return 0.0, 0.0
        if self._age >= self.max_lifetime:
            self.player.stop()
            return 0.0, 0.0
        return self.player.update(dt)


class HapticScheduler:
    """Thread-safe registry of active cues."""

    def __init__(self) -> None:
        self._cues: dict[str, Cue] = {}
        self._lock = threading.Lock()

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._cues)

    def play(
        self,
        cue_id: str,
        spec: PatternSpec,
        priority: int = 70,
        dominance: float = 0.8,
        delay: float = 0.0,
    ) -> None:
        """Start (or restart) a named cue."""
        player = PatternPlayer()
        player.start(spec)
        cue = Cue(
            cue_id=cue_id,
            player=player,
            priority=priority,
            dominance=dominance,
            delay=delay,
            max_lifetime=min(MAX_TEST_DURATION, spec.duration + delay + 1.0),
        )
        with self._lock:
            self._cues[cue_id] = cue

    def cancel(self, cue_id: str) -> None:
        with self._lock:
            cue = self._cues.pop(cue_id, None)
        if cue:
            cue.player.stop()

    def clear(self) -> None:
        with self._lock:
            cues = list(self._cues.values())
            self._cues.clear()
        for cue in cues:
            cue.player.stop()

    def is_active(self, cue_id: str) -> bool:
        with self._lock:
            return cue_id in self._cues

    def update(self, dt: float, out: list[Contribution]) -> None:
        """Advance every cue, appending contributions to `out` in place."""
        with self._lock:
            cues = list(self._cues.items())

        expired: list[str] = []
        for cue_id, cue in cues:
            left, right = cue.update(dt)
            if cue.expired:
                expired.append(cue_id)
            if left > 0.0 or right > 0.0:
                out.append(
                    Contribution(
                        effect_id=cue_id,
                        left=left,
                        right=right,
                        priority=cue.priority,
                        dominance=cue.dominance,
                    )
                )

        if expired:
            with self._lock:
                for cue_id in expired:
                    self._cues.pop(cue_id, None)
