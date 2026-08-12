"""Shared fixtures.

Every test runs against an isolated data directory so a test can never
read or overwrite the real user's profiles and settings.
"""

from __future__ import annotations

import struct

import pytest

from app.core.models import TelemetryFrame


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path / "data"))
    yield tmp_path / "data"


@pytest.fixture
def frame():
    """A plausible mid-corner telemetry frame."""

    def build(**overrides) -> TelemetryFrame:
        defaults = dict(
            valid=True,
            game="test",
            speed_kph=200.0,
            rpm=9000.0,
            max_rpm=12000.0,
            idle_rpm=4000.0,
            gear=5,
            throttle=1.0,
            brake=0.0,
        )
        defaults.update(overrides)
        return TelemetryFrame(**defaults)

    return build


# --------------------------------------------------------------------------
# F1 packet builders - byte-exact to the published spec so the parser is
# tested against real layouts rather than against its own assumptions.
# --------------------------------------------------------------------------
def f1_header(packet_id: int, player_index: int = 0, packet_format: int = 2023) -> bytes:
    if packet_format <= 2022:
        return struct.pack(
            "<HBBBBQfIBB", packet_format, 1, 0, 1, packet_id, 999, 1.5, 100, player_index, 255
        )
    return struct.pack(
        "<HBBBBBQfIIBB",
        packet_format, 23, 1, 0, 1, packet_id, 999, 1.5, 100, 100, player_index, 255,
    )


def f1_car_telemetry_entry(
    speed: int = 287,
    throttle: float = 1.0,
    steer: float = 0.0,
    brake: float = 0.0,
    gear: int = 7,
    rpm: int = 11800,
    rev_lights: int = 90,
    surfaces: tuple[int, int, int, int] = (0, 0, 0, 0),
) -> bytes:
    """One CarTelemetryData entry (60 bytes). `surfaces` is F1 order RL,RR,FL,FR."""
    return struct.pack(
        "<HfffBbHBBH4H4B4BH4f4B",
        speed, throttle, steer, brake, 0, gear, rpm, 1, rev_lights, 0,
        *[500] * 4, *[90] * 4, *[95] * 4, 110,
        *[23.5] * 4,
        *surfaces,
    )


def f1_car_status_entry(max_rpm: int = 12000, idle_rpm: int = 4000, abs_on: int = 1) -> bytes:
    """One CarStatusData entry (55 bytes, F1 23 layout)."""
    return struct.pack(
        "<BBBBBfffHHBBHBBBbfffBfffB",
        0, abs_on, 1, 50, 0,
        90.0, 110.0, 20.0,
        max_rpm, idle_rpm,
        8, 1, 0, 7, 7, 3, 0,
        500.0, 120.0, 4.0, 2,
        1.0, 2.0, 3.0, 0,
    )


def f1_motion_entry(g_lat: float = 0.0, g_lon: float = 0.0, g_vert: float = 0.0) -> bytes:
    """One CarMotionData entry (60 bytes)."""
    return struct.pack(
        "<6f6h6f",
        1.0, 2.0, 3.0, 10.0, 0.0, 5.0,
        0, 0, 0, 0, 0, 0,
        g_lat, g_lon, g_vert, 0.1, 0.0, 0.0,
    )


def f1_motion_ex(
    suspension_position=(0.1, 0.1, 0.1, 0.1),
    suspension_velocity=(2.0, 2.0, 2.0, 2.0),
    suspension_acceleration=(15.0, 15.0, 15.0, 15.0),
    wheel_speed=(70.0, 70.0, 70.0, 70.0),
    wheel_slip=(0.0, 0.0, 0.0, 0.0),
) -> bytes:
    """MotionEx prefix (80 bytes). All arrays are F1 order RL,RR,FL,FR."""
    return struct.pack(
        "<4f4f4f4f4f",
        *suspension_position,
        *suspension_velocity,
        *suspension_acceleration,
        *wheel_speed,
        *wheel_slip,
    )
