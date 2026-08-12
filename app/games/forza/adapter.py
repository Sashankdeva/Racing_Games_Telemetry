"""Forza adapter - deliberately not implemented.

This file exists to prove the seam, not to pretend support exists. It
reports itself as unsupported and produces no telemetry, so the Games page
can state the truth rather than showing a fake "connected" state.

What implementing it would actually involve:

  1. Subclass GameAdapter, set `supported = True`.
  2. Add a UDP listener for Forza's "Data Out" stream (default port 5300).
     Forza sends a single fixed-layout struct per frame rather than F1's
     tagged multi-packet scheme, so it needs its own packets/parser pair -
     mirroring games/f1/packets.py and games/f1/parser.py.
  3. Translate that struct into TelemetryFrame. Forza provides most of what
     the effects want directly: rpm with idle/max, per-wheel slip ratios,
     per-wheel surface rumble, suspension travel, and g-forces.
  4. Register the class in games/registry.py.

Nothing in app/haptics/ needs to change - effects consume TelemetryFrame
only, so every existing effect works the moment frames start arriving. The
one honest gap is surface classification: Forza reports a normalized rumble
value per wheel rather than F1's surface enum, so `surfaces` should be left
UNKNOWN and the surface effect driven from that value instead of inventing
a category.
"""

from __future__ import annotations

from app.games.base import UnsupportedAdapter


class ForzaAdapter(UnsupportedAdapter):
    def __init__(self) -> None:
        super().__init__(
            game_id="forza",
            display_name="Forza Motorsport / Horizon",
            description=(
                "Architecture ready. The haptic engine and every effect already "
                "work with any adapter - only the Forza telemetry reader itself "
                "is outstanding."
            ),
        )
