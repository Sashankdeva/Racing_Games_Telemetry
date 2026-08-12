"""Signal generation primitives for haptic effects.

An ERM motor only accepts amplitude - there is no frequency channel. All
perceived "texture" and "rate" therefore come from amplitude modulation:
we vary the drive level over time and the motor's own inertia turns that
into a felt rhythm.

Practical limits this module is built around:
  * Below roughly 0.15 drive an ERM does not overcome static friction at
    all, so small values must be mapped up, not scaled down.
  * Modulation above ~35 Hz is smeared into a flat buzz by rotor inertia,
    so there is no point generating faster patterns.
  * Between ~4 Hz and ~30 Hz the motor tracks well enough that the rhythm
    is clearly felt - this is the band effects should live in.

Every object here is stateful and updated in place: the haptic loop runs at
120 Hz and must not allocate per tick.
"""

from __future__ import annotations

import math
import random
from enum import Enum

TWO_PI = math.pi * 2.0

#: Fastest modulation worth generating - beyond this the rotor cannot track it.
MAX_USEFUL_MOD_HZ = 38.0
#: Slowest modulation that still reads as a rhythm rather than a level change.
MIN_USEFUL_MOD_HZ = 1.0


# --------------------------------------------------------------------------
# scalar helpers
# --------------------------------------------------------------------------
def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    if value != value:  # NaN
        return low
    return low if value < low else high if value > high else value


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def above_threshold(value: float, threshold: float) -> float:
    """Rescale `value` so it emerges from 0 exactly at `threshold`.

    Without this an effect with a threshold pops in at full strength the
    instant it crosses; with it, the effect fades in from silence.
    """
    if threshold <= 0.0:
        return clamp(value)
    if threshold >= 1.0:
        return 0.0
    if value <= threshold:
        return 0.0
    return clamp((value - threshold) / (1.0 - threshold))


def apply_response(value: float, response: float) -> float:
    """Response curve. 1.0 linear, <1 more sensitive early, >1 more top-end."""
    value = clamp(value)
    if response == 1.0 or value <= 0.0:
        return value
    return value ** max(0.05, response)


def soft_limit(value: float, knee: float = 0.75) -> float:
    """Saturate toward 1.0 instead of clipping flat at it.

    Hard clipping makes every strong moment feel identical; a soft knee
    keeps some contrast between "strong" and "enormous".
    """
    if value <= knee:
        return max(0.0, value)
    if knee >= 1.0:
        return clamp(value)
    span = 1.0 - knee
    return knee + span * math.tanh((value - knee) / span)


def balance_to_gains(balance: float) -> tuple[float, float]:
    """Map -1(left) .. 0(centre) .. +1(right) to (left_gain, right_gain).

    Centre keeps both motors at full so balance only ever attenuates.
    """
    balance = clamp(balance, -1.0, 1.0)
    left = 1.0 if balance <= 0.0 else 1.0 - balance
    right = 1.0 if balance >= 0.0 else 1.0 + balance
    return left, right


# --------------------------------------------------------------------------
# waveforms
# --------------------------------------------------------------------------
class Waveform(str, Enum):
    SINE = "sine"
    SQUARE = "square"
    PULSE = "pulse"
    SAW_DOWN = "saw_down"
    TRIANGLE = "triangle"


def evaluate(
    shape: Waveform, phase: float, sharpness: float = 1.0, duty: float = 0.5
) -> float:
    """Evaluate a unipolar (0..1) waveform at `phase` in [0,1).

    `sharpness` blends from a rounded shape (0.0) to a hard-edged one (1.0),
    which is what gives kerbs and ABS their mechanical bite while letting
    road texture stay soft.
    """
    phase = phase % 1.0
    sharpness = clamp(sharpness)

    if shape is Waveform.SINE:
        smooth = 0.5 - 0.5 * math.cos(TWO_PI * phase)
        if sharpness <= 0.0:
            return smooth
        hard = 1.0 if phase < duty else 0.0
        return lerp(smooth, hard, sharpness)

    if shape is Waveform.SQUARE:
        hard = 1.0 if phase < duty else 0.0
        if sharpness >= 1.0:
            return hard
        smooth = 0.5 - 0.5 * math.cos(TWO_PI * phase)
        return lerp(smooth, hard, sharpness)

    if shape is Waveform.PULSE:
        # Short spike then silence - the rhythmic "tick" used by kerbs.
        if phase >= duty:
            return 0.0
        local = phase / max(duty, 1e-6)
        spike = 1.0 - local  # linear decay across the pulse
        if sharpness >= 1.0:
            return 1.0 if local < 0.5 else spike
        return lerp(0.5 - 0.5 * math.cos(TWO_PI * local * 0.5), spike, sharpness)

    if shape is Waveform.SAW_DOWN:
        value = 1.0 - phase
        return value ** (1.0 + 2.0 * sharpness)

    # TRIANGLE
    return 1.0 - abs(2.0 * phase - 1.0)


class Oscillator:
    """Phase-continuous modulator.

    Frequency is set every tick from live telemetry; keeping the phase
    accumulator continuous across changes is what stops an RPM sweep from
    producing audible/tactile clicks.
    """

    __slots__ = ("phase", "shape", "sharpness", "duty", "frequency")

    def __init__(
        self,
        shape: Waveform = Waveform.SINE,
        frequency: float = 10.0,
        sharpness: float = 0.0,
        duty: float = 0.5,
    ) -> None:
        self.phase = 0.0
        self.shape = shape
        self.frequency = frequency
        self.sharpness = sharpness
        self.duty = duty

    def update(self, dt: float, frequency: float | None = None) -> float:
        if frequency is not None:
            self.frequency = frequency
        freq = clamp(self.frequency, 0.0, MAX_USEFUL_MOD_HZ)
        self.phase = (self.phase + freq * dt) % 1.0
        return evaluate(self.shape, self.phase, self.sharpness, self.duty)

    def reset(self, phase: float = 0.0) -> None:
        self.phase = phase % 1.0

    def cycle_completed(self, dt: float) -> bool:
        """True if the next update would wrap - useful for retriggering."""
        freq = clamp(self.frequency, 0.0, MAX_USEFUL_MOD_HZ)
        return self.phase + freq * dt >= 1.0


# --------------------------------------------------------------------------
# noise
# --------------------------------------------------------------------------
class NoiseSource:
    """Band-limited value noise for surfaces that must feel irregular.

    Gravel is not a periodic vibration - a pure oscillator reads as a tone
    and immediately feels synthetic. This walks between random targets at a
    controlled rate; `smooth=False` sample-and-holds instead, which is
    harsher and suits loose surfaces.
    """

    __slots__ = ("_rate", "_value", "_target", "_phase", "_rng", "smooth")

    def __init__(self, rate_hz: float = 18.0, smooth: bool = True, seed: int | None = None) -> None:
        self._rate = max(0.1, rate_hz)
        self._rng = random.Random(seed)
        self._value = self._rng.random()
        self._target = self._rng.random()
        self._phase = 0.0
        self.smooth = smooth

    @property
    def rate(self) -> float:
        return self._rate

    def set_rate(self, rate_hz: float) -> None:
        self._rate = clamp(rate_hz, 0.1, 60.0)

    def update(self, dt: float) -> float:
        self._phase += self._rate * dt
        if self._phase >= 1.0:
            self._phase %= 1.0
            self._value = self._target
            self._target = self._rng.random()
        if not self.smooth:
            return self._value
        # Cosine interpolation - avoids the linear-ramp "sawtooth" artifact.
        t = 0.5 - 0.5 * math.cos(math.pi * self._phase)
        return lerp(self._value, self._target, t)


# --------------------------------------------------------------------------
# envelopes
# --------------------------------------------------------------------------
class EnvelopeStage(Enum):
    IDLE = 0
    ATTACK = 1
    HOLD = 2
    DECAY = 3
    SUSTAIN = 4
    RELEASE = 5


class Envelope:
    """Attack / hold / decay envelope with an optional sustain.

    With `sustain_level == 0` this is a one-shot: attack, hold, decay, done -
    exactly the shape a gear shift or a collision needs. With a non-zero
    sustain it holds until `release()` is called.

    Attack times below one tick are clamped to a single tick, so an impact
    always reaches full amplitude on the very next sample rather than being
    quietly rounded away.
    """

    __slots__ = (
        "attack",
        "hold",
        "decay",
        "sustain_level",
        "release_time",
        "_stage",
        "_level",
        "_elapsed",
        "_amplitude",
        "_release_from",
    )

    def __init__(
        self,
        attack: float = 0.01,
        hold: float = 0.0,
        decay: float = 0.15,
        sustain_level: float = 0.0,
        release_time: float = 0.05,
    ) -> None:
        self.attack = max(0.0, attack)
        self.hold = max(0.0, hold)
        self.decay = max(1e-4, decay)
        self.sustain_level = clamp(sustain_level)
        self.release_time = max(1e-4, release_time)

        self._stage = EnvelopeStage.IDLE
        self._level = 0.0
        self._elapsed = 0.0
        self._amplitude = 1.0
        self._release_from = 0.0

    @property
    def stage(self) -> EnvelopeStage:
        return self._stage

    @property
    def level(self) -> float:
        return self._level

    @property
    def active(self) -> bool:
        return self._stage is not EnvelopeStage.IDLE

    def trigger(self, amplitude: float = 1.0) -> None:
        """(Re)start the envelope. Retriggering keeps the louder of the two
        amplitudes so a second hit never quietens an in-flight one."""
        amplitude = clamp(amplitude)
        if self._stage is not EnvelopeStage.IDLE:
            amplitude = max(amplitude, self._level)
        self._amplitude = amplitude
        self._stage = EnvelopeStage.ATTACK
        self._elapsed = 0.0

    def release(self) -> None:
        if self._stage in (EnvelopeStage.IDLE, EnvelopeStage.RELEASE):
            return
        self._release_from = self._level
        self._stage = EnvelopeStage.RELEASE
        self._elapsed = 0.0

    def reset(self) -> None:
        self._stage = EnvelopeStage.IDLE
        self._level = 0.0
        self._elapsed = 0.0

    def update(self, dt: float) -> float:
        if self._stage is EnvelopeStage.IDLE:
            return 0.0

        self._elapsed += dt

        if self._stage is EnvelopeStage.ATTACK:
            if self.attack <= dt:
                self._level = self._amplitude  # instant attack for impacts
                self._stage = EnvelopeStage.HOLD
                self._elapsed = 0.0
            else:
                self._level = self._amplitude * (self._elapsed / self.attack)
                if self._elapsed >= self.attack:
                    self._level = self._amplitude
                    self._stage = EnvelopeStage.HOLD
                    self._elapsed = 0.0
            return self._level

        if self._stage is EnvelopeStage.HOLD:
            self._level = self._amplitude
            if self._elapsed >= self.hold:
                self._stage = EnvelopeStage.DECAY
                self._elapsed = 0.0
            return self._level

        if self._stage is EnvelopeStage.DECAY:
            progress = clamp(self._elapsed / self.decay)
            target = self.sustain_level * self._amplitude
            self._level = lerp(self._amplitude, target, progress)
            if progress >= 1.0:
                if self.sustain_level > 0.0:
                    self._stage = EnvelopeStage.SUSTAIN
                    self._level = target
                else:
                    self._stage = EnvelopeStage.IDLE
                    self._level = 0.0
            return self._level

        if self._stage is EnvelopeStage.SUSTAIN:
            self._level = self.sustain_level * self._amplitude
            return self._level

        # RELEASE
        progress = clamp(self._elapsed / self.release_time)
        self._level = lerp(self._release_from, 0.0, progress)
        if progress >= 1.0:
            self._stage = EnvelopeStage.IDLE
            self._level = 0.0
        return self._level


# --------------------------------------------------------------------------
# filters
# --------------------------------------------------------------------------
class SlewLimiter:
    """Asymmetric rate limiter, in units per second.

    Used sparingly and per effect. A global slew limiter is exactly the
    mistake that makes a haptic engine feel mushy, so the defaults here are
    deliberately fast enough to pass a transient untouched.
    """

    __slots__ = ("rise_rate", "fall_rate", "_value")

    def __init__(self, rise_rate: float = 60.0, fall_rate: float = 60.0, initial: float = 0.0) -> None:
        self.rise_rate = rise_rate
        self.fall_rate = fall_rate
        self._value = initial

    @property
    def value(self) -> float:
        return self._value

    def reset(self, value: float = 0.0) -> None:
        self._value = value

    def update(self, target: float, dt: float) -> float:
        delta = target - self._value
        if delta > 0.0:
            self._value += min(delta, self.rise_rate * dt)
        elif delta < 0.0:
            self._value -= min(-delta, self.fall_rate * dt)
        return self._value


class OnePole:
    """First-order low-pass, for the few effects that genuinely want it
    (suspension body movement, road texture) - never applied globally."""

    __slots__ = ("cutoff_hz", "_value")

    def __init__(self, cutoff_hz: float = 8.0, initial: float = 0.0) -> None:
        self.cutoff_hz = max(0.01, cutoff_hz)
        self._value = initial

    @property
    def value(self) -> float:
        return self._value

    def reset(self, value: float = 0.0) -> None:
        self._value = value

    def update(self, target: float, dt: float) -> float:
        alpha = 1.0 - math.exp(-TWO_PI * self.cutoff_hz * dt)
        self._value += (target - self._value) * clamp(alpha)
        return self._value


class PeakHold:
    """Tracks a peak and decays it - used for meters and impact detection."""

    __slots__ = ("decay_rate", "_value")

    def __init__(self, decay_rate: float = 2.0) -> None:
        self.decay_rate = decay_rate
        self._value = 0.0

    @property
    def value(self) -> float:
        return self._value

    def update(self, sample: float, dt: float) -> float:
        self._value = max(sample, self._value - self.decay_rate * dt)
        if self._value < 0.0:
            self._value = 0.0
        return self._value


class Differentiator:
    """Rate of change with a light smoothing pole, for detecting spikes
    (impacts, sudden slip) without amplifying single-sample noise."""

    __slots__ = ("_previous", "_filter", "_primed")

    def __init__(self, cutoff_hz: float = 25.0) -> None:
        self._previous = 0.0
        self._filter = OnePole(cutoff_hz)
        self._primed = False

    def reset(self) -> None:
        self._previous = 0.0
        self._filter.reset(0.0)
        self._primed = False

    def update(self, value: float, dt: float) -> float:
        if not self._primed:
            self._previous = value
            self._primed = True
            return 0.0
        if dt <= 0.0:
            return self._filter.value
        derivative = (value - self._previous) / dt
        self._previous = value
        return self._filter.update(derivative, dt)
