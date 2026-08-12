"""Signal primitives: curves, oscillators, envelopes, filters."""

from __future__ import annotations


import pytest

from app.haptics.signal import (
    Differentiator,
    Envelope,
    NoiseSource,
    OnePole,
    Oscillator,
    SlewLimiter,
    Waveform,
    above_threshold,
    apply_response,
    balance_to_gains,
    clamp,
    evaluate,
    soft_limit,
)


class TestScalarHelpers:
    def test_clamp_bounds_and_nan(self):
        assert clamp(-1.0) == 0.0
        assert clamp(2.0) == 1.0
        assert clamp(0.4) == 0.4
        assert clamp(float("nan")) == 0.0

    def test_above_threshold_fades_in_from_zero(self):
        # The point of the helper: no pop at the threshold crossing.
        assert above_threshold(0.5, 0.5) == 0.0
        assert above_threshold(0.5, 0.0) == 0.5
        assert above_threshold(0.75, 0.5) == pytest.approx(0.5)
        assert above_threshold(1.0, 0.5) == pytest.approx(1.0)

    def test_above_threshold_full_threshold_silences(self):
        assert above_threshold(1.0, 1.0) == 0.0

    def test_apply_response_curve_direction(self):
        assert apply_response(0.5, 1.0) == pytest.approx(0.5)
        assert apply_response(0.5, 0.5) > 0.5  # more sensitive early
        assert apply_response(0.5, 2.0) < 0.5  # weighted to the top

    def test_soft_limit_saturates_below_one(self):
        assert soft_limit(0.5) == 0.5  # untouched under the knee
        assert soft_limit(1.0) < 1.0
        assert soft_limit(5.0) < 1.0
        # Must stay monotonic so louder input still feels louder.
        assert soft_limit(2.0) > soft_limit(1.2)

    def test_balance_only_attenuates(self):
        assert balance_to_gains(0.0) == (1.0, 1.0)
        assert balance_to_gains(-1.0) == (1.0, 0.0)
        assert balance_to_gains(1.0) == (0.0, 1.0)


class TestWaveforms:
    @pytest.mark.parametrize("shape", list(Waveform))
    def test_output_stays_unipolar(self, shape):
        for step in range(64):
            value = evaluate(shape, step / 64.0, sharpness=0.5)
            assert 0.0 <= value <= 1.0

    def test_sharpness_hardens_edges(self):
        # At 25% through the cycle a square is fully on while a sine is not.
        soft = evaluate(Waveform.SINE, 0.25, sharpness=0.0)
        hard = evaluate(Waveform.SINE, 0.25, sharpness=1.0)
        assert hard > soft


class TestOscillator:
    def test_phase_is_continuous_across_frequency_change(self):
        """Changing rate mid-sweep must not reset phase - that would click."""
        osc = Oscillator(Waveform.SINE, 10.0)
        for _ in range(5):
            osc.update(1 / 120, 10.0)
        before = osc.phase
        osc.update(1 / 120, 30.0)
        assert osc.phase > before  # advanced, not restarted

    def test_frequency_is_capped_at_useful_limit(self):
        osc = Oscillator(Waveform.SINE, 10.0)
        osc.update(1 / 120, 500.0)
        # Phase advance corresponds to the cap, not the requested rate.
        assert osc.phase <= 38.0 / 120 + 1e-9

    def test_completes_expected_cycles(self):
        osc = Oscillator(Waveform.SINE, 10.0)
        crossings = 0
        previous = osc.phase
        for _ in range(120):  # one second at 120 Hz
            phase = osc.update(1 / 120, 10.0) and osc.phase
            if phase < previous:
                crossings += 1
            previous = phase
        assert crossings == pytest.approx(10, abs=1)


class TestEnvelope:
    def test_one_shot_completes_and_stops(self):
        env = Envelope(attack=0.005, hold=0.01, decay=0.05)
        env.trigger(1.0)
        assert env.active

        for _ in range(200):
            env.update(1 / 120)
        assert not env.active
        assert env.level == 0.0

    def test_attack_shorter_than_a_tick_reaches_full_immediately(self):
        """An impact must be at full amplitude on the very next sample."""
        env = Envelope(attack=0.0005, hold=0.02, decay=0.2)
        env.trigger(1.0)
        assert env.update(1 / 120) == pytest.approx(1.0)

    def test_retrigger_never_quietens_an_active_envelope(self):
        env = Envelope(attack=0.001, hold=0.05, decay=0.2)
        env.trigger(1.0)
        env.update(1 / 120)
        env.trigger(0.2)  # a weaker second hit
        assert env.update(1 / 120) >= 0.9

    def test_sustain_holds_until_released(self):
        env = Envelope(attack=0.001, hold=0.0, decay=0.02, sustain_level=0.5)
        env.trigger(1.0)
        for _ in range(30):
            env.update(1 / 120)
        assert env.level == pytest.approx(0.5, abs=0.01)

        env.release()
        for _ in range(60):
            env.update(1 / 120)
        assert not env.active


class TestFilters:
    def test_slew_limiter_respects_asymmetric_rates(self):
        slew = SlewLimiter(rise_rate=1.0, fall_rate=10.0)
        assert slew.update(1.0, 0.1) == pytest.approx(0.1)  # rise capped
        slew.reset(1.0)
        assert slew.update(0.0, 0.1) == pytest.approx(0.0)  # fall reaches target

    def test_one_pole_converges_without_overshoot(self):
        pole = OnePole(10.0)
        for _ in range(200):
            value = pole.update(1.0, 1 / 120)
            assert value <= 1.0
        assert value == pytest.approx(1.0, abs=0.01)

    def test_differentiator_reports_zero_on_first_sample(self):
        diff = Differentiator()
        assert diff.update(5.0, 1 / 120) == 0.0

    def test_differentiator_detects_a_spike(self):
        diff = Differentiator(cutoff_hz=60.0)
        for _ in range(10):
            diff.update(0.0, 1 / 120)
        spike = diff.update(10.0, 1 / 120)
        assert spike > 0.0


class TestNoise:
    def test_stays_in_range(self):
        noise = NoiseSource(rate_hz=25.0, seed=1)
        for _ in range(500):
            assert 0.0 <= noise.update(1 / 120) <= 1.0

    def test_is_deterministic_for_a_given_seed(self):
        a = NoiseSource(rate_hz=25.0, seed=42)
        b = NoiseSource(rate_hz=25.0, seed=42)
        for _ in range(50):
            assert a.update(1 / 120) == b.update(1 / 120)

    def test_sample_and_hold_is_less_smooth_than_interpolated(self):
        """Gravel (harsh) must actually differ from grass (smooth)."""
        rough = NoiseSource(rate_hz=25.0, smooth=False, seed=7)
        smooth = NoiseSource(rate_hz=25.0, smooth=True, seed=7)

        def total_variation(source):
            previous = source.update(1 / 120)
            total = 0.0
            for _ in range(400):
                current = source.update(1 / 120)
                total += abs(current - previous)
                previous = current
            return total

        assert total_variation(rough) > total_variation(smooth)
