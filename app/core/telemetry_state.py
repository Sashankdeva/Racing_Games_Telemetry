"""Live telemetry state - the single source of truth for the application.

The telemetry thread writes here; the UI and every consumer (lap analysis,
tyre model, and later the coach and strategy engine) read from here.
Latest-frame-wins: there is no queue, because a consumer that falls behind
wants the *current* state of the car, not a backlog of stale ones.

Three states, not two
---------------------

The distinction that matters is between "no packet arrived recently" and
"there is no data". Those are completely different situations and
collapsing them loses real information:

    NO_DATA   nothing valid has ever been received this session
    LIVE      packets are arriving now
    STALE     packets arrived before, but not within the timeout

This module previously replaced the stored frame with NO_TELEMETRY once it
aged past the timeout, which meant a paused game or a few dropped packets
erased the lap number, position, tyre compound and everything else the
driver was looking at. A missing packet is not missing data: the car is
still on lap 18 on the same set of tyres.

So the last valid frame is retained indefinitely and only ever replaced by
a newer valid frame, or cleared explicitly when the session genuinely ends
(`clear()`). Staleness is reported *alongside* the data, as status and age,
so a consumer can show the values while making it obvious they are not
current. Presenting stale data as live remains forbidden; discarding it
was never the way to prevent that.

Session history - completed laps, bests, stints - lives elsewhere
(`DriverSession`, `LapAnalysis`, stints) and is built from frames as they
arrive. It never consults the current frame, so it is unaffected by
telemetry stopping.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum

from app.core.models import NO_TELEMETRY, TelemetryFrame

#: Seconds after which a frame is considered stale.
DEFAULT_TIMEOUT = 1.0


class TelemetryStatus(str, Enum):
    """Whether the data is current, old, or absent entirely."""

    NO_DATA = "NO DATA"
    STALE = "STALE"
    LIVE = "LIVE"


@dataclass(frozen=True, slots=True)
class TelemetrySnapshot:
    """Immutable view for consumers. Polled, never pushed.

    `frame` is the last valid frame regardless of status, so a consumer
    always has something real to show. `status` says how much to trust it.
    """

    frame: TelemetryFrame
    status: TelemetryStatus
    #: Seconds since the frame was captured. 0.0 when there has never been one.
    age: float
    frames_received: int

    @property
    def live(self) -> bool:
        return self.status is TelemetryStatus.LIVE

    @property
    def stale(self) -> bool:
        return self.status is TelemetryStatus.STALE

    @property
    def has_data(self) -> bool:
        """True once anything valid has been received this session."""
        return self.status is not TelemetryStatus.NO_DATA

    @property
    def valid(self) -> bool:
        return self.frame.valid


class TelemetryState:
    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._timeout = max(0.05, timeout)
        #: The last frame that was valid. Never overwritten by staleness -
        #: only by a newer valid frame, or by an explicit clear().
        self._frame: TelemetryFrame = NO_TELEMETRY
        self._count = 0
        self._last_packet_time: float = 0.0
        self._lock = threading.Lock()

    @property
    def timeout(self) -> float:
        return self._timeout

    def set_timeout(self, seconds: float) -> None:
        self._timeout = max(0.05, seconds)

    def submit(self, frame: TelemetryFrame) -> None:
        """Called from the telemetry thread.

        Only valid frames are retained. An invalid frame carries no
        information and must not be allowed to displace a good one.
        """
        if not frame.valid:
            return
        with self._lock:
            self._frame = frame
            self._count += 1
            self._last_packet_time = time.perf_counter()

    def clear(self) -> None:
        """Discard everything and return to NO_DATA.

        This is the explicit end-of-session signal - stopping telemetry,
        switching game mode, loading a replay. Staleness must never call it.
        """
        with self._lock:
            self._frame = NO_TELEMETRY
            self._last_packet_time = 0.0

    def snapshot(self) -> TelemetrySnapshot:
        with self._lock:
            frame = self._frame
            count = self._count
            last_packet = self._last_packet_time

        if not frame.valid:
            return TelemetrySnapshot(
                NO_TELEMETRY, TelemetryStatus.NO_DATA, 0.0, count
            )

        age = frame.age()
        status = (
            TelemetryStatus.LIVE if age <= self._timeout else TelemetryStatus.STALE
        )
        # The frame is returned either way. Consumers must render it under
        # the reported status rather than being handed a blank.
        del last_packet
        return TelemetrySnapshot(frame, status, age, count)

    @property
    def status(self) -> TelemetryStatus:
        return self.snapshot().status

    @property
    def seconds_since_last_packet(self) -> float:
        """Age of the newest frame, or 0.0 when none has arrived."""
        with self._lock:
            last_packet = self._last_packet_time
        if not last_packet:
            return 0.0
        return time.perf_counter() - last_packet

    @property
    def frames_received(self) -> int:
        with self._lock:
            return self._count
