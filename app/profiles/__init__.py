"""Haptic profiles: schema, defaults and persistence."""

from app.profiles.manager import ProfileManager
from app.profiles.schema import (
    MasterConfig,
    Profile,
    builtin_profiles,
    default_effect_settings,
    slugify,
)

__all__ = [
    "ProfileManager",
    "Profile",
    "MasterConfig",
    "builtin_profiles",
    "default_effect_settings",
    "slugify",
]
