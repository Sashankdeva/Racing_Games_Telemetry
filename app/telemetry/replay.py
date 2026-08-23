"""Replay recorded packets through the live pipeline.

The critical property: replay hands raw bytes to the *same* adapter method
the UDP listener calls. There is no parallel parsing path, so anything
replay proves also holds live, and a bug found in replay is the real bug.

Timing is reproduced from the recorded offsets so packet rates and
staleness behave as they did in the session. Speed can be scaled for
faster-than-real debugging, and stepping one packet at a time makes a
single frame's decode inspectable.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from app.core.logging import get_logger
from app.telemetry.recording import RecordingMeta, read_meta, read_packets

_log = get_logger(__name__)

PacketSink = Callable[[bytes], None]


class ReplayPlayer:
    """Plays a recording into a packet sink on a background thread."""

    def __init__(self, path: Path, sink: PacketSink) -> None:
        self.path = Path(path)
        self._sink = sink
        self.meta: RecordingMeta | None = read_meta(self.path)

        self._packets: list[tuple[float, bytes]] = []
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._paused = threading.Event()
        self._speed = 1.0
        self._loop = False
        self._index = 0
        self._played = 0

    # ------------------------------------------------------------------
    @property
    def loaded(self) -> bool:
        return bool(self._packets)

    @property
    def packet_count(self) -> int:
        return len(self._packets)

    @property
    def position(self) -> int:
        return self._index

    @property
    def played(self) -> int:
        return self._played

    @property
    def running(self) -> bool:
        return self._running.is_set()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    @property
    def duration(self) -> float:
        return self._packets[-1][0] if self._packets else 0.0

    def set_speed(self, speed: float) -> None:
        self._speed = max(0.1, min(20.0, speed))

    def set_loop(self, loop: bool) -> None:
        self._loop = loop

    # ------------------------------------------------------------------
    def load(self) -> bool:
        """Read the whole recording into memory.

        Recordings are short by design (30-60 s, a few MB), so holding them
        in memory keeps replay timing exact and seeking trivial.
        """
        self._packets = list(read_packets(self.path))
        self._index = 0
        if not self._packets:
            _log.error("Recording %s contains no packets", self.path.name)
            return False
        _log.info(
            "Loaded %d packets (%.1fs) from %s",
            len(self._packets), self.duration, self.path.name,
        )
        return True

    def start(self) -> bool:
        if self.running:
            return True
        if not self._packets and not self.load():
            return False

        self._running.set()
        self._paused.clear()
        self._thread = threading.Thread(target=self._run, name="replay", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running.clear()
        self._paused.clear()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def rewind(self) -> None:
        self._index = 0

    def step(self, count: int = 1) -> int:
        """Emit the next `count` packets synchronously.

        Deterministic single-stepping: no thread, no timing, so a specific
        packet's decode can be inspected in isolation.
        """
        sent = 0
        for _ in range(max(1, count)):
            if self._index >= len(self._packets):
                break
            _, data = self._packets[self._index]
            self._index += 1
            self._played += 1
            self._emit(data)
            sent += 1
        return sent

    # ------------------------------------------------------------------
    def _emit(self, data: bytes) -> None:
        try:
            self._sink(data)
        except Exception:  # noqa: BLE001 - a bad packet must not kill replay
            _log.exception("Replay sink failed on packet %d", self._index)

    def _run(self) -> None:
        while self._running.is_set():
            if self._index >= len(self._packets):
                if not self._loop:
                    break
                self._index = 0

            started = time.perf_counter()
            base_offset = self._packets[self._index][0]

            while self._running.is_set() and self._index < len(self._packets):
                if self._paused.is_set():
                    time.sleep(0.05)
                    # Re-anchor so a pause does not cause a burst on resume.
                    started = time.perf_counter()
                    base_offset = self._packets[self._index][0]
                    continue

                offset, data = self._packets[self._index]
                target = (offset - base_offset) / self._speed
                wait = target - (time.perf_counter() - started)
                if wait > 0:
                    time.sleep(min(wait, 0.25))
                    continue

                self._index += 1
                self._played += 1
                self._emit(data)

        self._running.clear()
        _log.info("Replay finished (%d packets played)", self._played)
