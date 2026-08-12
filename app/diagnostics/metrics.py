"""Collects a single consistent view of system health for the UI.

Pulls from the controller, engine and adapter on demand rather than having
those components push status around. The Diagnostics page polls this a few
times a second; nothing here runs in the haptic hot path.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.controller import xinput
from app.controller.blitz import XInputController
from app.games.base import AdapterStatus, GameAdapter
from app.haptics.engine import EngineSnapshot, HapticEngine


@dataclass(frozen=True, slots=True)
class DiagnosticsReport:
    # controller
    xinput_available: bool = False
    xinput_dll: str = ""
    xinput_error: str = ""
    controller_connected: bool = False
    controller_index: int = 0
    controller_name: str = ""
    connected_indices: tuple[int, ...] = ()
    rumble_writes_ok: int = 0
    rumble_writes_failed: int = 0
    last_result_code: int = 0

    # telemetry
    adapter: AdapterStatus | None = None

    # haptics
    engine: EngineSnapshot = field(default_factory=EngineSnapshot)
    target_tick_rate: float = 0.0
    scheduled_cues: int = 0

    @property
    def rumble_ok(self) -> bool:
        """True once at least one write has succeeded and none are failing."""
        return self.rumble_writes_ok > 0 and self.last_result_code == 0

    @property
    def tick_rate_healthy(self) -> bool:
        if not self.engine.running or self.target_tick_rate <= 0:
            return False
        return self.engine.tick_rate >= self.target_tick_rate * 0.8


class DiagnosticsCollector:
    def __init__(
        self,
        controller: XInputController,
        engine: HapticEngine,
        adapter: GameAdapter | None = None,
    ) -> None:
        self.controller = controller
        self.engine = engine
        self.adapter = adapter

    def set_adapter(self, adapter: GameAdapter | None) -> None:
        self.adapter = adapter

    def collect(self) -> DiagnosticsReport:
        info = self.controller.info()
        writes_ok, writes_failed = self.controller.write_stats

        return DiagnosticsReport(
            xinput_available=xinput.available(),
            xinput_dll=xinput.dll_name(),
            xinput_error=xinput.load_error(),
            controller_connected=info.connected,
            controller_index=info.index,
            controller_name=info.name,
            connected_indices=tuple(xinput.connected_indices()),
            rumble_writes_ok=writes_ok,
            rumble_writes_failed=writes_failed,
            last_result_code=self.controller.last_result_code,
            adapter=self.adapter.status() if self.adapter else None,
            engine=self.engine.snapshot(),
            target_tick_rate=self.engine.tick_rate,
            scheduled_cues=self.engine.scheduler.active_count,
        )
