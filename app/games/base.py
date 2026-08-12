"""Game adapter interface.

A game adapter owns everything game-specific: its transport (UDP, shared
memory, whatever), its packet layouts, and the translation into the
normalized TelemetryFrame. It hands finished frames to a callback and knows
nothing about haptics.

Adding a game means implementing this interface and registering it. The
haptic engine, the effects and the UI require no changes - that is the
whole point of the split.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable

from app.core.models import TelemetryFrame

FrameCallback = Callable[[TelemetryFrame], None]


class TelemetryStage(IntEnum):
    """How far the telemetry pipeline has actually got.

    Exists because "the socket is open" was being read as "data is
    arriving". Those are very different states and only an explicit ladder
    makes the difference visible: a bound socket with zero packets is a
    game-configuration problem, whereas packets arriving with no valid
    frames is a parsing problem. Ordered so comparisons work.
    """

    ERROR = 0
    WAITING = 1  # adapter not started
    SOCKET_BOUND = 2  # listening, nothing received yet
    PACKETS_RECEIVED = 3  # raw datagrams arriving, none understood
    PACKETS_PARSED = 4  # headers/payloads decode, no frame emitted yet
    TELEMETRY_VALID = 5  # frames produced, but gone quiet since
    TELEMETRY_LIVE = 6  # frames flowing right now

    @property
    def label(self) -> str:
        return {
            TelemetryStage.ERROR: "Error",
            TelemetryStage.WAITING: "Waiting",
            TelemetryStage.SOCKET_BOUND: "UDP socket bound - no packets",
            TelemetryStage.PACKETS_RECEIVED: "Packets received - none parsed",
            TelemetryStage.PACKETS_PARSED: "Packets parsed - no frame yet",
            TelemetryStage.TELEMETRY_VALID: "Telemetry valid - stalled",
            TelemetryStage.TELEMETRY_LIVE: "Telemetry live",
        }[self]


@dataclass(frozen=True, slots=True)
class AdapterStatus:
    game_id: str
    display_name: str
    running: bool = False
    connected: bool = False
    packets_received: int = 0
    packets_rejected: int = 0
    packet_rate: float = 0.0
    last_packet_age: float = 0.0
    detail: str = ""
    error: str = ""
    stage: TelemetryStage = TelemetryStage.WAITING
    bytes_per_sec: float = 0.0
    #: Raw datagrams counted immediately after recvfrom(), before any
    #: interpretation - the ground truth for "is anything arriving at all".
    raw_packets: int = 0
    #: Datagrams whose header AND payload decoded successfully.
    packets_parsed: int = 0
    frames_emitted: int = 0
    packet_types: tuple[tuple[str, int], ...] = ()
    #: Where the packets came from, and where traffic was seen if the
    #: configured port is silent but another one is not.
    last_sender: str = ""
    detected_port: int = 0
    receive_buffer_kb: int = 0
    #: Live values straight from the last normalized frame. Deliberately
    #: independent of the haptic engine so the UI can prove telemetry is
    #: arriving even when the engine is stopped or output is muted.
    live_rpm: float = 0.0
    live_max_rpm: float = 0.0
    live_speed_kph: float = 0.0
    live_gear: int = 0
    live_throttle: float = 0.0
    live_brake: float = 0.0
    #: RAW values straight off the parsed packet, before any normalization.
    #: Kept separate so a mismatch between raw and normalized localises the
    #: fault to the adapter rather than the parser (or vice versa).
    raw_rpm: float = 0.0
    raw_speed_kph: float = 0.0
    raw_gear: int = 0
    raw_throttle: float = 0.0
    raw_brake: float = 0.0
    raw_max_rpm: float = 0.0
    player_car_index: int = 0
    #: Gear-change trace: (previous, current, "UPSHIFT"/"DOWNSHIFT", count)
    prev_gear: int = 0
    current_gear: int = 0
    last_shift: str = "none"
    shift_count: int = 0


class RateTracker:
    """Packets-per-second over a sliding window."""

    def __init__(self, window: float = 2.0) -> None:
        self._window = window
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def mark(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._stamps.append(now)
            cutoff = now - self._window
            while self._stamps and self._stamps[0] < cutoff:
                self._stamps.popleft()

    def rate(self) -> float:
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            while self._stamps and self._stamps[0] < cutoff:
                self._stamps.popleft()
            return len(self._stamps) / self._window

    def reset(self) -> None:
        with self._lock:
            self._stamps.clear()


class GameAdapter(ABC):
    """Base class for every supported game."""

    #: Stable id used in settings and the UI.
    game_id: str = "unknown"
    display_name: str = "Unknown Game"
    #: False for adapters that are architecture-only placeholders.
    supported: bool = False
    #: Shown on the Games page.
    description: str = ""

    def __init__(self) -> None:
        self._on_frame: FrameCallback | None = None

    def set_frame_callback(self, callback: FrameCallback | None) -> None:
        self._on_frame = callback

    def _emit(self, frame: TelemetryFrame) -> None:
        callback = self._on_frame
        if callback is not None:
            callback(frame)

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def status(self) -> AdapterStatus: ...

    def configure(self, **options) -> None:
        """Apply adapter-specific options (port, timeout, ...)."""

    def __enter__(self) -> "GameAdapter":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()


class UnsupportedAdapter(GameAdapter):
    """Placeholder for a game the architecture is ready for but which has
    no implementation yet. Deliberately produces no telemetry rather than
    faking any, so the UI can honestly report its state."""

    def __init__(self, game_id: str, display_name: str, description: str = "") -> None:
        super().__init__()
        self.game_id = game_id
        self.display_name = display_name
        self.description = description
        self.supported = False

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def status(self) -> AdapterStatus:
        return AdapterStatus(
            game_id=self.game_id,
            display_name=self.display_name,
            running=False,
            connected=False,
            detail="Architecture ready - telemetry not implemented",
        )
