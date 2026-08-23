"""Game adapters.

Nothing above this package imports anything game-specific: consumers work
with normalized TelemetryFrame objects only."""

from app.games.base import AdapterStatus, GameAdapter, UnsupportedAdapter
from app.games.registry import ADAPTER_CLASSES, create_adapters, default_game_id

__all__ = [
    "AdapterStatus",
    "GameAdapter",
    "UnsupportedAdapter",
    "ADAPTER_CLASSES",
    "create_adapters",
    "default_game_id",
]
