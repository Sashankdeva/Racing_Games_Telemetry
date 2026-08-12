"""Binary F1 packets -> plain Python values.

Every function here is defensive by contract: malformed, truncated or
unknown packets return None instead of raising. A game sending an
unexpected layout must never be able to crash the telemetry thread or, far
worse, leave the motors running.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from app.core.models import SurfaceType, Wheels
from app.games.f1 import packets as p


@dataclass(slots=True)
class CarTelemetry:
    speed_kph: float = 0.0
    throttle: float = 0.0
    steer: float = 0.0
    brake: float = 0.0
    clutch: float = 0.0
    gear: int = 0
    engine_rpm: float = 0.0
    drs: bool = False
    rev_lights_percent: int = 0
    surfaces: tuple[SurfaceType, ...] = ()


@dataclass(slots=True)
class CarStatus:
    traction_control: int = 0
    anti_lock_brakes: bool = False
    pit_limiter: bool = False
    max_rpm: float = 0.0
    idle_rpm: float = 0.0
    max_gears: int = 0


@dataclass(slots=True)
class MotionData:
    g_lateral: float = 0.0
    g_longitudinal: float = 0.0
    g_vertical: float = 0.0


@dataclass(slots=True)
class MotionExtended:
    suspension_position: Wheels = field(default_factory=Wheels)
    suspension_velocity: Wheels = field(default_factory=Wheels)
    suspension_acceleration: Wheels = field(default_factory=Wheels)
    wheel_speed: Wheels = field(default_factory=Wheels)
    wheel_slip: Wheels = field(default_factory=Wheels)


@dataclass(slots=True)
class CarDamage:
    front_left_wing: int = 0
    front_right_wing: int = 0
    rear_wing: int = 0
    floor: int = 0

    @property
    def total(self) -> int:
        return self.front_left_wing + self.front_right_wing + self.rear_wing + self.floor


def _player_slice_ok(data: bytes, offset: int, size: int) -> bool:
    return offset >= 0 and len(data) >= offset + size


def plausible_telemetry(t: "CarTelemetry") -> bool:
    """Sanity-check decoded values.

    A wrong stride still decodes without raising - it just yields nonsense.
    Validating here means an unknown layout is detected and retried instead
    of quietly driving the haptics from garbage.
    """
    return (
        0.0 <= t.speed_kph <= 500.0
        and 0.0 <= t.engine_rpm <= 20000.0
        and -1 <= t.gear <= 9
        and -0.01 <= t.throttle <= 1.01
        and -0.01 <= t.brake <= 1.01
        and -1.01 <= t.steer <= 1.01
    )


def parse_car_telemetry(data: bytes, header: p.PacketHeader) -> CarTelemetry | None:
    payload = len(data) - header.size
    strides = [p.CAR_TELEMETRY_SIZE]
    derived = p.car_telemetry_stride(payload)
    if derived != p.CAR_TELEMETRY_SIZE:
        strides.append(derived)

    for stride in strides:
        result = _parse_car_telemetry_at(data, header, stride)
        if result is not None and plausible_telemetry(result):
            return result
    return None


def _parse_car_telemetry_at(
    data: bytes, header: p.PacketHeader, stride: int
) -> CarTelemetry | None:
    offset = header.size + header.player_car_index * stride
    if not _player_slice_ok(data, offset, p.CAR_TELEMETRY_SIZE):
        return None
    try:
        values = p.CAR_TELEMETRY.unpack_from(data, offset)
    except struct.error:
        return None

    # Field order mirrors the struct definition in packets.py.
    speed = values[0]
    throttle, steer, brake = values[1], values[2], values[3]
    clutch = values[4]
    gear = values[5]
    engine_rpm = values[6]
    drs = values[7]
    rev_lights = values[8]
    # values[10:14] brake temps, [14:18] tyre surface temps,
    # [18:22] tyre inner temps, [22] engine temp, [23:27] pressures,
    # [27:31] surface types
    surface_values = values[27:31]

    return CarTelemetry(
        speed_kph=float(speed),
        throttle=float(throttle),
        steer=float(steer),
        brake=float(brake),
        clutch=float(clutch) / 100.0,
        gear=int(gear),
        engine_rpm=float(engine_rpm),
        drs=bool(drs),
        rev_lights_percent=int(rev_lights),
        surfaces=p.to_surfaces(surface_values),
    )


def parse_car_status(data: bytes, header: p.PacketHeader) -> CarStatus | None:
    stride = p.car_status_stride(header.packet_format, len(data) - header.size)
    offset = header.size + header.player_car_index * stride
    if not _player_slice_ok(data, offset, p.CAR_STATUS_PREFIX_SIZE):
        return None
    try:
        (
            traction_control,
            anti_lock_brakes,
            _fuel_mix,
            _brake_bias,
            pit_limiter,
            _fuel_in_tank,
            _fuel_capacity,
            _fuel_laps,
            max_rpm,
            idle_rpm,
            max_gears,
        ) = p.CAR_STATUS_PREFIX.unpack_from(data, offset)
    except struct.error:
        return None

    return CarStatus(
        traction_control=int(traction_control),
        anti_lock_brakes=bool(anti_lock_brakes),
        pit_limiter=bool(pit_limiter),
        max_rpm=float(max_rpm),
        idle_rpm=float(idle_rpm),
        max_gears=int(max_gears),
    )


def parse_motion(data: bytes, header: p.PacketHeader) -> MotionData | None:
    offset = (
        header.size
        + header.player_car_index * p.CAR_MOTION_SIZE
        + p.G_FORCE_OFFSET
    )
    if not _player_slice_ok(data, offset, p.G_FORCES.size):
        return None
    try:
        lateral, longitudinal, vertical = p.G_FORCES.unpack_from(data, offset)
    except struct.error:
        return None
    return MotionData(
        g_lateral=float(lateral),
        g_longitudinal=float(longitudinal),
        g_vertical=float(vertical),
    )


def parse_motion_extended(data: bytes, header: p.PacketHeader) -> MotionExtended | None:
    """Suspension and wheel data.

    2023+ carries this in its own MotionEx packet; 2022 appends it after the
    22 CarMotionData entries. The five arrays we need lead the block in both
    cases, so one prefix parse serves both.
    """
    if header.is_legacy_layout:
        offset = header.size + p.MAX_CARS * p.CAR_MOTION_SIZE
    else:
        offset = header.size

    if not _player_slice_ok(data, offset, p.MOTION_EX_PREFIX_SIZE):
        return None
    try:
        values = p.MOTION_EX_PREFIX.unpack_from(data, offset)
    except struct.error:
        return None

    return MotionExtended(
        suspension_position=p.to_wheels(values[0:4]),
        suspension_velocity=p.to_wheels(values[4:8]),
        suspension_acceleration=p.to_wheels(values[8:12]),
        wheel_speed=p.to_wheels(values[12:16]),
        wheel_slip=p.to_wheels(values[16:20]),
    )


def parse_pit_status(data: bytes, header: p.PacketHeader) -> int | None:
    """0 = on track, 1 = pit lane, 2 = in pit area."""
    payload = len(data) - header.size
    stride = p.lap_data_stride(header.packet_format, payload)
    offset = header.size + header.player_car_index * stride + p.LAP_PIT_STATUS_OFFSET
    if not _player_slice_ok(data, offset, 1):
        return None
    return data[offset]


def parse_car_damage(data: bytes, header: p.PacketHeader) -> CarDamage | None:
    offset = (
        header.size
        + header.player_car_index * p.CAR_DAMAGE_SIZE_2023
        + p.CAR_DAMAGE_WING_OFFSET
    )
    if not _player_slice_ok(data, offset, p.CAR_DAMAGE_WINGS.size):
        return None
    try:
        fl, fr, rear, floor = p.CAR_DAMAGE_WINGS.unpack_from(data, offset)
    except struct.error:
        return None
    return CarDamage(int(fl), int(fr), int(rear), int(floor))


def parse_event_code(data: bytes, header: p.PacketHeader) -> str | None:
    offset = header.size
    if not _player_slice_ok(data, offset, p.EVENT_CODE_SIZE):
        return None
    try:
        return data[offset : offset + p.EVENT_CODE_SIZE].decode("ascii", errors="strict")
    except UnicodeDecodeError:
        return None
