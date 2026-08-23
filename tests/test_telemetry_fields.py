"""End-to-end telemetry: every Phase 1 field traced over a real UDP socket.

Encodes known values into byte-exact F1 packets, sends them through the
actual socket, and asserts the normalized frame matches. This is the test
that would have caught a wrong struct offset or a dropped field.
"""
import socket
import struct
import time

from app.config.settings import AppSettings
from app.core.application import Application

import pytest

PORT = 20971


def hdr(pid):
    return struct.pack("<HBBBBBQfIIBB", 2026, 26, 1, 0, 1, pid, 999, 1.5, 100, 100, 0, 255)


# --- known values, F1 wheel order RL, RR, FL, FR ---
SPEED, RPM, GEAR, THR, BRK, STEER = 287, 11800, 7, 0.83, 0.21, -0.35
MAXRPM, IDLE = 13000, 4200
BRAKE_T = (520, 530, 540, 550)
SURF_T = (88, 89, 90, 91)
INNER_T = (95, 96, 97, 98)
PRESS = (22.1, 22.2, 23.3, 23.4)
FUEL, FUELCAP, FUELLAPS = 88.5, 110.0, 1.7
ERS_J, ERS_MODE, TYRE_AGE, COMPOUND = 3_200_000.0, 3, 12, 17  # 17 = Medium
POSITION, LAP, LASTLAP_MS, S1_MS, S2_MS = 4, 23, 91_234, 28_100, 31_500
TOTAL_LAPS, TRACK_T, AIR_T, WEATHER, SESSION = 58, 41, 27, 2, 15  # overcast / Race
WEAR = (11.0, 12.0, 13.0, 14.0)
FLW, FRW, RW, FLOOR, DIFF, SIDE, GBX, ENG = 5, 8, 3, 2, 1, 4, 6, 9

car = struct.pack(
    "<HfffBbHBBH4H4B4BH4f4B", SPEED, THR, STEER, BRK, 50, GEAR, RPM, 1, 90, 0,
    *BRAKE_T, *SURF_T, *INNER_T, 110, *PRESS, 0, 0, 0, 0,
)
status = struct.pack(
    "<BBBBBfffHHBBHBBBbfffBfffB", 0, 1, 1, 50, 0, FUEL, FUELCAP, FUELLAPS,
    MAXRPM, IDLE, 8, 1, 0, COMPOUND, 16, TYRE_AGE, 0,
    500.0, 120.0, ERS_J, ERS_MODE, 1.0, 2.0, 3.0, 0,
)
lap = struct.pack(
    "<IIHBHBHHfffBBBBBBB", LASTLAP_MS, 45_000, S1_MS, 0, S2_MS, 0, 1200, 5400,
    1500.0, 60000.0, 0.0, POSITION, LAP, 0, 1, 1, 0, 0,
) + b"\x00" * 13
session = struct.pack(
    "<BbbBHBbBHH", WEATHER, TRACK_T, AIR_T, TOTAL_LAPS, 5300, SESSION, 1, 0, 1800, 3600
) + b"\x00" * 600
damage = struct.pack(
    "<4f4B4B" + "B" * 18, *WEAR, 0, 0, 0, 0, 0, 0, 0, 0,
    FLW, FRW, RW, FLOOR, DIFF, SIDE, 0, 0, GBX, ENG, 0, 0, 0, 0, 0, 0, 0, 0,
)


@pytest.fixture
def live_app():
    app = Application(AppSettings())
    # Transport settings are per game mode now, so set them on the mode
    # rather than on the global settings object.
    app.mode_settings.udp_port = PORT
    app.mode_settings.auto_start_telemetry = True
    app._configure_adapter()
    app.startup()
    time.sleep(0.4)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for pkt in (
        hdr(1) + session,
        hdr(7) + status * 22,
        hdr(2) + lap * 22,
        hdr(10) + damage * 22,
        hdr(6) + car * 22,
    ):
        sender.sendto(pkt, ("127.0.0.1", PORT))
        time.sleep(0.03)
    time.sleep(0.2)
    yield app
    app.shutdown()


class TestFullFieldTrace:
    def test_pipeline_reaches_live(self, live_app):
        status_ = live_app.report().adapter
        assert int(status_.stage) == 6
        assert status_.packets_rejected == 0
        assert live_app.telemetry.snapshot().live

    @pytest.mark.parametrize(
        "field,expected",
        [
            ("rpm", RPM), ("speed_kph", SPEED), ("gear", GEAR),
            ("max_rpm", MAXRPM), ("position", POSITION), ("current_lap", LAP),
            ("total_laps", TOTAL_LAPS), ("tyre_age_laps", TYRE_AGE),
            ("front_left_wing_damage", FLW), ("rear_wing_damage", RW),
            ("gearbox_damage", GBX), ("engine_damage", ENG),
            ("track_temperature", TRACK_T), ("air_temperature", AIR_T),
        ],
    )
    def test_integer_fields(self, live_app, field, expected):
        assert getattr(live_app.telemetry.snapshot().frame, field) == expected

    @pytest.mark.parametrize(
        "field,expected",
        [
            ("throttle", THR), ("brake", BRK), ("steering", STEER),
            ("fuel_in_tank", FUEL), ("fuel_remaining_laps", FUELLAPS),
            ("ers_store_percent", 80.0), ("last_lap_time_s", 91.234),
            ("sector1_time_s", 28.1), ("sector2_time_s", 31.5),
            ("delta_to_car_ahead_s", 1.2), ("delta_to_leader_s", 5.4),
        ],
    )
    def test_float_fields(self, live_app, field, expected):
        assert getattr(live_app.telemetry.snapshot().frame, field) == pytest.approx(
            expected, abs=0.02
        )

    @pytest.mark.parametrize(
        "field,expected",
        [("tyre_compound", "Medium"), ("ers_mode", "Overtake"),
         ("weather", "Overcast"), ("session_type", "Race")],
    )
    def test_string_fields(self, live_app, field, expected):
        assert getattr(live_app.telemetry.snapshot().frame, field) == expected

    def test_per_wheel_mapping(self, live_app):
        """F1 sends RL, RR, FL, FR - the classic off-by-two trap."""
        f = live_app.telemetry.snapshot().frame
        assert f.tyre_surface_temp.fl == SURF_T[2]
        assert f.tyre_surface_temp.fr == SURF_T[3]
        assert f.tyre_surface_temp.rl == SURF_T[0]
        assert f.tyre_surface_temp.rr == SURF_T[1]
        assert f.tyre_wear.fl == pytest.approx(WEAR[2])
        assert f.brake_temp.fl == BRAKE_T[2]
        assert f.tyre_pressure.fl == pytest.approx(PRESS[2], abs=0.01)

    def test_stale_frame_is_not_reported_live(self, live_app):
        """Stale must not read as live - but must not be erased either.

        This test previously asserted `not snapshot.frame.valid`, which
        encoded the bug: going stale wiped the lap number, position, tyre
        compound and everything else the driver was looking at. A dropped
        packet does not move the car off lap 18.
        """
        from app.core.telemetry_state import TelemetryStatus

        before = live_app.telemetry.snapshot().frame
        live_app.telemetry.set_timeout(0.05)
        time.sleep(0.2)

        snapshot = live_app.telemetry.snapshot()
        assert not snapshot.live
        assert snapshot.status is TelemetryStatus.STALE
        # The values survive, and are flagged rather than blanked.
        assert snapshot.frame.valid
        assert snapshot.frame is before
        assert snapshot.age > 0.05
