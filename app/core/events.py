"""Minimal thread-safe publish/subscribe bus.

Used for discrete, low-frequency facts ("controller disconnected",
"telemetry lost") that must cross thread boundaries. High-frequency data
(motor levels, telemetry frames) is NOT pushed through here - the UI polls
an immutable snapshot instead, which avoids flooding the event system and
keeps the Qt thread out of the haptic hot path.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from enum import Enum
from typing import Any, Callable


class Event(str, Enum):
    CONTROLLER_CONNECTED = "controller.connected"
    CONTROLLER_DISCONNECTED = "controller.disconnected"
    CONTROLLER_ERROR = "controller.error"

    TELEMETRY_CONNECTED = "telemetry.connected"
    TELEMETRY_LOST = "telemetry.lost"
    TELEMETRY_ERROR = "telemetry.error"

    PROFILE_CHANGED = "profile.changed"
    PROFILE_SAVED = "profile.saved"

    EMERGENCY_STOP = "safety.emergency_stop"
    EMERGENCY_STOP_CLEARED = "safety.emergency_stop_cleared"
    SAFETY_CUTOFF = "safety.cutoff"


Handler = Callable[..., None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[Event, list[Handler]] = defaultdict(list)
        self._lock = threading.RLock()

    def subscribe(self, event: Event, handler: Handler) -> None:
        with self._lock:
            self._handlers[event].append(handler)

    def unsubscribe(self, event: Event, handler: Handler) -> None:
        with self._lock:
            if handler in self._handlers[event]:
                self._handlers[event].remove(handler)

    def emit(self, event: Event, **payload: Any) -> None:
        """Notify subscribers. A failing handler never breaks the emitter -
        this is called from the haptic and telemetry threads."""
        with self._lock:
            handlers = list(self._handlers[event])
        for handler in handlers:
            try:
                handler(**payload)
            except Exception:  # noqa: BLE001 - isolation is the point
                from app.core.logging import get_logger

                get_logger(__name__).exception("Event handler failed for %s", event)

    def clear(self) -> None:
        with self._lock:
            self._handlers.clear()
