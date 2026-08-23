"""F1 26 (packet format 2026) carries 24 cars, not 22.

Established from a real session: the observed packet sizes only have one
consistent solution, and LapData/Participants keep their exact F1 25 entry
sizes while the totals grow by precisely two entries.

    packet         payload   F1 25 entry   F1 26 fit
    LapData          1370        57        24 x 57 + 2
    Participants     1441        60        24 x 60 + 1
    CarTelemetry     1419        60        24 x 59 + 3
    CarStatus        1416        55        24 x 59
    CarDamage        1104        42        24 x 46
    Motion           1296        60        24 x 54

With 22 assumed, every stride is wrong, the player's slice is read from the
wrong offset, and the dashboard shows garbage - which is exactly what was
reported: throttle frozen, speed and RPM stuck at zero, rev limit 35,262.

Only the array length and entry stride are treated as changed here. The
inner field layouts are the verified 2025 ones; where a 2026 entry is
shorter than the 2025 struct (CarTelemetry: 59 vs 60), the tail is required
to validate before it is believed.
"""

from __future__ import annotations

import struct

import pytest

from app.games.f1 import packets as p
from app.games.f1 import parser

HEADER = 29
CARS_26 = 24
PLAYER = 9


@pytest.fixture(autouse=True)
def clean_strides():
    parser.reset_stride_cache()
    yield
    parser.reset_stride_cache()


def header(packet_id: int, player: int = PLAYER) -> bytes:
    return struct.pack(
        "<HBBBBBQfIIBB", 2026, 26, 1, 0, 1, packet_id, 7, 1.0, 1, 1, player, 255
    )


def fit(entry: bytes, stride: int) -> bytes:
    return entry[:stride] if len(entry) > stride else entry + b"\x00" * (stride - len(entry))


def telemetry_packet(speed=210, rpm=11500, throttle=0.75, brake=0.0, gear=6):
    body = b""
    for car in range(CARS_26):
        if car == PLAYER:
            values = (speed, throttle, 0.1, brake, 0, gear, rpm)
        else:
            values = (150, 0.2, 0.0, 0.0, 0, 3, 8000)
        core = struct.pack("<HfffBbHBBH", *values, 0, 50, 0)
        tail = struct.pack(
            "<4H4B4BH4f4B", *[380] * 4, *[88] * 4, *[95] * 4, 105,
            *[22.5] * 4, *[0] * 4,
        )
        body += fit(core + tail, 59)
    return header(6) + body + b"\x00" * 3


def status_packet(max_rpm=13000, fuel=88.0):
    body = b""
    for car in range(CARS_26):
        entry = struct.pack(
            "<BBBBBfffHHBBHBBBbfffBfffB",
            1, 1, 1, 52, 0,
            fuel if car == PLAYER else 50.0, 110.0, 20.0,
            max_rpm if car == PLAYER else 11000, 4500,
            8, 1, 0, 16, 16, 5, 0,
            300.0, 120.0, 3_400_000.0, 2, 1.0, 2.0, 3.0, 0,
        )
        body += fit(entry, 59)
    return header(7) + body


def lap_packet(position=3, lap=4):
    body = b""
    for car in range(CARS_26):
        entry = struct.pack(
            "<IIHBHBHHfffBBBBBBB",
            91234, 45000, 28500, 0, 31200, 0, 420, 1900,
            1200.0, 5000.0, 0.0,
            position if car == PLAYER else 15, lap, 0, 1, 1, 0, 0,
        )
        body += fit(entry, 57)
    return header(2) + body + b"\x00" * 2


def motion_packet(g_lat=2.1, g_lon=-1.4):
    body = b""
    for car in range(CARS_26):
        lat, lon = (g_lat, g_lon) if car == PLAYER else (0.0, 0.0)
        entry = struct.pack(
            "<6f6h", 100.0, 5.0, 200.0, 55.0, 0.0, 10.0, 0, 0, 0, 0, 0, 0
        ) + struct.pack("<3f", lat, lon, 0.4)
        body += fit(entry, 54)
    return header(0) + body


def damage_packet(wear=37.5):
    body = b""
    for car in range(CARS_26):
        value = wear if car == PLAYER else 2.0
        entry = struct.pack(
            "<4f4B4B" + "B" * 18, value, value, value, value, *[0] * 8, *[0] * 18
        )
        body += fit(entry, 46)
    return header(10) + body


class TestPacketSizesMatchTheRealGame:
    """If these drift, the fixtures no longer describe the real game."""

    @pytest.mark.parametrize(
        "builder,expected",
        [
            (telemetry_packet, 1448),
            (status_packet, 1445),
            (lap_packet, 1399),
            (motion_packet, 1325),
            (damage_packet, 1133),
        ],
    )
    def test_size(self, builder, expected):
        assert len(builder()) == expected


class TestStrideSolving:
    def test_car_telemetry_reads_the_players_car(self):
        data = telemetry_packet(speed=233, rpm=12100, throttle=0.9, brake=0.0, gear=7)
        result = parser.parse_car_telemetry(data, p.parse_header(data))

        assert result is not None
        assert result.speed_kph == 233
        assert result.engine_rpm == 12100
        assert result.throttle == pytest.approx(0.9, abs=1e-6)
        assert result.gear == 7

    def test_throttle_tracks_its_input(self):
        """The reported fault: brake moved, throttle was frozen."""
        seen = []
        for value in (0.0, 0.25, 0.5, 0.75, 1.0):
            data = telemetry_packet(throttle=value)
            result = parser.parse_car_telemetry(data, p.parse_header(data))
            seen.append(round(result.throttle, 3))
        assert seen == [0.0, 0.25, 0.5, 0.75, 1.0]

    def test_speed_and_rpm_are_not_stuck_at_zero(self):
        for speed, rpm in ((0, 4200), (120, 9000), (310, 13000)):
            data = telemetry_packet(speed=speed, rpm=rpm)
            result = parser.parse_car_telemetry(data, p.parse_header(data))
            assert result.speed_kph == speed
            assert result.engine_rpm == rpm

    def test_car_status_rev_limit_is_sane(self):
        """The live session reported max_rpm 35262 from a mis-strided read."""
        data = status_packet(max_rpm=13000, fuel=88.0)
        result = parser.parse_car_status_full(data, p.parse_header(data))

        assert result is not None
        assert result.max_rpm == 13000
        assert result.fuel_in_tank == pytest.approx(88.0)

    def test_lap_data_reads_the_players_position(self):
        data = lap_packet(position=3, lap=4)
        result = parser.parse_lap_data(data, p.parse_header(data))

        assert result is not None
        assert result.position == 3
        assert result.current_lap == 4

    def test_motion_g_forces_are_physical(self):
        """Mis-strided motion produced values like -8e6 and 1e32."""
        data = motion_packet(g_lat=2.1, g_lon=-1.4)
        result = parser.parse_motion(data, p.parse_header(data))

        assert result is not None
        assert result.g_lateral == pytest.approx(2.1, abs=1e-5)
        assert result.g_longitudinal == pytest.approx(-1.4, abs=1e-5)

    def test_damage_needs_the_car_count_from_other_packets(self):
        """Damage is legitimately all-zero, so its own bounds check cannot
        pick the stride. It must inherit the count learned elsewhere."""
        telemetry = telemetry_packet()
        parser.parse_car_telemetry(telemetry, p.parse_header(telemetry))

        data = damage_packet(wear=37.5)
        result = parser.parse_car_damage_full(data, p.parse_header(data))

        assert result is not None
        assert result.tyre_wear.fl == pytest.approx(37.5)

    def test_solved_strides_match_the_arithmetic(self):
        # Each packet id is routed to exactly one parser by the adapter, so
        # pair them the same way here.
        for builder, fn in (
            (telemetry_packet, parser.parse_car_telemetry),
            (status_packet, parser.parse_car_status_full),
            (lap_packet, parser.parse_lap_data),
            (motion_packet, parser.parse_motion),
            (damage_packet, parser.parse_car_damage_full),
        ):
            data = builder()
            assert fn(data, p.parse_header(data)) is not None

        assert parser._stride_cache[(6, 1419)] == 59
        assert parser._stride_cache[(7, 1416)] == 59
        assert parser._stride_cache[(2, 1370)] == 57
        assert parser._stride_cache[(0, 1296)] == 54
        assert parser._stride_cache[(10, 1104)] == 46


class TestUnverifiedTailIsNotInvented:
    """F1 26's CarTelemetry entry is 59 bytes against a 60-byte 2025 struct,
    so the thermal tail cannot simply be read across. Showing shifted bytes
    as tyre temperatures would be fabrication."""

    def test_tail_is_only_used_when_it_validates(self):
        data = telemetry_packet()
        result = parser.parse_car_telemetry(data, p.parse_header(data))

        assert result is not None
        for value in result.tyre_pressure.as_tuple():
            assert value == 0.0 or 5.0 <= value <= 45.0

    def test_core_survives_an_unreadable_tail(self):
        """Truncate every entry to the core: driving data must still work."""
        body = b""
        for _ in range(CARS_26):
            body += struct.pack("<HfffBbHBBH", 195, 0.6, 0.0, 0.0, 0, 5, 10500, 0, 50, 0)
        data = header(6, player=0) + body

        result = parser.parse_car_telemetry(data, p.parse_header(data))
        assert result is not None
        assert result.speed_kph == 195
        assert result.engine_rpm == 10500


class TestF1_25StillWorks:
    """The 22-car layout must not regress."""

    def test_22_car_telemetry_still_parses(self):
        body = b""
        for car in range(22):
            values = (240, 0.85, 0.0, 0.0, 0, 7, 11900) if car == 5 else (100, 0.1, 0.0, 0.0, 0, 2, 5000)
            body += struct.pack(
                "<HfffBbHBBH4H4B4BH4f4B", *values, 0, 50, 0,
                *[400] * 4, *[90] * 4, *[95] * 4, 110, *[23.0] * 4, *[0] * 4,
            )
        data = struct.pack(
            "<HBBBBBQfIIBB", 2025, 25, 1, 0, 1, 6, 7, 1.0, 1, 1, 5, 255
        ) + body + b"\x00" * 3

        assert len(data) == 1352  # the real F1 25 size
        result = parser.parse_car_telemetry(data, p.parse_header(data))
        assert result is not None
        assert result.speed_kph == 240
        assert result.engine_rpm == 11900
        assert result.tyre_pressure.fl == pytest.approx(23.0)
