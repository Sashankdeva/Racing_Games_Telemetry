"""Watches the controller for hot-plug transitions.

Polls at a low rate on its own thread (XInputGetState on an empty slot is
comparatively expensive, so this must never run in the haptic loop) and
raises events on edges. The engine subscribes to DISCONNECTED to cut all
vibration immediately.
"""

from __future__ import annotations

import threading
import time

from app.controller.blitz import XInputController
from app.core.events import Event, EventBus
from app.core.logging import get_logger

_log = get_logger(__name__)

POLL_INTERVAL = 1.0
#: Slots are only rescanned this often while nothing is connected.
SCAN_INTERVAL = 2.0


class DeviceManager:
    def __init__(
        self,
        controller: XInputController,
        bus: EventBus,
        auto_detect: bool = True,
    ) -> None:
        self.controller = controller
        self.bus = bus
        self.auto_detect = auto_detect

        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._connected = False
        self._last_scan = 0.0

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running.set()
        self._thread = threading.Thread(
            target=self._loop, name="device-manager", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def check_now(self) -> bool:
        """Poll once on the calling thread and emit any edge event."""
        connected = self.controller.is_connected()
        if connected != self._connected:
            self._connected = connected
            if connected:
                info = self.controller.info()
                _log.info("Controller connected on index %d", info.index)
                self.bus.emit(Event.CONTROLLER_CONNECTED, index=info.index, name=info.name)
            else:
                _log.warning("Controller disconnected from index %d", self.controller.index)
                self.bus.emit(Event.CONTROLLER_DISCONNECTED, index=self.controller.index)
        return connected

    def _loop(self) -> None:
        while self._running.is_set():
            try:
                if not self.check_now() and self.auto_detect:
                    self._try_find_controller()
            except Exception:  # noqa: BLE001 - the watchdog must never die
                _log.exception("Device manager poll failed")
            time.sleep(POLL_INTERVAL)

    def _try_find_controller(self) -> None:
        """Adopt another slot if the configured one is empty."""
        now = time.monotonic()
        if now - self._last_scan < SCAN_INTERVAL:
            return
        self._last_scan = now

        from app.controller import xinput

        for index in xinput.connected_indices():
            if index != self.controller.index:
                _log.info(
                    "Controller found on index %d (was watching %d); switching",
                    index,
                    self.controller.index,
                )
                self.controller.set_index(index)
                self.check_now()
                return
