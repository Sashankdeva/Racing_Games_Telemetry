"""Manually-triggered haptic patterns for the Controller page's Test Lab.

These are independent of telemetry: they exist so the hardware and the feel
of the motor model can be validated without a game running, and so users
have a reference for what each intensity actually feels like.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.haptics.signal import (
    MAX_USEFUL_MOD_HZ,
    Envelope,
    NoiseSource,
    Oscillator,
    Waveform,
    clamp,
    lerp,
)


class PatternKind(str, Enum):
    CONSTANT = "constant"
    PULSE = "pulse"
    RAMP_UP = "ramp_up"
    RAMP_DOWN = "ramp_down"
    IMPACT = "impact"
    ENGINE = "engine"
    KERB = "kerb"
    NOISE = "noise"


class MotorTarget(str, Enum):
    LEFT = "left"
    RIGHT = "right"
    BOTH = "both"


#: Hard ceiling on any manual pattern - the Test Lab must never be able to
#: leave the motors running indefinitely.
MAX_TEST_DURATION = 30.0


@dataclass(slots=True)
class PatternSpec:
    kind: PatternKind = PatternKind.CONSTANT
    intensity: float = 0.6
    duration: float = 2.0
    target: MotorTarget = MotorTarget.BOTH
    pulse_rate: float = 8.0
    attack: float = 0.01
    release: float = 0.08
    sharpness: float = 0.8

    def clamped(self) -> "PatternSpec":
        return PatternSpec(
            kind=self.kind,
            intensity=clamp(self.intensity),
            duration=clamp(self.duration, 0.05, MAX_TEST_DURATION),
            target=self.target,
            pulse_rate=clamp(self.pulse_rate, 0.5, MAX_USEFUL_MOD_HZ),
            attack=clamp(self.attack, 0.0, 2.0),
            release=clamp(self.release, 0.0, 3.0),
            sharpness=clamp(self.sharpness),
        )


#: Presets exposed as one-click buttons.
PRESETS: dict[str, PatternSpec] = {
    "Soft": PatternSpec(PatternKind.CONSTANT, 0.30, 1.5, attack=0.25, release=0.35),
    "Medium": PatternSpec(PatternKind.CONSTANT, 0.60, 1.5, attack=0.10, release=0.20),
    "Strong": PatternSpec(PatternKind.CONSTANT, 1.00, 1.5, attack=0.05, release=0.15),
    "Pulse": PatternSpec(PatternKind.PULSE, 0.85, 3.0, pulse_rate=6.0, sharpness=0.9),
    "Impact": PatternSpec(PatternKind.IMPACT, 1.00, 0.6, attack=0.0, release=0.25, sharpness=1.0),
    "Engine": PatternSpec(PatternKind.ENGINE, 0.75, 4.0, pulse_rate=18.0, sharpness=0.5),
    "Kerb": PatternSpec(PatternKind.KERB, 0.90, 2.5, pulse_rate=14.0, sharpness=1.0),
}


class PatternPlayer:
    """Plays one PatternSpec, producing (left, right) per tick."""

    def __init__(self, spec: PatternSpec | None = None) -> None:
        self.spec = (spec or PatternSpec()).clamped()
        self._elapsed = 0.0
        self._finished = True
        self._osc = Oscillator(Waveform.SINE, 8.0)
        self._noise = NoiseSource(rate_hz=26.0, smooth=False, seed=77)
        self._envelope = Envelope()

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def elapsed(self) -> float:
        return self._elapsed

    def start(self, spec: PatternSpec) -> None:
        self.spec = spec.clamped()
        self._elapsed = 0.0
        self._finished = False
        self._osc.reset()

        if self.spec.kind is PatternKind.IMPACT:
            self._envelope.attack = self.spec.attack
            self._envelope.hold = 0.01
            self._envelope.decay = max(0.05, self.spec.duration)
            self._envelope.sustain_level = 0.0
            self._envelope.trigger(self.spec.intensity)

    def stop(self) -> None:
        self._finished = True
        self._elapsed = 0.0
        self._envelope.reset()

    def update(self, dt: float) -> tuple[float, float]:
        if self._finished:
            return 0.0, 0.0

        self._elapsed += dt
        spec = self.spec

        if self._elapsed >= spec.duration and spec.kind is not PatternKind.IMPACT:
            self._finished = True
            return 0.0, 0.0

        level = self._level(dt)

        if spec.kind is PatternKind.IMPACT and not self._envelope.active:
            self._finished = True
            return 0.0, 0.0

        # Fade the tail so a pattern never ends on an abrupt cut.
        if spec.release > 0.0 and spec.kind not in (
            PatternKind.IMPACT,
            PatternKind.RAMP_DOWN,
        ):
            remaining = spec.duration - self._elapsed
            if remaining < spec.release:
                level *= clamp(remaining / spec.release)

        level = clamp(level)
        if spec.target is MotorTarget.LEFT:
            return level, 0.0
        if spec.target is MotorTarget.RIGHT:
            return 0.0, level
        return level, level

    def _level(self, dt: float) -> float:
        spec = self.spec
        kind = spec.kind
        progress = clamp(self._elapsed / spec.duration) if spec.duration > 0 else 1.0

        if kind is PatternKind.CONSTANT:
            level = spec.intensity
            if spec.attack > 0.0 and self._elapsed < spec.attack:
                level *= self._elapsed / spec.attack
            return level

        if kind is PatternKind.RAMP_UP:
            return spec.intensity * progress

        if kind is PatternKind.RAMP_DOWN:
            return spec.intensity * (1.0 - progress)

        if kind is PatternKind.IMPACT:
            return self._envelope.update(dt)

        if kind is PatternKind.PULSE:
            self._osc.shape = Waveform.SQUARE
            self._osc.sharpness = spec.sharpness
            return spec.intensity * self._osc.update(dt, spec.pulse_rate)

        if kind is PatternKind.KERB:
            self._osc.shape = Waveform.PULSE
            self._osc.sharpness = spec.sharpness
            self._osc.duty = 0.45
            return spec.intensity * self._osc.update(dt, spec.pulse_rate)

        if kind is PatternKind.ENGINE:
            # Sweeps up through the rev range so the rate change is audible.
            self._osc.shape = Waveform.SINE
            self._osc.sharpness = spec.sharpness * 0.6
            rate = lerp(8.0, spec.pulse_rate, progress)
            modulation = self._osc.update(dt, rate)
            base = lerp(0.35, 1.0, progress) * spec.intensity
            depth = lerp(0.8, 0.3, progress)
            return base * ((1.0 - depth) + depth * modulation)

        # NOISE
        return spec.intensity * self._noise.update(dt)
