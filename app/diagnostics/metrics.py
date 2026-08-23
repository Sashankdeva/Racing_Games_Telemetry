"""Collects one consistent view of telemetry health for the UI.

Pulls from the adapter and the telemetry state on demand rather than
having them push status around. The Diagnostics page polls this a few
times a second.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.telemetry_state import (
    TelemetrySnapshot,
    TelemetryState,
    TelemetryStatus,
)
from app.games.base import AdapterStatus, GameAdapter
from app.core.models import NO_TELEMETRY, TelemetryFrame


@dataclass(frozen=True, slots=True)
class DiagnosticsReport:
    adapter: AdapterStatus | None = None
    telemetry: TelemetrySnapshot | None = None

    @property
    def frame(self) -> TelemetryFrame:
        """The last valid frame, whether or not it is current.

        Stale is not empty: when the game pauses or drops packets the car
        is still on the same lap and the same tyres. Consumers pair this
        with `status` rather than being handed a blank frame.
        """
        return self.telemetry.frame if self.telemetry else NO_TELEMETRY

    @property
    def status(self) -> TelemetryStatus:
        return self.telemetry.status if self.telemetry else TelemetryStatus.NO_DATA

    @property
    def live(self) -> bool:
        return bool(self.telemetry and self.telemetry.live)

    @property
    def stale(self) -> bool:
        return bool(self.telemetry and self.telemetry.stale)

    @property
    def has_data(self) -> bool:
        """True once anything valid has arrived this session."""
        return bool(self.telemetry and self.telemetry.has_data)

    @property
    def age(self) -> float:
        return self.telemetry.age if self.telemetry else 0.0


class DiagnosticsCollector:
    def __init__(
        self, telemetry: TelemetryState, adapter: GameAdapter | None = None
    ) -> None:
        self.telemetry = telemetry
        self.adapter = adapter

    def set_adapter(self, adapter: GameAdapter | None) -> None:
        self.adapter = adapter

    def collect(self) -> DiagnosticsReport:
        return DiagnosticsReport(
            adapter=self.adapter.status() if self.adapter else None,
            telemetry=self.telemetry.snapshot(),
        )
