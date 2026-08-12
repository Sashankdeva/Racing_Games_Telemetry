"""Profile data model, defaults, and tolerant (de)serialization.

Loading is deliberately forgiving: unknown keys are ignored and missing
keys fall back to defaults. A profile written by a newer build, or one a
user has hand-edited badly, degrades to something usable instead of
refusing to load and leaving the app with no configuration.

The built-in profiles are defined here in code. Editing one writes a copy
to disk that shadows the code version; "Reset" deletes that copy and the
original comes back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.haptics.effects import EFFECT_CLASSES
from app.haptics.effects.base import EffectSettings
from app.haptics.motor import MotorConfig

SCHEMA_VERSION = 1


@dataclass(slots=True)
class MasterConfig:
    """Global haptic shaping - mirrors engine.MasterSettings."""

    intensity: float = 1.0
    dynamic_range: float = 1.0
    feel: float = 0.55
    response: float = 0.85
    #: Off by default; per-effect processing is preferred (see Haptics page).
    global_smoothing: float = 0.0
    output_limit: float = 1.0


@dataclass(slots=True)
class Profile:
    name: str = "Default"
    description: str = ""
    builtin: bool = False
    version: int = SCHEMA_VERSION
    master: MasterConfig = field(default_factory=MasterConfig)
    motor: MotorConfig = field(default_factory=MotorConfig)
    effects: dict[str, EffectSettings] = field(default_factory=dict)

    # --- helpers ----------------------------------------------------------
    @property
    def slug(self) -> str:
        return slugify(self.name)

    def effect(self, effect_id: str) -> EffectSettings:
        settings = self.effects.get(effect_id)
        if settings is None:
            settings = EffectSettings()
            self.effects[effect_id] = settings
        return settings

    def copy(self, new_name: str | None = None) -> "Profile":
        return Profile(
            name=new_name or self.name,
            description=self.description,
            builtin=False,
            version=self.version,
            master=MasterConfig(**{
                key: getattr(self.master, key) for key in MasterConfig.__slots__
            }),
            motor=MotorConfig(**{
                key: getattr(self.motor, key) for key in MotorConfig.__slots__
            }),
            effects={eid: s.copy() for eid, s in self.effects.items()},
        )

    # --- serialization ----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "name": self.name,
            "description": self.description,
            "master": {key: getattr(self.master, key) for key in MasterConfig.__slots__},
            "motor": {key: getattr(self.motor, key) for key in MotorConfig.__slots__},
            "effects": {
                effect_id: {
                    "enabled": s.enabled,
                    "intensity": s.intensity,
                    "threshold": s.threshold,
                    "response": s.response,
                    "sharpness": s.sharpness,
                    "balance": s.balance,
                    "advanced": dict(s.advanced),
                }
                for effect_id, s in self.effects.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], builtin: bool = False) -> "Profile":
        if not isinstance(data, dict):
            raise ValueError("Profile data must be an object")

        name = str(data.get("name") or "Unnamed").strip() or "Unnamed"
        profile = cls(
            name=name,
            description=str(data.get("description") or ""),
            builtin=builtin,
            version=_as_int(data.get("schema_version"), SCHEMA_VERSION),
        )

        master_data = data.get("master")
        if isinstance(master_data, dict):
            for key in MasterConfig.__slots__:
                if key in master_data:
                    setattr(profile.master, key, _as_float(master_data[key], getattr(profile.master, key)))

        motor_data = data.get("motor")
        if isinstance(motor_data, dict):
            for key in MotorConfig.__slots__:
                if key not in motor_data:
                    continue
                current = getattr(profile.motor, key)
                if isinstance(current, bool):
                    setattr(profile.motor, key, bool(motor_data[key]))
                else:
                    setattr(profile.motor, key, _as_float(motor_data[key], current))

        effects_data = data.get("effects")
        if isinstance(effects_data, dict):
            for effect_id, raw in effects_data.items():
                if not isinstance(raw, dict):
                    continue
                profile.effects[str(effect_id)] = _effect_from_dict(raw)

        profile.normalize()
        return profile

    def normalize(self) -> None:
        """Guarantee an entry for every registered effect, and clamp values.

        Effects added in a later build simply appear with their defaults
        rather than being silently absent from an older profile.
        """
        for cls in EFFECT_CLASSES:
            if cls.id not in self.effects:
                self.effects[cls.id] = default_effect_settings(cls.id)

        # Drop settings for effects that no longer exist.
        known = {cls.id for cls in EFFECT_CLASSES}
        for effect_id in list(self.effects):
            if effect_id not in known:
                del self.effects[effect_id]

        self.master.intensity = _clamp(self.master.intensity, 0.0, 1.5)
        self.master.dynamic_range = _clamp(self.master.dynamic_range, 0.0, 1.0)
        self.master.feel = _clamp(self.master.feel, 0.0, 1.0)
        self.master.response = _clamp(self.master.response, 0.0, 1.0)
        self.master.global_smoothing = _clamp(self.master.global_smoothing, 0.0, 1.0)
        self.master.output_limit = _clamp(self.master.output_limit, 0.1, 1.0)
        self.motor = self.motor.clamped()

        for settings in self.effects.values():
            settings.intensity = _clamp(settings.intensity, 0.0, 2.0)
            settings.threshold = _clamp(settings.threshold, 0.0, 0.95)
            settings.response = _clamp(settings.response, 0.3, 3.0)
            settings.sharpness = _clamp(settings.sharpness, 0.0, 1.0)
            settings.balance = _clamp(settings.balance, -1.0, 1.0)


# --------------------------------------------------------------------------
# defaults
# --------------------------------------------------------------------------
#: Per-effect defaults, tuned so the stock profile already feels right.
#: Sharp events sit at full strength; the continuous beds underneath them
#: are held well back so they support rather than mask.
_DEFAULTS: dict[str, dict[str, float]] = {
    # No threshold: the engine is continuous, and any gate makes it
    # pop in and out around idle instead of fading naturally.
    "engine_rpm": {"intensity": 1.00, "threshold": 0.00, "sharpness": 0.35},
    "gear_shift": {"intensity": 1.00, "threshold": 0.00, "sharpness": 0.85},
    "kerb": {"intensity": 1.00, "threshold": 0.00, "sharpness": 0.85},
    "abs_lock": {"intensity": 0.95, "threshold": 0.05, "sharpness": 0.90},
    "wheelspin": {"intensity": 0.85, "threshold": 0.06, "sharpness": 0.60},
    "collision": {"intensity": 1.00, "threshold": 0.08, "sharpness": 0.90},
    "surface": {"intensity": 0.90, "threshold": 0.00, "sharpness": 0.70},
    "suspension": {"intensity": 0.55, "threshold": 0.10, "sharpness": 0.40},
    "braking": {"intensity": 0.50, "threshold": 0.10, "sharpness": 0.40},
    "acceleration": {"intensity": 0.45, "threshold": 0.14, "sharpness": 0.35},
    "road_texture": {"intensity": 0.50, "threshold": 0.16, "sharpness": 0.40},
}


def default_effect_settings(effect_id: str) -> EffectSettings:
    values = _DEFAULTS.get(effect_id, {})
    return EffectSettings(
        enabled=True,
        intensity=values.get("intensity", 1.0),
        threshold=values.get("threshold", 0.0),
        response=values.get("response", 1.0),
        sharpness=values.get("sharpness", 0.5),
        balance=0.0,
    )


def _base_profile(name: str, description: str) -> Profile:
    profile = Profile(name=name, description=description, builtin=True)
    profile.normalize()
    return profile


def builtin_profiles() -> list[Profile]:
    """The profiles shipped with the app, in display order."""
    profiles: list[Profile] = []

    default = _base_profile(
        "Default",
        "Balanced all-round feel. A good starting point for any car and track.",
    )
    profiles.append(default)

    realistic = _base_profile(
        "F1 Realistic",
        "Weighted toward what the car is physically doing. Sharp events stay "
        "sharp; the engine bed sits back so kerbs and locking read clearly.",
    )
    realistic.master.feel = 0.60
    realistic.master.response = 0.90
    realistic.effect("engine_rpm").intensity = 0.85
    realistic.effect("road_texture").intensity = 0.40
    realistic.effect("suspension").intensity = 0.65
    realistic.effect("acceleration").intensity = 0.50
    profiles.append(realistic)

    strong = _base_profile(
        "F1 Strong",
        "Everything pushed harder, with a firmer motor curve. Best if your "
        "controller's motors feel weak or you race with a lot of wheel noise.",
    )
    strong.master.intensity = 1.30
    strong.master.feel = 0.80
    strong.master.response = 0.95
    strong.motor.min_effective = 0.20
    for effect_id in ("engine_rpm", "kerb", "abs_lock", "wheelspin", "surface"):
        strong.effect(effect_id).intensity = min(2.0, strong.effect(effect_id).intensity * 1.25)
    strong.effect("suspension").intensity = 0.75
    strong.effect("braking").intensity = 0.70
    profiles.append(strong)

    subtle = _base_profile(
        "F1 Subtle",
        "Quiet and informative. Impacts and locking still cut through, but "
        "the continuous rumble is dialled well back for long stints.",
    )
    subtle.master.intensity = 0.70
    subtle.master.feel = 0.40
    subtle.master.dynamic_range = 0.85
    subtle.effect("engine_rpm").intensity = 0.55
    subtle.effect("engine_rpm").threshold = 0.15
    subtle.effect("road_texture").intensity = 0.25
    subtle.effect("acceleration").enabled = False
    subtle.effect("braking").intensity = 0.30
    subtle.effect("suspension").intensity = 0.35
    subtle.effect("surface").intensity = 0.70
    profiles.append(subtle)

    custom = _base_profile(
        "Custom",
        "Your own settings. Starts as a copy of Default - change anything.",
    )
    custom.builtin = True
    profiles.append(custom)

    return profiles


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "profile"


def _clamp(value: float, low: float, high: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return low
    if value != value:
        return low
    return low if value < low else high if value > high else value


def _as_float(value: Any, fallback: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return fallback if result != result else result


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _effect_from_dict(raw: dict[str, Any]) -> EffectSettings:
    advanced = raw.get("advanced")
    return EffectSettings(
        enabled=bool(raw.get("enabled", True)),
        intensity=_as_float(raw.get("intensity"), 1.0),
        threshold=_as_float(raw.get("threshold"), 0.0),
        response=_as_float(raw.get("response"), 1.0),
        sharpness=_as_float(raw.get("sharpness"), 0.5),
        balance=_as_float(raw.get("balance"), 0.0),
        advanced=dict(advanced) if isinstance(advanced, dict) else {},
    )
