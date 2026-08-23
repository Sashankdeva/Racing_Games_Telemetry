"""Binary F1 packets -> plain Python values.

Every function here is defensive by contract: malformed, truncated or
unknown packets return None instead of raising. A game sending an
unexpected layout must never be able to crash the telemetry thread, and a
mis-strided decode must be rejected rather than shown as real data.
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
    # These were already being unpacked and thrown away.
    brake_temp: Wheels = field(default_factory=Wheels)
    tyre_surface_temp: Wheels = field(default_factory=Wheels)
    tyre_inner_temp: Wheels = field(default_factory=Wheels)
    tyre_pressure: Wheels = field(default_factory=Wheels)
    engine_temp: float = 0.0


@dataclass(slots=True)
class CarStatus:
    traction_control: int = 0
    anti_lock_brakes: bool = False
    pit_limiter: bool = False
    max_rpm: float = 0.0
    idle_rpm: float = 0.0
    max_gears: int = 0
    fuel_in_tank: float = 0.0
    fuel_capacity: float = 0.0
    fuel_remaining_laps: float = 0.0
    tyre_compound: str = ""
    tyre_age_laps: int = -1
    ers_store_joules: float = 0.0
    ers_mode: str = ""
    ers_harvested_lap: float = 0.0
    ers_deployed_lap: float = 0.0


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


#: (packet id, payload size) -> the stride that produced plausible values.
#: Solving runs once per packet shape; every later packet uses the cached
#: answer, so this costs nothing at 60 Hz.
_stride_cache: dict[tuple[int, int], int] = {}

#: How many cars this session's packets carry. Learned only from packet
#: types whose contents are strongly self-validating (car telemetry, lap
#: data, car status), then applied to the weakly-validating ones.
#:
#: Motion and damage need this. Their fields are legitimately zero when the
#: car is stationary and undamaged, so "looks plausible" cannot distinguish
#: the right stride from one that landed in padding. The car count, however,
#: is the same for every packet in a session - so it is established where
#: the evidence is strong and reused where it is not.
_learned_car_count: int | None = None


def reset_stride_cache() -> None:
    """Forget learned strides - used when the game or mode changes."""
    global _learned_car_count
    _stride_cache.clear()
    _learned_car_count = None


def _solve_stride(
    data: bytes,
    header: p.PacketHeader,
    known_stride: int,
    min_stride: int,
    max_stride: int,
    attempt,
    learns_count: bool = False,
):
    """Find the per-car stride that decodes to plausible values.

    `attempt(offset)` parses at a candidate offset and returns a value, or
    None if it does not validate. The first stride that validates wins and
    is remembered for this packet shape.
    """
    global _learned_car_count

    payload = len(data) - header.size
    key = (header.packet_id, payload)

    cached = _stride_cache.get(key)
    if cached is not None:
        result = attempt(header.size + header.player_car_index * cached)
        if result is not None:
            return result
        # The layout changed under us (mode switch, different session).
        _stride_cache.pop(key, None)

    candidates = p.candidate_strides(payload, known_stride, min_stride, max_stride)
    if _learned_car_count is not None:
        # Try layouts consistent with the rest of this session first.
        candidates.sort(key=lambda pair: pair[1] != _learned_car_count)

    for stride, count in candidates:
        # The player's own index must exist within the array.
        if header.player_car_index >= count:
            continue
        result = attempt(header.size + header.player_car_index * stride)
        if result is not None:
            _stride_cache[key] = stride
            if learns_count:
                _learned_car_count = count
            return result
    return None


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


def plausible_tyre_tail(
    brake_temp: Wheels, surface_temp: Wheels, pressure: Wheels
) -> bool:
    """Sanity-check the thermal tail of CarTelemetry.

    Separate from the core check because the tail's layout is only verified
    up to 2025. If a newer title moved these fields, the honest answer is
    "no tyre data" rather than plausible-looking bytes read at the wrong
    offset.
    """
    return (
        all(0.0 <= v <= 2000.0 for v in brake_temp.as_tuple())
        and all(0.0 <= v <= 250.0 for v in surface_temp.as_tuple())
        and all(5.0 <= v <= 45.0 for v in pressure.as_tuple())
    )


def parse_car_telemetry(data: bytes, header: p.PacketHeader) -> CarTelemetry | None:
    """Decode the player's car telemetry.

    The stride is solved from the packet rather than assumed, because the
    per-car array is 22 entries up to F1 25 and 24 in F1 26.
    """
    return _solve_stride(
        data,
        header,
        known_stride=p.CAR_TELEMETRY_SIZE,
        min_stride=p.CAR_TELEMETRY_CORE_SIZE,
        max_stride=120,
        attempt=lambda offset: _parse_car_telemetry_at(data, offset),
        learns_count=True,
    )


def _parse_car_telemetry_at(data: bytes, offset: int) -> CarTelemetry | None:
    if not _player_slice_ok(data, offset, p.CAR_TELEMETRY_CORE_SIZE):
        return None
    try:
        core = p.CAR_TELEMETRY_CORE.unpack_from(data, offset)
    except struct.error:
        return None

    speed, throttle, steer, brake, clutch, gear, engine_rpm, drs, rev_lights, _bits = core

    telemetry = CarTelemetry(
        speed_kph=float(speed),
        throttle=float(throttle),
        steer=float(steer),
        brake=float(brake),
        clutch=float(clutch) / 100.0,
        gear=int(gear),
        engine_rpm=float(engine_rpm),
        drs=bool(drs),
        rev_lights_percent=int(rev_lights),
    )
    if not plausible_telemetry(telemetry):
        return None

    _apply_tyre_tail(data, offset, telemetry)
    return telemetry


#: Where the thermal block starts within a car-telemetry entry, relative to
#: the entry. 2022-2025 put it right after the 22-byte core. F1 26's entry
#: is one byte shorter, so if the byte that disappeared sits anywhere before
#: this block the whole thing shifts - and a single misread byte blanks
#: every tyre temperature and pressure at once.
TAIL_OFFSET_CANDIDATES = (0, -1, 1, -2, 2)

#: (packet id, payload size) -> the tail offset that decoded plausibly.
_tail_offset_cache: dict[tuple[int, int], int] = {}


def _apply_tyre_tail(data: bytes, offset: int, telemetry: "CarTelemetry") -> None:
    """Decode the thermal block, or leave it empty.

    The offset is solved from the data rather than assumed, for the same
    reason the stride is: the layout moved between titles and guessing
    where produces plausible-looking nonsense. A candidate is accepted only
    when brake temperatures, tyre temperatures AND pressures are all
    physically sensible at once - three independent range checks passing
    together at a wrong offset is not realistic.

    If none validates, the tyre fields stay empty and the UI reports them
    UNAVAILABLE. That is the honest answer; inventing temperatures is not.
    """
    base = offset + p.CAR_TELEMETRY_CORE_SIZE
    for delta in TAIL_OFFSET_CANDIDATES:
        at = base + delta
        if not _player_slice_ok(data, at, p.CAR_TELEMETRY_TAIL_SIZE) or at < 0:
            continue
        try:
            tail = p.CAR_TELEMETRY_TAIL.unpack_from(data, at)
        except struct.error:
            continue

        brake_temp = p.to_wheels(tail[0:4])
        surface_temp = p.to_wheels(tail[4:8])
        inner_temp = p.to_wheels(tail[8:12])
        pressure = p.to_wheels(tail[13:17])
        if not plausible_tyre_tail(brake_temp, surface_temp, pressure):
            continue

        telemetry.brake_temp = brake_temp
        telemetry.tyre_surface_temp = surface_temp
        telemetry.tyre_inner_temp = inner_temp
        telemetry.engine_temp = float(tail[12])
        telemetry.tyre_pressure = pressure
        telemetry.surfaces = p.to_surfaces(tail[17:21])
        return


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


def plausible_motion(motion: "MotionData") -> bool:
    """An F1 car pulls maybe 6 g. Anything beyond 20 is a bad decode.

    Without this a mis-strided motion packet yielded values like -8e6 and
    1e32, which then fed the contact detector.
    """
    return all(
        abs(v) <= 20.0
        for v in (motion.g_lateral, motion.g_longitudinal, motion.g_vertical)
    )


def parse_motion(data: bytes, header: p.PacketHeader) -> MotionData | None:
    def attempt(offset: int) -> MotionData | None:
        at = offset + p.G_FORCE_OFFSET
        if not _player_slice_ok(data, at, p.G_FORCES.size):
            return None
        try:
            lateral, longitudinal, vertical = p.G_FORCES.unpack_from(data, at)
        except struct.error:
            return None
        motion = MotionData(
            g_lateral=float(lateral),
            g_longitudinal=float(longitudinal),
            g_vertical=float(vertical),
        )
        return motion if plausible_motion(motion) else None

    return _solve_stride(
        data,
        header,
        known_stride=p.CAR_MOTION_SIZE,
        min_stride=p.G_FORCE_OFFSET + p.G_FORCES.size,
        max_stride=120,
        attempt=attempt,
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


# --------------------------------------------------------------------------
# Phase 1 telemetry expansion
# --------------------------------------------------------------------------
@dataclass(slots=True)
class LapData:
    position: int = 0
    current_lap: int = 0
    last_lap_time_s: float = 0.0
    current_lap_time_s: float = 0.0
    sector1_time_s: float = 0.0
    sector2_time_s: float = 0.0
    sector: int = 0
    lap_distance_m: float = 0.0
    delta_to_car_ahead_s: float = 0.0
    delta_to_leader_s: float = 0.0
    pit_status: int = 0
    lap_invalid: bool = False
    penalties_s: int = 0


@dataclass(slots=True)
class SessionData:
    weather: str = ""
    track_temperature: float = 0.0
    air_temperature: float = 0.0
    total_laps: int = 0
    track_length_m: int = 0
    session_type: str = ""
    session_time_left_s: float = 0.0


@dataclass(slots=True)
class DamageData:
    tyre_wear: Wheels = field(default_factory=Wheels)
    front_left_wing: int = 0
    front_right_wing: int = 0
    rear_wing: int = 0
    floor: int = 0
    diffuser: int = 0
    sidepod: int = 0
    gearbox: int = 0
    engine: int = 0

    @property
    def total(self) -> int:
        return (
            self.front_left_wing + self.front_right_wing
            + self.rear_wing + self.floor
        )


def plausible_lap(lap: "LapData") -> bool:
    """Reject a mis-strided decode instead of showing nonsense."""
    return (
        0 <= lap.position <= 26  # F1 26 fields 24; leave headroom
        and 0 <= lap.current_lap <= 200
        and 0.0 <= lap.last_lap_time_s < 3600.0
        and 0 <= lap.sector <= 2
        and 0 <= lap.pit_status <= 2
        and 0.0 <= lap.lap_distance_m < 20000.0
    )


def parse_lap_data(data: bytes, header: p.PacketHeader) -> LapData | None:
    return _solve_stride(
        data,
        header,
        known_stride=p.lap_data_stride(header.packet_format, len(data) - header.size),
        min_stride=p.LAP_DATA_PREFIX.size,
        max_stride=120,
        attempt=lambda offset: _parse_lap_data_at(data, offset),
        learns_count=True,
    )


def _parse_lap_data_at(data: bytes, offset: int) -> LapData | None:
    if not _player_slice_ok(data, offset, p.LAP_DATA_PREFIX.size):
        return None
    try:
        (
            last_ms, current_ms,
            s1_ms, s1_min, s2_ms, s2_min,
            delta_front_ms, delta_leader_ms,
            lap_distance, _total_distance, _sc_delta,
            position, current_lap, pit_status, _num_stops,
            sector, invalid, penalties,
        ) = p.LAP_DATA_PREFIX.unpack_from(data, offset)
    except struct.error:
        return None

    lap = LapData(
        position=int(position),
        current_lap=int(current_lap),
        last_lap_time_s=last_ms / 1000.0,
        current_lap_time_s=current_ms / 1000.0,
        sector1_time_s=s1_min * 60.0 + s1_ms / 1000.0,
        sector2_time_s=s2_min * 60.0 + s2_ms / 1000.0,
        sector=int(sector),
        lap_distance_m=float(lap_distance),
        delta_to_car_ahead_s=delta_front_ms / 1000.0,
        delta_to_leader_s=delta_leader_ms / 1000.0,
        pit_status=int(pit_status),
        lap_invalid=bool(invalid),
        penalties_s=int(penalties),
    )
    return lap if plausible_lap(lap) else None


def parse_session(data: bytes, header: p.PacketHeader) -> SessionData | None:
    offset = header.size
    if not _player_slice_ok(data, offset, p.SESSION_PREFIX.size):
        return None
    try:
        (
            weather, track_temp, air_temp, total_laps, track_length,
            session_type, _track_id, _formula,
            time_left, _duration,
        ) = p.SESSION_PREFIX.unpack_from(data, offset)
    except struct.error:
        return None

    return SessionData(
        weather=p.WEATHER.get(int(weather), f"code {weather}"),
        track_temperature=float(track_temp),
        air_temperature=float(air_temp),
        total_laps=int(total_laps),
        track_length_m=int(track_length),
        session_type=p.SESSION_TYPES.get(int(session_type), f"code {session_type}"),
        session_time_left_s=float(time_left),
    )


def plausible_status(status: "CarStatus") -> bool:
    """Catch a mis-strided CarStatus.

    The reported symptom was a rev limit of 35,262 - not a decode failure,
    just the wrong car's bytes. Bounds here are deliberately wide enough to
    cover any F1 power unit while still rejecting nonsense.
    """
    return (
        4000.0 <= status.max_rpm <= 20000.0
        and 0.0 <= status.idle_rpm < status.max_rpm
        and 0 <= status.max_gears <= 10
        and 0.0 <= status.fuel_in_tank <= 200.0
        and 0.0 <= status.fuel_capacity <= 200.0
        and -1 <= status.tyre_age_laps <= 200
        and 0.0 <= status.ers_store_joules <= 1e8
    )


def parse_car_status_full(data: bytes, header: p.PacketHeader) -> CarStatus | None:
    """Full CarStatusData: adds fuel, tyre compound/age and ERS."""
    return _solve_stride(
        data,
        header,
        known_stride=p.car_status_stride(
            header.packet_format, len(data) - header.size
        ),
        min_stride=p.CAR_STATUS_PREFIX_SIZE,
        max_stride=120,
        attempt=lambda offset: _parse_car_status_at(data, header, offset),
        learns_count=True,
    )


def _parse_car_status_at(
    data: bytes, header: p.PacketHeader, offset: int
) -> CarStatus | None:
    if not _player_slice_ok(data, offset, p.CAR_STATUS_FULL.size):
        # Fall back to the prefix, which still yields the rev range.
        return _parse_car_status_prefix_at(data, offset)
    try:
        (
            traction_control, anti_lock_brakes, _fuel_mix, _brake_bias,
            pit_limiter, fuel_in_tank, fuel_capacity, fuel_laps,
            max_rpm, idle_rpm, max_gears, _drs_allowed, _drs_distance,
            actual_compound, _visual_compound, tyre_age, _flags,
            _power_ice, _power_mguk, ers_store, ers_mode,
            harvested_mguk, harvested_mguh, deployed, _paused,
        ) = p.CAR_STATUS_FULL.unpack_from(data, offset)
    except struct.error:
        return _parse_car_status_prefix_at(data, offset)

    status = CarStatus(
        traction_control=int(traction_control),
        anti_lock_brakes=bool(anti_lock_brakes),
        pit_limiter=bool(pit_limiter),
        max_rpm=float(max_rpm),
        idle_rpm=float(idle_rpm),
        max_gears=int(max_gears),
        fuel_in_tank=float(fuel_in_tank),
        fuel_capacity=float(fuel_capacity),
        fuel_remaining_laps=float(fuel_laps),
        tyre_compound=p.TYRE_COMPOUNDS.get(int(actual_compound), ""),
        tyre_age_laps=int(tyre_age),
        ers_store_joules=float(ers_store),
        ers_mode=p.ERS_MODES.get(int(ers_mode), ""),
        ers_harvested_lap=float(harvested_mguk) + float(harvested_mguh),
        ers_deployed_lap=float(deployed),
    )
    if plausible_status(status):
        return status
    # Full layout did not validate; the prefix may still be right.
    return _parse_car_status_prefix_at(data, offset)


def _parse_car_status_prefix_at(data: bytes, offset: int) -> CarStatus | None:
    """Leading CarStatusData fields only - enough for the rev range."""
    if not _player_slice_ok(data, offset, p.CAR_STATUS_PREFIX_SIZE):
        return None
    try:
        (
            traction_control, anti_lock_brakes, _fuel_mix, _brake_bias,
            pit_limiter, fuel_in_tank, fuel_capacity, _fuel_laps,
            max_rpm, idle_rpm, max_gears,
        ) = p.CAR_STATUS_PREFIX.unpack_from(data, offset)
    except struct.error:
        return None

    status = CarStatus(
        traction_control=int(traction_control),
        anti_lock_brakes=bool(anti_lock_brakes),
        pit_limiter=bool(pit_limiter),
        max_rpm=float(max_rpm),
        idle_rpm=float(idle_rpm),
        max_gears=int(max_gears),
        fuel_in_tank=float(fuel_in_tank),
        fuel_capacity=float(fuel_capacity),
    )
    return status if plausible_status(status) else None


def plausible_damage(damage: "DamageData") -> bool:
    """Wear and damage are percentages; anything else is a bad decode."""
    return all(0.0 <= v <= 100.0 for v in damage.tyre_wear.as_tuple()) and all(
        0 <= v <= 100
        for v in (
            damage.front_left_wing, damage.front_right_wing, damage.rear_wing,
            damage.floor, damage.diffuser, damage.sidepod,
            damage.gearbox, damage.engine,
        )
    )


def parse_car_damage_full(data: bytes, header: p.PacketHeader) -> DamageData | None:
    """Tyre wear plus component damage percentages."""
    return _solve_stride(
        data,
        header,
        known_stride=p.CAR_DAMAGE_SIZE_2023,
        min_stride=p.CAR_DAMAGE_FULL.size,
        max_stride=120,
        attempt=lambda offset: _parse_car_damage_at(data, offset),
    )


def _parse_car_damage_at(data: bytes, offset: int) -> DamageData | None:
    if not _player_slice_ok(data, offset, p.CAR_DAMAGE_FULL.size):
        return None
    try:
        values = p.CAR_DAMAGE_FULL.unpack_from(data, offset)
    except struct.error:
        return None

    # 0-3 tyre wear, 4-7 tyre damage, 8-11 brake damage, then components.
    damage = DamageData(
        tyre_wear=p.to_wheels(values[0:4]),
        front_left_wing=int(values[12]),
        front_right_wing=int(values[13]),
        rear_wing=int(values[14]),
        floor=int(values[15]),
        diffuser=int(values[16]),
        sidepod=int(values[17]),
        gearbox=int(values[20]),
        engine=int(values[21]),
    )
    return damage if plausible_damage(damage) else None
