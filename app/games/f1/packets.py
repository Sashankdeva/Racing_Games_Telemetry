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

MAX_CARS = 22

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
