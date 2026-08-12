"""Available game adapters.

Registering an adapter here is the only wiring a new game needs - the Games
page and the application both build themselves from this list.
"""

from __future__ import annotations

from app.games.base import GameAdapter
from app.games.f1.adapter import F1Adapter
from app.games.forza.adapter import ForzaAdapter

ADAPTER_CLASSES: tuple[type[GameAdapter], ...] = (F1Adapter, ForzaAdapter)


def create_adapters() -> list[GameAdapter]:
    return [cls() for cls in ADAPTER_CLASSES]


def default_game_id() -> str:
    for cls in ADAPTER_CLASSES:
        if cls.supported:
            return cls.game_id
    return ADAPTER_CLASSES[0].game_id
