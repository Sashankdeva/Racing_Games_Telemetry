"""Game modes and their version-specific configuration.

    Application
        -> GameMode (F1 25 / F1 26)
            -> ERS config, DRS config, strategy params,
               capabilities, terminology, packet expectations
        -> shared normalized model, shared engine, shared UI

Only *game-specific* facts live here. The UI and the future strategy engine
read this configuration rather than branching on the mode themselves, so
there is exactly one DRS implementation and one ERS model, parameterised
per title.

Honesty rule, applied throughout:

The 2026 regulations are public and genuinely different - the MGU-H is
gone, electrical deployment roughly doubles, and DRS is replaced by active
aero plus a manual override. Those are represented here because they are
real, published facts about the ruleset.

What is NOT asserted is how any of that surfaces in the *telemetry stream*.
F1 26 content running inside the F1 25 framework has not been observed here
packet-by-packet, so every field whose availability or meaning is unverified
is marked UNCONFIRMED rather than claimed. The UI shows unconfirmed fields
and labels them; it never fabricates a value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class GameMode(str, Enum):
    F1_25 = "f1_25"
    F1_26 = "f1_26"

    @property
    def label(self) -> str:
        return {GameMode.F1_25: "F1 25", GameMode.F1_26: "F1 26"}[self]

    @classmethod
    def parse(cls, value: str | None) -> "GameMode":
        """Never raises - an unknown or missing mode falls back to F1 25."""
        if not value:
            return cls.F1_25
        try:
            return cls(str(value))
        except ValueError:
            return cls.F1_25


class Capability(str, Enum):
    """A telemetry field group or feature the active game may provide."""

    CORE_TELEMETRY = "core_telemetry"
    DRS = "drs"
    ACTIVE_AERO = "active_aero"
    MANUAL_OVERRIDE = "manual_override"
    TYRE_TEMPS = "tyre_temps"
    TYRE_PRESSURE = "tyre_pressure"
    TYRE_WEAR = "tyre_wear"
    TYRE_COMPOUND = "tyre_compound"
    BRAKE_TEMPS = "brake_temps"
    FUEL = "fuel"
    ERS = "ers"
    ERS_MGUH = "ers_mguh"
    LAP_TIMING = "lap_timing"
    LAP_DELTAS = "lap_deltas"
    POSITION = "position"
    DAMAGE = "damage"
    WEATHER = "weather"
    SESSION_INFO = "session_info"
    MOTION = "motion"
    SURFACE_TYPE = "surface_type"
    OPPONENTS = "opponents"

    STRATEGY = "strategy"
    COACH = "coach"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


@dataclass(frozen=True, slots=True)
class ErsConfig:
    """Energy recovery configuration for one title.

    The shared engine consumes this normalized model; it never branches on
    the game itself. Values that describe the *ruleset* are asserted;
    anything about how the game reports them stays unconfirmed.
    """

    #: Deploy modes the game exposes, in ascending aggression.
    modes: tuple[str, ...] = ("None", "Medium", "Hotlap", "Overtake")
    #: Usable store, in joules, per lap under the regulations.
    store_joules: float = 4_000_000.0
    #: Peak deployment power in kW - the headline 2026 change.
    max_deploy_kw: float = 120.0
    #: MGU-H present. Removed for 2026.
    has_mguh: bool = True
    #: Roughly how much of total power is electrical.
    electrical_share: float = 0.2
    #: What the game calls it.
    label: str = "ERS"
    deploy_label: str = "Deploy mode"
    #: True when the mapping of these onto telemetry is unverified here.
    telemetry_unconfirmed: bool = False
    notes: str = ""


@dataclass(frozen=True, slots=True)
class DrsConfig:
    """Drag-reduction / straight-line configuration for one title.

    One implementation, parameterised - not a DRS module per game.
    """

    #: False for 2026, where DRS is replaced by active aero.
    has_drs: bool = True
    #: 2026 active aero: selectable low-drag / high-downforce wing states.
    has_active_aero: bool = False
    #: 2026 replacement for the DRS overtake aid.
    has_manual_override: bool = False
    #: Aero modes the driver can select, if any.
    aero_modes: tuple[str, ...] = ()
    #: What the UI should call the straight-line aid.
    label: str = "DRS"
    #: Short description shown next to the setting.
    description: str = "Drag Reduction System - rear wing opens in a DRS zone."
    #: Gap to the car ahead, in seconds, that enables the aid.
    activation_gap_s: float = 1.0
    telemetry_unconfirmed: bool = False
    notes: str = ""


@dataclass(frozen=True, slots=True)
class StrategyParams:
    """Per-title strategy assumptions.

    Deliberately coarse starting values. The strategy engine (not yet
    built) reads these instead of embedding constants, so a rules change is
    a data edit rather than a code change.
    """

    #: Typical pit lane loss if the track profile has no better figure.
    default_pit_loss_s: float = 21.0
    #: Compounds available in a normal dry race weekend.
    dry_compounds: tuple[str, ...] = ("Soft", "Medium", "Hard")
    #: Mandatory compound changes in a dry race.
    mandatory_compounds: int = 1
    #: Fuel-saving potential per lap, as a fraction of race pace.
    fuel_save_potential: float = 0.01
    #: How much of a lap's time advantage ERS deployment can provide.
    ers_lap_advantage_s: float = 0.3
    #: Confidence in the above for this title.
    confidence: float = 0.3
    notes: str = ""


#: Capabilities verified against real packet layouts (F1 25 lineage).
_VERIFIED = frozenset({
    Capability.CORE_TELEMETRY,
    Capability.DRS,
    Capability.TYRE_TEMPS,
    Capability.TYRE_PRESSURE,
    Capability.TYRE_WEAR,
    Capability.TYRE_COMPOUND,
    Capability.BRAKE_TEMPS,
    Capability.FUEL,
    Capability.ERS,
    Capability.ERS_MGUH,
    Capability.LAP_TIMING,
    Capability.LAP_DELTAS,
    Capability.POSITION,
    Capability.DAMAGE,
    Capability.WEATHER,
    Capability.SESSION_INFO,
    Capability.MOTION,
    Capability.SURFACE_TYPE,
})

_NOT_IMPLEMENTED = frozenset({
    Capability.OPPONENTS,
    Capability.STRATEGY,
    Capability.COACH,
})

# F1 26: same core telemetry, minus MGU-H (removed by the regulations),
# plus active aero and manual override whose telemetry representation we
# have not verified.
_F1_26_VERIFIED = _VERIFIED - {Capability.ERS_MGUH, Capability.DRS}
_F1_26_UNCONFIRMED = frozenset({
    Capability.ACTIVE_AERO,
    Capability.MANUAL_OVERRIDE,
})


@dataclass(frozen=True, slots=True)
class GameProfile:
    """Everything version-specific about one game."""

    mode: GameMode
    display_name: str
    expected_formats: tuple[int, ...]
    capabilities: frozenset[Capability]
    ers: ErsConfig
    drs: DrsConfig
    strategy: StrategyParams
    unconfirmed: frozenset[Capability] = frozenset()
    unavailable: frozenset[Capability] = frozenset()
    default_port: int = 20777
    terminology: dict[str, str] = field(default_factory=dict)
    notes: str = ""

    def supports(self, capability: Capability) -> bool:
        """True if the field/feature should be shown with real values."""
        return capability in self.capabilities or capability in self.unconfirmed

    def is_unconfirmed(self, capability: Capability) -> bool:
        return capability in self.unconfirmed

    def status(self, capability: Capability) -> str:
        if capability in self.capabilities:
            return "available"
        if capability in self.unconfirmed:
            return "unconfirmed"
        return "unavailable"

    def term(self, key: str, default: str = "") -> str:
        """Game-specific wording, e.g. 'DRS' vs 'Manual Override'."""
        return self.terminology.get(key, default or key)

    def format_matches(self, packet_format: int) -> bool:
        return packet_format in self.expected_formats


_PROFILES: dict[GameMode, GameProfile] = {
    GameMode.F1_25: GameProfile(
        mode=GameMode.F1_25,
        display_name="F1 25",
        expected_formats=(2025,),
        capabilities=_VERIFIED,
        unavailable=_NOT_IMPLEMENTED | {Capability.ACTIVE_AERO, Capability.MANUAL_OVERRIDE},
        ers=ErsConfig(
            modes=("None", "Medium", "Hotlap", "Overtake"),
            store_joules=4_000_000.0,
            max_deploy_kw=120.0,
            has_mguh=True,
            electrical_share=0.2,
            label="ERS",
            deploy_label="Deploy mode",
            notes="2014-2025 power unit: MGU-K + MGU-H, 4 MJ store, 120 kW deployment.",
        ),
        drs=DrsConfig(
            has_drs=True,
            label="DRS",
            description="Drag Reduction System - rear wing opens within 1s of the car ahead in a DRS zone.",
            activation_gap_s=1.0,
        ),
        strategy=StrategyParams(
            default_pit_loss_s=21.0,
            dry_compounds=("Soft", "Medium", "Hard"),
            mandatory_compounds=1,
            ers_lap_advantage_s=0.3,
            confidence=0.35,
            notes="Established ruleset; assumptions are reasonably well understood.",
        ),
        terminology={
            "drs": "DRS",
            "drs_zone": "DRS Zone",
            "ers": "ERS",
            "ers_deploy": "Deploy Mode",
            "overtake": "Overtake",
        },
        notes="Packet format 2025. Layouts verified against the published spec.",
    ),
    GameMode.F1_26: GameProfile(
        mode=GameMode.F1_26,
        display_name="F1 26",
        # F1 26 content inside the F1 25 framework may report either.
        expected_formats=(2026, 2025),
        capabilities=_F1_26_VERIFIED,
        unconfirmed=_F1_26_UNCONFIRMED,
        unavailable=_NOT_IMPLEMENTED | {Capability.DRS, Capability.ERS_MGUH},
        ers=ErsConfig(
            modes=("None", "Medium", "Hotlap", "Overtake"),
            store_joules=9_000_000.0,
            max_deploy_kw=350.0,
            has_mguh=False,
            electrical_share=0.5,
            label="ERS",
            deploy_label="Energy management",
            telemetry_unconfirmed=True,
            notes=(
                "2026 power unit: MGU-H removed, electrical output raised to "
                "roughly a 50/50 split with the ICE and deployment to ~350 kW. "
                "Those are published regulation facts. How the game reports "
                "store size and deploy modes is NOT yet verified here."
            ),
        ),
        drs=DrsConfig(
            has_drs=False,
            has_active_aero=True,
            has_manual_override=True,
            aero_modes=("Z-mode (high downforce)", "X-mode (low drag)"),
            label="Active Aero / Override",
            description=(
                "2026 replaces DRS with movable front and rear wings (X-mode "
                "low drag on straights, Z-mode high downforce in corners) plus "
                "a Manual Override energy boost for attacking."
            ),
            activation_gap_s=1.0,
            telemetry_unconfirmed=True,
            notes=(
                "Regulation change is public. Whether the game exposes aero "
                "state or override as telemetry, and in which field, is "
                "unverified - hence UNCONFIRMED rather than a claim."
            ),
        ),
        strategy=StrategyParams(
            default_pit_loss_s=21.0,
            dry_compounds=("Soft", "Medium", "Hard"),
            mandatory_compounds=1,
            # Higher deployment means energy management matters more, but by
            # how much is a guess until observed - hence the low confidence.
            ers_lap_advantage_s=0.5,
            confidence=0.1,
            notes=(
                "New ruleset. Degradation, energy management and overtaking "
                "behaviour are largely unknown until observed, so strategy "
                "confidence starts very low."
            ),
        ),
        terminology={
            "drs": "Manual Override",
            "drs_zone": "Override",
            "ers": "ERS",
            "ers_deploy": "Energy Management",
            "overtake": "Manual Override",
        },
        notes=(
            "Packet format 2026 (or 2025 when run through the F1 25 "
            "framework). Header and core layouts are unchanged from 2025. "
            "Regulation differences are configured above; telemetry-level "
            "differences are marked unconfirmed until observed."
        ),
    ),
}


def game_profile(mode: GameMode) -> GameProfile:
    return _PROFILES[mode]


def all_profiles() -> list[GameProfile]:
    return [_PROFILES[mode] for mode in GameMode]
