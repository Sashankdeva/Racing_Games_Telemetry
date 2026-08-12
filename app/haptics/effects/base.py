"""Effect interface.

An effect turns normalized telemetry into a per-motor intensity. Each one
owns its *own* signal character - modulation rate, sharpness, envelope,
smoothing - because a single global filter cannot serve both a gear shift
(needs a sub-10 ms attack) and body float over a crest (needs a few Hz of
smoothing). That per-effect ownership is the core of how this engine avoids
feeling uniformly mushy.

Effects must be cheap and allocation-free in `update()`: it runs at 120 Hz.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.core.models import TelemetryFrame, Wheels
from app.haptics.signal import above_threshold, apply_response, balance_to_gains, clamp


def wheels_to_lr(wheels: Wheels) -> tuple[float, float]:
    """Collapse per-wheel values onto the two motors.

    max() rather than mean() so a single wheel dropping onto a kerb is felt
    at full strength on that side instead of being halved into vagueness.
    """
    return max(wheels.fl, wheels.rl), max(wheels.fr, wheels.rr)


@dataclass(slots=True)
class EffectSettings:
    """User-facing knobs. Every effect understands these five; anything
    effect-specific lives in `advanced` so profiles stay forward-compatible."""

    enabled: bool = True
    #: Output scale, 0..2. Above 1 pushes the effect past its natural level.
    intensity: float = 1.0
    #: Input level below which the effect stays silent, 0..1.
    threshold: float = 0.0
    #: Response curve; <1 more sensitive early, >1 weighted to the top.
    response: float = 1.0
    #: 0 = rounded/soft edges, 1 = hard mechanical edges.
    sharpness: float = 0.5
    #: -1 fully left .. 0 centre .. +1 fully right.
    balance: float = 0.0
    advanced: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: float) -> float:
        value = self.advanced.get(key, default)
        return value if isinstance(value, (int, float)) else default

    def copy(self) -> "EffectSettings":
        return EffectSettings(
            enabled=self.enabled,
            intensity=self.intensity,
            threshold=self.threshold,
            response=self.response,
            sharpness=self.sharpness,
            balance=self.balance,
            advanced=dict(self.advanced),
        )


@dataclass(slots=True)
class EffectOutput:
    """What an effect wants the motors to do this tick."""

    left: float = 0.0
    right: float = 0.0

    @property
    def peak(self) -> float:
        return max(self.left, self.right)

    def silent(self) -> bool:
        return self.left <= 0.0 and self.right <= 0.0


class Effect(ABC):
    """Base class for every haptic effect.

    Subclasses implement `generate()` and inherit threshold/response/
    intensity/balance handling plus the enable check.
    """

    #: Stable identifier used in profiles and the UI. Must be unique.
    id: str = "effect"
    #: Human-readable name for the Effects page.
    name: str = "Effect"
    #: One-line explanation shown under the name.
    description: str = ""
    #: Higher wins in the mixer. Impacts > surface events > continuous beds.
    priority: int = 10
    #: How hard this effect ducks lower-priority ones (0..1).
    dominance: float = 0.3
    #: Shown on the Effects page to explain what the sharpness knob does.
    sharpness_label: str = "Sharpness"
    #: Effects that read no per-wheel data hide the balance control.
    supports_balance: bool = True
    #: True for effects holding envelopes/latches that must be cleared when
    #: telemetry drops, even if their current output is already zero.
    _holds_state: bool = False

    def duck(self) -> float:
        """Sidechain suppression of lower-priority effects, 0..1.

        Transient effects override this to punch a hole in the continuous
        beds that outlasts their own output - see Contribution.duck.
        """
        return 0.0

    def __init__(self, settings: EffectSettings | None = None) -> None:
        self.settings = settings or EffectSettings()
        self._last_output = EffectOutput()

    # --- lifecycle --------------------------------------------------------
    def apply_settings(self, settings: EffectSettings) -> None:
        self.settings = settings

    def reset(self) -> None:
        """Drop all internal state. Called on stop and profile change."""
        self._last_output = EffectOutput()

    @property
    def last_output(self) -> EffectOutput:
        return self._last_output

    # --- evaluation -------------------------------------------------------
    def update(self, dt: float, telemetry: TelemetryFrame) -> EffectOutput:
        """Evaluate this effect. Returns silence when disabled or invalid."""
        if not self.settings.enabled:
            if self._last_output.peak > 0.0:
                self.reset()
            self._last_output = EffectOutput()
            return self._last_output

        # No usable game data, or the game is paused: fall silent and drop
        # internal state so a half-finished envelope cannot resume later.
        if not telemetry.valid or telemetry.paused:
            if self._last_output.peak > 0.0 or self._holds_state:
                self.reset()
            self._last_output = EffectOutput()
            return self._last_output

        output = self.generate(dt, telemetry, self.settings)

        gain = clamp(self.settings.intensity, 0.0, 2.0)
        left_gain, right_gain = balance_to_gains(self.settings.balance)

        self._last_output = EffectOutput(
            left=clamp(output.left * gain * left_gain),
            right=clamp(output.right * gain * right_gain),
        )
        return self._last_output

    @abstractmethod
    def generate(
        self, dt: float, telemetry: TelemetryFrame, settings: EffectSettings
    ) -> EffectOutput:
        """Produce the raw 0..1 per-motor signal, before intensity/balance."""

    # --- helpers for subclasses ------------------------------------------
    @staticmethod
    def shape(value: float, settings: EffectSettings) -> float:
        """Apply this effect's threshold then its response curve."""
        return apply_response(
            above_threshold(value, settings.threshold), settings.response
        )

    @staticmethod
    def both(value: float) -> EffectOutput:
        return EffectOutput(left=value, right=value)
