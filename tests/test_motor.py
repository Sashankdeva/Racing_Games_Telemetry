"""Motor model: dead-zone mapping, curve, slew, and hard silence."""

from __future__ import annotations

import pytest

from app.haptics.motor import SILENCE_EPSILON, Motor, MotorConfig


class TestDeadZone:
    def test_small_input_is_lifted_above_the_stall_point(self):
        """The whole point of min_effective: a quiet effect must still spin
        the rotor rather than disappearing into the dead zone."""
        motor = Motor(MotorConfig(min_effective=0.2, slew_enabled=False))
        drive = motor.update(0.05, 1 / 120)
        assert drive >= 0.2

    def test_zero_input_is_hard_silence(self):
        motor = Motor(MotorConfig(min_effective=0.2, slew_enabled=False))
        assert motor.update(0.0, 1 / 120) == 0.0

    def test_input_below_epsilon_is_silence_not_a_lifted_floor(self):
        motor = Motor(MotorConfig(min_effective=0.2, slew_enabled=False))
        assert motor.update(SILENCE_EPSILON / 2, 1 / 120) == 0.0

    def test_full_input_reaches_max_output(self):
        motor = Motor(MotorConfig(max_output=1.0, slew_enabled=False))
        assert motor.update(1.0, 1 / 120) == pytest.approx(1.0)

    def test_max_output_is_respected(self):
        motor = Motor(MotorConfig(max_output=0.6, slew_enabled=False))
        assert motor.update(1.0, 1 / 120) == pytest.approx(0.6)


class TestSlew:
    def test_default_slew_passes_a_transient_essentially_intact(self):
        """A gear shift must not be smoothed away by the motor model.

        With default rates a full-scale step completes in ~2 ticks at
        120 Hz, far quicker than the rotor's own spin-up.
        """
        motor = Motor(MotorConfig())
        dt = 1 / 120
        motor.update(1.0, dt)
        second = motor.update(1.0, dt)
        assert second >= 0.95

    def test_slew_can_be_disabled(self):
        motor = Motor(MotorConfig(slew_enabled=False, min_effective=0.0, curve=1.0))
        assert motor.update(1.0, 1 / 120) == pytest.approx(1.0)

    def test_slow_slew_actually_limits_the_rate(self):
        motor = Motor(MotorConfig(slew_rise=2.0, min_effective=0.0, curve=1.0))
        value = motor.update(1.0, 1 / 120)
        assert value == pytest.approx(2.0 / 120, abs=1e-6)

    def test_settles_exactly_on_zero(self):
        motor = Motor(MotorConfig(slew_fall=50.0))
        motor.update(1.0, 0.5)
        for _ in range(200):
            motor.update(0.0, 1 / 120)
        assert motor.drive == 0.0

    def test_snap_to_zero_bypasses_slew(self):
        motor = Motor(MotorConfig(slew_rise=1.0, slew_fall=1.0))
        motor.update(1.0, 1.0)
        motor.snap_to_zero()
        assert motor.drive == 0.0


class TestConfig:
    def test_values_are_clamped_to_sane_ranges(self):
        config = MotorConfig(
            min_effective=5.0, max_output=99.0, curve=0.0, slew_rise=-1.0
        ).clamped()
        assert 0.0 <= config.min_effective <= 0.6
        assert config.max_output <= 1.0
        assert config.curve >= 0.3
        assert config.slew_rise >= 1.0

    def test_output_is_monotonic_in_input(self):
        motor = Motor(MotorConfig(slew_enabled=False))
        previous = -1.0
        for step in range(21):
            value = motor.update(step / 20, 1 / 120)
            assert value >= previous
            previous = value

    def test_nan_input_never_reaches_the_hardware(self):
        motor = Motor(MotorConfig())
        assert motor.update(float("nan"), 1 / 120) == 0.0
