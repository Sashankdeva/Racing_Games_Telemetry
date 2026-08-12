"""XInput-backed controller, targeting the Cosmic Byte Blitz Dual Mode.

The Blitz enumerates as a generic "Controller (XBOX 360 For Windows)" over
its 2.4 GHz dongle, so this class works for any XInput pad - the Blitz is
simply the device it was tuned and verified against.
"""

from __future__ import annotations

import threading

from app.controller import xinput
from app.controller.base import ControllerBackend, DeviceInfo
from app.core.logging import RateLimitedLogger, get_logger

_log = get_logger(__name__)
_rate_log = RateLimitedLogger(_log)

DEVICE_NAME = "Controller (XBOX 360 For Windows)"

# Don't re-send a value that rounds to the same 16-bit speed - saves a USB
# round trip per tick when output is steady. Roughly 1/2000 of full scale.
_WRITE_EPSILON = 1.0 / 2000.0


def _clamp01(value: float) -> float:
    if value != value:  # NaN guard - never let NaN reach the motors
        return 0.0
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


class XInputController(ControllerBackend):
    def __init__(self, index: int = 0, output_limit: float = 1.0) -> None:
        self.index = index
        self.output_limit = _clamp01(output_limit)
        self._lock = threading.Lock()
        self._last_left = 0.0
        self._last_right = 0.0
        self._connected = False
        self._write_ok = 0
        self._write_fail = 0
        self._last_result = xinput.ERROR_SUCCESS

    # --- state ------------------------------------------------------------
    def is_connected(self) -> bool:
        self._connected = xinput.is_connected(self.index)
        return self._connected

    def info(self) -> DeviceInfo:
        connected = self.is_connected()
        return DeviceInfo(
            name=DEVICE_NAME if connected else "No controller detected",
            index=self.index,
            connection="XInput / 2.4 GHz" if connected else "None",
            connected=connected,
        )

    def set_index(self, index: int) -> None:
        """Retarget another XInput slot, silencing the old one first."""
        with self._lock:
            if index == self.index:
                return
            xinput.set_vibration(self.index, 0, 0)
            self.index = index
            self._last_left = -1.0  # force the next write through
            self._last_right = -1.0

    def set_output_limit(self, limit: float) -> None:
        self.output_limit = _clamp01(limit)

    # --- output -----------------------------------------------------------
    def set_motors(self, left: float, right: float) -> bool:
        """Drive the motors with 0..1 intensities, after the safety limit."""
        left = _clamp01(left) * self.output_limit
        right = _clamp01(right) * self.output_limit

        with self._lock:
            unchanged = (
                abs(left - self._last_left) < _WRITE_EPSILON
                and abs(right - self._last_right) < _WRITE_EPSILON
            )
            if unchanged:
                return self._last_result == xinput.ERROR_SUCCESS

            result = xinput.set_vibration(
                self.index,
                round(left * xinput.MOTOR_SPEED_MAX),
                round(right * xinput.MOTOR_SPEED_MAX),
            )
            self._last_result = result
            self._last_left = left
            self._last_right = right

            if result == xinput.ERROR_SUCCESS:
                self._write_ok += 1
                return True

            self._write_fail += 1
            if result == xinput.ERROR_DEVICE_NOT_CONNECTED:
                self._connected = False
                _rate_log.warning(
                    "disconnected", "Controller %d not connected during write", self.index
                )
            else:
                _rate_log.error(
                    "write_fail", "XInputSetState failed on %d (code %s)", self.index, result
                )
            return False

    def stop(self) -> bool:
        """Silence both motors, bypassing the change-detection shortcut."""
        with self._lock:
            result = xinput.set_vibration(self.index, 0, 0)
            self._last_left = 0.0
            self._last_right = 0.0
            self._last_result = result
            return result == xinput.ERROR_SUCCESS

    # --- diagnostics ------------------------------------------------------
    @property
    def last_intensities(self) -> tuple[float, float]:
        return self._last_left, self._last_right

    @property
    def write_stats(self) -> tuple[int, int]:
        """(successful writes, failed writes)"""
        return self._write_ok, self._write_fail

    @property
    def last_result_code(self) -> int:
        return self._last_result

    def reset_stats(self) -> None:
        self._write_ok = 0
        self._write_fail = 0
