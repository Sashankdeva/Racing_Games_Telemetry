"""Controller backend interface.

The haptic engine writes through this interface only, so a different
rumble transport (or a null/simulated device for tests) can be swapped in
without touching the engine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    name: str
    index: int
    connection: str
    connected: bool

    @property
    def summary(self) -> str:
        state = "Connected" if self.connected else "Disconnected"
        return f"{self.name} ({self.connection}) - {state}"


class ControllerBackend(ABC):
    """A device that accepts two independent 0..1 rumble intensities."""

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def set_motors(self, left: float, right: float) -> bool:
        """Drive both motors. Returns False if the write failed."""

    @abstractmethod
    def stop(self) -> bool:
        """Force both motors to zero immediately."""

    @abstractmethod
    def info(self) -> DeviceInfo: ...

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()


class NullController(ControllerBackend):
    """No-op backend used for tests and for running the UI with no hardware."""

    def __init__(self, index: int = 0) -> None:
        self.index = index
        self.last_left = 0.0
        self.last_right = 0.0
        self.write_count = 0

    def is_connected(self) -> bool:
        return False

    def set_motors(self, left: float, right: float) -> bool:
        self.last_left = left
        self.last_right = right
        self.write_count += 1
        return True

    def stop(self) -> bool:
        return self.set_motors(0.0, 0.0)

    def info(self) -> DeviceInfo:
        return DeviceInfo("No controller", self.index, "None", False)
