"""F1 UDP packet layout constants (F1 22 / 23 / 24).

Codemasters' telemetry format changes between titles, mostly by *appending*
fields. This module therefore records only what is needed and treats every
layout as a prefix: parsing reads the leading fields it understands and
ignores whatever trails them. That way an F1 24 packet with extra members
still parses correctly instead of failing a strict size check.

Wheel ordering is the classic trap: F1 arrays are [RL, RR, FL, FR], not
front-first. `to_wheels()` is the single place that mapping happens.
"""

from __future__ import annotations

import struct

from app.core.models import SurfaceType, Wheels

# --- packet ids -----------------------------------------------------------
PACKET_MOTION = 0
PACKET_SESSION = 1
PACKET_LAP_DATA = 2
PACKET_EVENT = 3
PACKET_PARTICIPANTS = 4
PACKET_CAR_SETUPS = 5
PACKET_CAR_TELEMETRY = 6
PACKET_CAR_STATUS = 7
PACKET_FINAL_CLASSIFICATION = 8
PACKET_LOBBY_INFO = 9
PACKET_CAR_DAMAGE = 10
PACKET_SESSION_HISTORY = 11
PACKET_TYRE_SETS = 12
PACKET_MOTION_EX = 13

#: Formats with layouts verified against the published spec.
KNOWN_FORMATS = (2022, 2023, 2024, 2025)

#: Accepted range. Deliberately a RANGE, not an allowlist: an allowlist
#: silently rejects every packet from any newer title (F1 26 reports format
#: 2026), which looks identical to "the game is not sending". Codemasters
#: has only ever appended fields, and the header has been stable since
#: 2023, so an unknown newer format is parsed with the newest known layout
#: and the trailing bytes are ignored. Far better to try and validate than
#: to refuse outright.
MIN_PACKET_FORMAT = 2018
MAX_PACKET_FORMAT = 2035

#: Kept for backwards compatibility with existing callers/tests.
SUPPORTED_FORMATS = KNOWN_FORMATS


def is_supported_format(packet_format: int) -> bool:
    return MIN_PACKET_FORMAT <= packet_format <= MAX_PACKET_FORMAT

#: Historic array size. F1 22-25 carry 22 cars; F1 26 (packet format 2026)
#: carries 24. Kept as the default for the layouts verified against the
#: published spec, but NEVER assumed when deriving a stride - see
#: candidate_strides().
MAX_CARS = 22

#: Array sizes to consider when solving a packet's per-car stride. Ordered
#: by how likely each is; the plausibility checks decide the winner, so
#: order only affects how quickly the right one is found.
CANDIDATE_CAR_COUNTS = (22, 24, 20, 26)

#: Trailing bytes to consider after the per-car array. Codemasters puts a
#: handful of scalars there (suggested gear, time-trial indices, ...).
MAX_TRAILING_BYTES = 8


def candidate_strides(
    payload_size: int,
    known_stride: int,
    min_stride: int,
    max_stride: int,
    counts: tuple[int, ...] = CANDIDATE_CAR_COUNTS,
) -> list[tuple[int, int]]:
    """(stride, car count) pairs that exactly account for `payload_size`.

    A packet is `count` entries of `stride` bytes plus a few trailing
    scalars. Given the observed size, only some (count, stride, trailing)
    combinations are arithmetically possible - this enumerates them instead
    of hardcoding a car count.

    That matters because F1 26 grew the arrays from 22 to 24 entries. With
    22 assumed, every derived stride is wrong, the player's slice is read
    from the wrong offset, and every field decodes as garbage. Solving from
    the real size also means a future title that changes this again works.

    The known-good stride is offered first, but ONLY when it actually fits
    the observed size. Offering it unconditionally was a trap: for an F1 26
    motion packet the 2025 stride of 60 does not divide the payload at all,
    yet it still read bytes that happened to be zero and passed a bounds
    check. An arithmetically impossible layout is never a candidate.
    """
    ordered: list[tuple[int, int]] = []
    seen: set[int] = set()

    def add(stride: int, count: int) -> None:
        if min_stride <= stride <= max_stride and stride not in seen:
            seen.add(stride)
            ordered.append((stride, count))

    for count in counts:
        trailing = payload_size - known_stride * count
        if 0 <= trailing <= MAX_TRAILING_BYTES:
            add(known_stride, count)
            break

    for count in counts:
        for trailing in range(MAX_TRAILING_BYTES + 1):
            usable = payload_size - trailing
            if usable > 0 and usable % count == 0:
                add(usable // count, count)
    return ordered

# --- header ---------------------------------------------------------------
# 2022: format, majorVer, minorVer, packetVer, packetId, sessionUID,
#       sessionTime, frameId, playerCarIndex, secondaryPlayerCarIndex
_HEADER_2022 = struct.Struct("<HBBBBQfIBB")
HEADER_SIZE_2022 = _HEADER_2022.size  # 24

# 2023+: adds gameYear after format, and overallFrameIdentifier after frameId
_HEADER_2023 = struct.Struct("<HBBBBBQfIIBB")
HEADER_SIZE_2023 = _HEADER_2023.size  # 29


class PacketHeader:
    __slots__ = (
        "packet_format",
        "game_year",
        "packet_version",
        "packet_id",
        "session_time",
        "frame_identifier",
        "player_car_index",
        "size",
    )

    def __init__(
        self,
        packet_format: int,
        game_year: int,
        packet_version: int,
        packet_id: int,
        session_time: float,
        frame_identifier: int,
        player_car_index: int,
        size: int,
    ) -> None:
        self.packet_format = packet_format
        self.game_year = game_year
        self.packet_version = packet_version
        self.packet_id = packet_id
        self.session_time = session_time
        self.frame_identifier = frame_identifier
        self.player_car_index = player_car_index
        self.size = size

    @property
    def is_legacy_layout(self) -> bool:
        """True for the 2022 header/motion layout."""
        return self.packet_format <= 2022


def parse_header(data: bytes) -> PacketHeader | None:
    """Decode the packet header, or None if it is not a packet we know.

    The format field is read first and decides which header layout applies,
    so an unknown title is rejected cleanly rather than mis-parsed.
    """
    if len(data) < 6:
        return None

    packet_format = struct.unpack_from("<H", data, 0)[0]
    if not is_supported_format(packet_format):
        return None

    if packet_format <= 2022:
        if len(data) < HEADER_SIZE_2022:
            return None
        (
            fmt,
            _major,
            _minor,
            packet_version,
            packet_id,
            _uid,
            session_time,
            frame_id,
            player_index,
            _secondary,
        ) = _HEADER_2022.unpack_from(data, 0)
        return PacketHeader(
            fmt, 22, packet_version, packet_id, session_time, frame_id,
            player_index, HEADER_SIZE_2022,
        )

    if len(data) < HEADER_SIZE_2023:
        return None
    (
        fmt,
        game_year,
        _major,
        _minor,
        packet_version,
        packet_id,
        _uid,
        session_time,
        frame_id,
        _overall_frame_id,
        player_index,
        _secondary,
    ) = _HEADER_2023.unpack_from(data, 0)
    return PacketHeader(
        fmt, game_year, packet_version, packet_id, session_time, frame_id,
        player_index, HEADER_SIZE_2023,
    )


# --- car telemetry (id 6) -------------------------------------------------
# Stable across 2022-2024.
CAR_TELEMETRY = struct.Struct("<HfffBbHBBH4H4B4BH4f4B")
CAR_TELEMETRY_SIZE = CAR_TELEMETRY.size  # 60

# Split into the driving core and the tyre/thermal tail, because F1 26 uses
# a 59-byte entry: reading the full 60-byte struct there would overrun into
# the next car. The core - speed, pedals, gear, RPM - leads the struct and
# has been byte-identical since 2022, so it stays trustworthy even when the
# tail's layout is unknown. The tail is then parsed only if it validates,
# rather than showing shifted bytes as if they were tyre temperatures.
CAR_TELEMETRY_CORE = struct.Struct("<HfffBbHBBH")
CAR_TELEMETRY_CORE_SIZE = CAR_TELEMETRY_CORE.size  # 18
CAR_TELEMETRY_TAIL = struct.Struct("<4H4B4BH4f4B")
CAR_TELEMETRY_TAIL_SIZE = CAR_TELEMETRY_TAIL.size  # 42

# --- car status (id 7) ----------------------------------------------------
# Only the leading fields are read; anything after m_maxGears is ignored.
CAR_STATUS_PREFIX = struct.Struct("<BBBBBfffHHB")
CAR_STATUS_PREFIX_SIZE = CAR_STATUS_PREFIX.size  # 22
#: Full per-car stride, needed to index the player's entry.
CAR_STATUS_SIZE_2023 = 55
CAR_STATUS_SIZE_2022 = 47

# --- motion (id 0) --------------------------------------------------------
CAR_MOTION = struct.Struct("<6f6h6f")
CAR_MOTION_SIZE = CAR_MOTION.size  # 60
#: Offset of m_gForceLateral inside one CarMotionData entry.
G_FORCE_OFFSET = 36
G_FORCES = struct.Struct("<3f")

# --- motion ex (id 13, 2023+) / motion tail (2022) ------------------------
# The five arrays we need sit at the very start of this block and are at the
# same offsets in 2023 and 2024 (2024 renamed m_wheelSlip to
# m_wheelSlipRatio and appended more arrays after them).
MOTION_EX_PREFIX = struct.Struct("<4f4f4f4f4f")
MOTION_EX_PREFIX_SIZE = MOTION_EX_PREFIX.size  # 80

# --- lap data (id 2) ------------------------------------------------------
#: Byte offset of m_pitStatus inside one LapData entry (same 2022-2024).
LAP_PIT_STATUS_OFFSET = 32
LAP_DATA_SIZE_2023 = 50
LAP_DATA_SIZE_2022 = 43

# --- car damage (id 10) ---------------------------------------------------
CAR_DAMAGE_SIZE_2023 = 42
#: tyresWear[4] float + tyresDamage[4] + brakesDamage[4], then wings.
CAR_DAMAGE_WING_OFFSET = 24
CAR_DAMAGE_WINGS = struct.Struct("<4B")  # FL wing, FR wing, rear wing, floor

# --- event (id 3) ---------------------------------------------------------
EVENT_CODE_SIZE = 4


def to_wheels(values: tuple[float, ...] | list[float]) -> Wheels:
    """Map an F1 [RL, RR, FL, FR] array onto named wheels.

    Every per-wheel array in the F1 spec uses this ordering; doing the
    mapping in one place is what keeps the rest of the codebase from having
    to remember it.
    """
    return Wheels(fl=values[2], fr=values[3], rl=values[0], rr=values[1])


def to_surfaces(values: tuple[int, ...] | list[int]) -> tuple[SurfaceType, ...]:
    """Map an F1 surface-type array onto SurfaceType, in fl/fr/rl/rr order."""
    mapped = []
    for index in (2, 3, 0, 1):
        try:
            mapped.append(SurfaceType(values[index]))
        except ValueError:
            mapped.append(SurfaceType.UNKNOWN)
    return tuple(mapped)


def car_status_stride(packet_format: int, payload_size: int = 0) -> int:
    """Per-car stride for CarStatusData.

    Derived from the payload where possible so a newer title that grew the
    struct still indexes the right car, falling back to the known sizes.
    """
    known = CAR_STATUS_SIZE_2022 if packet_format <= 2022 else CAR_STATUS_SIZE_2023
    if payload_size <= 0:
        return known
    derived = payload_size // MAX_CARS
    # Only trust the derived value if it is in a sane neighbourhood.
    if 40 <= derived <= 120:
        return derived
    return known


def car_telemetry_stride(payload_size: int = 0) -> int:
    """Per-car stride for CarTelemetryData.

    60 bytes across every format verified so far. The payload also carries
    a few trailing bytes (MFD panel indices, suggested gear), so the
    derived value is only used when it is clearly a better fit.
    """
    if payload_size <= 0:
        return CAR_TELEMETRY_SIZE
    derived = (payload_size - 3) // MAX_CARS
    if 50 <= derived <= 100:
        return derived
    return CAR_TELEMETRY_SIZE


def lap_data_stride(packet_format: int, payload_size: int) -> int:
    """LapData grew between titles; derive the stride from the payload when
    possible so a version we have not hard-coded still indexes correctly."""
    derived = payload_size // MAX_CARS
    if derived >= 40:
        return derived
    return LAP_DATA_SIZE_2022 if packet_format <= 2022 else LAP_DATA_SIZE_2023


# --- extended layouts (Phase 1 telemetry expansion) -----------------------
# Full CarStatusData, not just the prefix: fuel, ERS, tyre compound and age.
CAR_STATUS_FULL = struct.Struct("<BBBBBfffHHBBHBBBbfffBfffB")

# LapData prefix through m_penalties. Parsed as a prefix because later
# titles append fields; everything we need leads the struct.
LAP_DATA_PREFIX = struct.Struct("<IIHBHBHHfffBBBBBBB")

# PacketSessionData prefix: weather, temperatures, laps, session type.
SESSION_PREFIX = struct.Struct("<BbbBHBbBHH")

# CarDamageData: tyre wear then the component damage bytes.
CAR_DAMAGE_FULL = struct.Struct("<4f4B4B" + "B" * 18)

#: Tyre compound codes -> readable names (actual compound, not visual).
TYRE_COMPOUNDS = {
    16: "Soft", 17: "Medium", 18: "Hard", 7: "Inter", 8: "Wet",
    19: "Super Soft", 20: "Soft", 21: "Medium", 22: "Hard",
    9: "Dry", 10: "Wet", 11: "Inter", 12: "Wet",
}

WEATHER = {
    0: "Clear", 1: "Light cloud", 2: "Overcast",
    3: "Light rain", 4: "Heavy rain", 5: "Storm",
}

SESSION_TYPES = {
    0: "Unknown", 1: "Practice 1", 2: "Practice 2", 3: "Practice 3",
    4: "Short Practice", 5: "Qualifying 1", 6: "Qualifying 2",
    7: "Qualifying 3", 8: "Short Qualifying", 9: "One-Shot Qualifying",
    10: "Sprint Shootout 1", 11: "Sprint Shootout 2", 12: "Sprint Shootout 3",
    13: "Short Sprint Shootout", 14: "One-Shot Sprint Shootout",
    15: "Race", 16: "Race 2", 17: "Race 3", 18: "Time Trial",
}

ERS_MODES = {0: "None", 1: "Medium", 2: "Hotlap", 3: "Overtake"}
#: F1 reports ERS store in joules; this is the full-charge figure.
ERS_MAX_JOULES = 4_000_000.0
