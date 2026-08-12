"""Thin ctypes wrapper around the Windows XInput API.

Direct XInput only - no vJoy/ViGEm/pyxinput. This is the transport that was
physically verified against the Cosmic Byte Blitz on index 0.

Importing this module never raises: on a non-Windows machine or a system
without XInput, `available()` returns False and every call degrades to a
safe no-op so tests and the UI still run.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass

ERROR_SUCCESS = 0
ERROR_DEVICE_NOT_CONNECTED = 1167

MAX_DEVICES = 4
MOTOR_SPEED_MAX = 65535

# Preference order: 1_4 ships with Win8+, 1_3 with the DirectX redist,
# 9_1_0 is the legacy in-box fallback.
_CANDIDATE_DLLS = ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll")

_xinput = None
_dll_name = ""
_load_error = ""


def _init() -> None:
    global _xinput, _dll_name, _load_error

    if not hasattr(ctypes, "WinDLL"):  # non-Windows
        _load_error = "XInput is only available on Windows"
        return

    for name in _CANDIDATE_DLLS:
        try:
            lib = ctypes.WinDLL(name)
        except OSError:
            continue

        try:
            lib.XInputGetState.argtypes = [ctypes.c_uint, ctypes.POINTER(XInputState)]
            lib.XInputGetState.restype = ctypes.c_uint
            lib.XInputSetState.argtypes = [ctypes.c_uint, ctypes.POINTER(XInputVibration)]
            lib.XInputSetState.restype = ctypes.c_uint
        except AttributeError:
            continue

        _xinput = lib
        _dll_name = name
        return

    _load_error = f"None of {', '.join(_CANDIDATE_DLLS)} could be loaded"


class XInputGamepad(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class XInputState(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint), ("Gamepad", XInputGamepad)]


class XInputVibration(ctypes.Structure):
    _fields_ = [("wLeftMotorSpeed", ctypes.c_ushort), ("wRightMotorSpeed", ctypes.c_ushort)]


_init()


@dataclass(frozen=True, slots=True)
class PadState:
    packet_number: int
    buttons: int
    left_trigger: int
    right_trigger: int
    thumb_lx: int
    thumb_ly: int
    thumb_rx: int
    thumb_ry: int


def available() -> bool:
    """True if an XInput DLL was loaded successfully."""
    return _xinput is not None


def dll_name() -> str:
    return _dll_name


def load_error() -> str:
    return _load_error


def is_connected(index: int) -> bool:
    if _xinput is None:
        return False
    state = XInputState()
    return _xinput.XInputGetState(index, ctypes.byref(state)) == ERROR_SUCCESS


def connected_indices() -> list[int]:
    return [i for i in range(MAX_DEVICES) if is_connected(i)]


def get_state(index: int) -> PadState | None:
    if _xinput is None:
        return None
    state = XInputState()
    if _xinput.XInputGetState(index, ctypes.byref(state)) != ERROR_SUCCESS:
        return None
    pad = state.Gamepad
    return PadState(
        packet_number=state.dwPacketNumber,
        buttons=pad.wButtons,
        left_trigger=pad.bLeftTrigger,
        right_trigger=pad.bRightTrigger,
        thumb_lx=pad.sThumbLX,
        thumb_ly=pad.sThumbLY,
        thumb_rx=pad.sThumbRX,
        thumb_ry=pad.sThumbRY,
    )


def set_vibration(index: int, left_speed: int, right_speed: int) -> int:
    """Set raw 16-bit motor speeds. Returns the XInput result code
    (0 = ERROR_SUCCESS); ERROR_DEVICE_NOT_CONNECTED when unplugged."""
    if _xinput is None:
        return ERROR_DEVICE_NOT_CONNECTED
    left = max(0, min(MOTOR_SPEED_MAX, int(left_speed)))
    right = max(0, min(MOTOR_SPEED_MAX, int(right_speed)))
    vibration = XInputVibration(wLeftMotorSpeed=left, wRightMotorSpeed=right)
    return _xinput.XInputSetState(index, ctypes.byref(vibration))
