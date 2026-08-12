"""Effect registry.

Adding an effect means adding its class to EFFECT_CLASSES - the engine,
the profile schema and the Effects UI page all build themselves from this
list, so there is exactly one place to edit.
"""

from __future__ import annotations

from app.haptics.effects.abs_lock import AbsLockEffect
from app.haptics.effects.acceleration import AccelerationEffect
from app.haptics.effects.base import (
    Effect,
    EffectOutput,
    EffectSettings,
    wheels_to_lr,
)
from app.haptics.effects.braking import BrakingEffect
from app.haptics.effects.collision import CollisionEffect
from app.haptics.effects.engine_rpm import EngineRpmEffect
from app.haptics.effects.gear_shift import GearShiftEffect
from app.haptics.effects.kerb import KerbEffect
from app.haptics.effects.suspension import SuspensionEffect
from app.haptics.effects.surface import RoadTextureEffect, SurfaceEffect
from app.haptics.effects.wheelspin import WheelspinEffect

#: Display order on the Effects page - loudest/most important first.
EFFECT_CLASSES: tuple[type[Effect], ...] = (
    EngineRpmEffect,
    GearShiftEffect,
    KerbEffect,
    AbsLockEffect,
    WheelspinEffect,
    CollisionEffect,
    SurfaceEffect,
    SuspensionEffect,
    BrakingEffect,
    AccelerationEffect,
    RoadTextureEffect,
)

EFFECTS_BY_ID: dict[str, type[Effect]] = {cls.id: cls for cls in EFFECT_CLASSES}


def create_all(settings_by_id: dict[str, EffectSettings] | None = None) -> list[Effect]:
    """Instantiate every registered effect, applying settings where given."""
    settings_by_id = settings_by_id or {}
    effects: list[Effect] = []
    for cls in EFFECT_CLASSES:
        settings = settings_by_id.get(cls.id)
        effects.append(cls(settings.copy() if settings else None))
    return effects


__all__ = [
    "EFFECT_CLASSES",
    "EFFECTS_BY_ID",
    "Effect",
    "EffectOutput",
    "EffectSettings",
    "create_all",
    "wheels_to_lr",
    "AbsLockEffect",
    "AccelerationEffect",
    "BrakingEffect",
    "CollisionEffect",
    "EngineRpmEffect",
    "GearShiftEffect",
    "KerbEffect",
    "RoadTextureEffect",
    "SurfaceEffect",
    "SuspensionEffect",
    "WheelspinEffect",
]
