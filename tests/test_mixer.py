"""Mixer: priority ducking, soft limiting, and channel independence."""

from __future__ import annotations

import pytest

from app.haptics.mixer import Contribution, HapticMixer


@pytest.fixture
def mixer():
    return HapticMixer()


class TestBasics:
    def test_no_contributions_is_silence(self, mixer):
        result = mixer.mix([])
        assert (result.left, result.right) == (0.0, 0.0)
        assert result.active == []

    def test_single_contribution_passes_through(self, mixer):
        result = mixer.mix([Contribution("a", left=0.5, right=0.5)])
        assert result.left == pytest.approx(0.5)
        assert result.right == pytest.approx(0.5)

    def test_channels_are_independent(self, mixer):
        result = mixer.mix([Contribution("a", left=0.8, right=0.0)])
        assert result.left > 0.7
        assert result.right == 0.0

    def test_output_below_the_knee_is_untouched(self, mixer):
        """Normal levels must pass through with no compression at all."""
        result = mixer.mix([Contribution("a", left=0.6, right=0.6)])
        assert result.left == pytest.approx(0.6)

    def test_a_single_full_effect_keeps_almost_all_its_peak(self, mixer):
        """Peak matters more than over-unity contrast on weak ERM motors."""
        result = mixer.mix([Contribution("a", left=1.0, right=1.0)])
        assert result.left > 0.95


class TestPriorityDucking:
    def test_full_strength_dominant_effect_silences_everything_below(self, mixer):
        """A hard collision must own the controller for its duration.

        Tested as an equivalence rather than an absolute level: adding a
        full-strength engine bed underneath a full-strength collision must
        change nothing, because the collision consumed all the headroom.
        """
        collision = Contribution("collision", left=1.0, right=1.0, priority=100, dominance=1.0)
        alone = mixer.mix([collision])
        with_engine = mixer.mix([
            Contribution("engine", left=1.0, right=1.0, priority=10, dominance=0.2),
            collision,
        ])
        assert with_engine.left == pytest.approx(alone.left)
        assert with_engine.active[0] == "collision"

    def test_low_dominance_effect_lets_others_through(self, mixer):
        """The engine bed must not mask kerbs - that is why dominance is low."""
        engine_only = mixer.mix([
            Contribution("engine", left=0.5, right=0.5, priority=10, dominance=0.22)
        ])
        with_kerb = mixer.mix([
            Contribution("engine", left=0.5, right=0.5, priority=10, dominance=0.22),
            Contribution("kerb", left=0.8, right=0.0, priority=45, dominance=0.5),
        ])
        assert with_kerb.left > engine_only.left

    def test_two_effects_combine_rather_than_replacing(self, mixer):
        result = mixer.mix([
            Contribution("a", left=0.4, right=0.4, priority=10, dominance=0.2),
            Contribution("b", left=0.4, right=0.4, priority=20, dominance=0.2),
        ])
        assert result.left > 0.4  # not a plain max()
        assert result.left <= 1.0

    def test_priority_order_decides_who_ducks_whom(self, mixer):
        low_first = mixer.mix([
            Contribution("quiet", left=0.3, right=0.3, priority=5, dominance=0.9),
            Contribution("loud", left=0.9, right=0.9, priority=90, dominance=0.9),
        ])
        # The high-priority effect is applied first and keeps its full level.
        assert low_first.left >= 0.9 - 0.05


class TestSidechainDuck:
    """A transient must be able to punch a hole in the bed, not just add to it."""

    def test_duck_suppresses_lower_priority_effects(self, mixer):
        bed = Contribution("engine", left=0.8, right=0.8, priority=10, dominance=0.22)
        unducked = mixer.mix([bed]).left
        ducked = mixer.mix([
            bed,
            Contribution("shift", left=0.0, right=0.0, priority=60, duck=0.9),
        ]).left
        assert ducked < unducked * 0.3

    def test_duck_applies_even_when_the_effect_is_silent(self, mixer):
        """The trailing duck after a hit is the entire point."""
        result = mixer.mix([
            Contribution("engine", left=0.8, right=0.8, priority=10, dominance=0.22),
            Contribution("shift", left=0.0, right=0.0, priority=60, duck=1.0),
        ])
        assert result.left < 0.05

    def test_duck_does_not_affect_higher_priority_effects(self, mixer):
        result = mixer.mix([
            Contribution("collision", left=0.9, right=0.9, priority=100, dominance=1.0),
            Contribution("shift", left=0.0, right=0.0, priority=60, duck=1.0),
        ])
        assert result.left > 0.8

    def test_zero_duck_leaves_mixing_unchanged(self, mixer):
        contributions = [
            Contribution("engine", left=0.8, right=0.8, priority=10, dominance=0.22),
            Contribution("kerb", left=0.5, right=0.5, priority=45, dominance=0.5),
        ]
        without = mixer.mix(contributions).left
        with_zero = mixer.mix([
            Contribution(c.effect_id, c.left, c.right, c.priority, c.dominance, duck=0.0)
            for c in contributions
        ]).left
        assert without == pytest.approx(with_zero)

    def test_a_gear_shift_creates_real_contrast_against_a_loud_bed(self, mixer):
        """The bug this mechanism exists to fix: at high revs a shift used
        to add only ~0.2 over the engine bed and felt mushy."""
        from app.core.models import TelemetryFrame
        from app.haptics.effects.gear_shift import GearShiftEffect

        dt = 1 / 120
        effect = GearShiftEffect()
        effect.update(dt, TelemetryFrame(valid=True, rpm=11000, max_rpm=12000, gear=4))
        shifted = TelemetryFrame(valid=True, rpm=11000, max_rpm=12000, gear=5)

        levels = []
        for _ in range(60):
            output = effect.update(dt, shifted)
            levels.append(
                mixer.mix([
                    Contribution("engine", left=0.8, right=0.8, priority=10, dominance=0.22),
                    Contribution(
                        "gear_shift", output.left, output.right,
                        priority=60, dominance=0.7, duck=effect.duck(),
                    ),
                ]).left
            )

        assert max(levels) > 0.9        # the hit lands
        assert min(levels) < 0.65       # and leaves a hole in the bed afterwards
        assert max(levels) - min(levels) > 0.3

    def test_the_hit_lasts_long_enough_for_the_rotor_to_respond(self):
        """An envelope shorter than ERM spin-up never physically happens."""
        from app.core.models import TelemetryFrame
        from app.haptics.effects.gear_shift import GearShiftEffect

        dt = 1 / 120
        effect = GearShiftEffect()
        effect.update(dt, TelemetryFrame(valid=True, rpm=11000, max_rpm=12000, gear=4))
        shifted = TelemetryFrame(valid=True, rpm=11000, max_rpm=12000, gear=5)

        levels = [effect.update(dt, shifted).left for _ in range(60)]
        peak = max(levels)
        near_peak = sum(1 for value in levels if value > 0.8 * peak) * dt
        assert near_peak >= GearShiftEffect.ROTOR_SPINUP


class TestLimiting:
    def test_output_never_exceeds_one(self, mixer):
        result = mixer.mix([
            Contribution(f"e{i}", left=1.0, right=1.0, priority=i, dominance=0.0)
            for i in range(6)
        ])
        assert result.left <= 1.0
        assert result.right <= 1.0

    def test_limiter_flag_is_reported(self, mixer):
        result = mixer.mix([
            Contribution("a", left=1.0, right=1.0, priority=1, dominance=0.0),
            Contribution("b", left=1.0, right=1.0, priority=2, dominance=0.0),
        ])
        assert result.limited is True

    def test_contrast_survives_saturation(self, mixer):
        """Soft limiting, not clipping: 'huge' must still exceed 'loud'."""
        loud = mixer.mix([Contribution("a", left=0.9, right=0.9, dominance=0.0)])
        huge = mixer.mix([
            Contribution("a", left=0.9, right=0.9, priority=1, dominance=0.0),
            Contribution("b", left=0.9, right=0.9, priority=2, dominance=0.0),
        ])
        assert huge.left > loud.left


class TestActiveReporting:
    def test_active_list_is_ordered_by_priority(self, mixer):
        result = mixer.mix([
            Contribution("low", left=0.5, right=0.5, priority=1),
            Contribution("high", left=0.5, right=0.5, priority=99),
            Contribution("mid", left=0.5, right=0.5, priority=50),
        ])
        assert result.active == ["high", "mid", "low"]

    def test_inaudible_contributions_are_not_listed(self, mixer):
        result = mixer.mix([
            Contribution("audible", left=0.5, right=0.5),
            Contribution("silent", left=0.0001, right=0.0),
        ])
        assert result.active == ["audible"]
