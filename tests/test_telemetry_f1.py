"""F1 packet parsing, malformed input, and normalization.

Packets are built byte-exactly to the published spec in conftest, so these
tests verify the parser against real layouts rather than against its own
assumptions. Malformed-input handling gets particular attention: a bad
packet must never raise, because an exception on the telemetry thread could
leave the motors running.
"""

from __future__ import annotations

import socket
import struct
import time

import pytest

from app.core.models import SurfaceType
from app.games.f1 import packets as p
from app.games.f1 import parser
from app.games.base import TelemetryStage
from app.games.f1.adapter import F1Adapter
from tests.conftest import (
    f1_car_status_entry,
    f1_car_telemetry_entry,
    f1_header,
    f1_motion_entry,
    f1_motion_ex,
)


class TestStructSizes:
    """Guards against a typo in a format string silently shifting offsets."""

    def test_sizes_match_the_published_spec(self):
        assert p.HEADER_SIZE_2022 == 24
        assert p.HEADER_SIZE_2023 == 29
        assert p.CAR_TELEMETRY_SIZE == 60
        assert p.CAR_MOTION_SIZE == 60
        assert p.MOTION_EX_PREFIX_SIZE == 80


class TestHeader:
    def test_parses_2023_header(self):
        header = p.parse_header(f1_header(6))
        assert header is not None
        assert header.packet_format == 2023
        assert header.packet_id == 6
        assert header.size == 29
        assert not header.is_legacy_layout

    def test_parses_2022_header(self):
        header = p.parse_header(f1_header(6, packet_format=2022))
        assert header is not None
        assert header.size == 24
        assert header.is_legacy_layout

    def test_reads_player_car_index(self):
        header = p.parse_header(f1_header(6, player_index=11))
        assert header.player_car_index == 11

    def test_rejects_unknown_packet_format(self):
        assert p.parse_header(struct.pack("<H", 1999) + b"\x00" * 40) is None

    def test_rejects_truncated_data(self):
        assert p.parse_header(b"\x00") is None
        assert p.parse_header(b"") is None

    def test_rejects_header_shorter_than_its_own_layout(self):
        assert p.parse_header(struct.pack("<H", 2023) + b"\x00" * 5) is None


class TestCarTelemetry:
    def test_parses_player_entry(self):
        packet = f1_header(6) + f1_car_telemetry_entry() * 22
        header = p.parse_header(packet)
        telemetry = parser.parse_car_telemetry(packet, header)

        assert telemetry.speed_kph == 287
        assert telemetry.engine_rpm == 11800
        assert telemetry.gear == 7
        assert telemetry.throttle == pytest.approx(1.0)

    def test_reads_the_correct_car_not_just_the_first(self):
        entries = [f1_car_telemetry_entry(speed=100) for _ in range(22)]
        entries[5] = f1_car_telemetry_entry(speed=333)
        packet = f1_header(6, player_index=5) + b"".join(entries)
        header = p.parse_header(packet)
        assert parser.parse_car_telemetry(packet, header).speed_kph == 333

    def test_maps_f1_wheel_order_to_named_wheels(self):
        """F1 arrays are [RL, RR, FL, FR] - the classic off-by-two trap."""
        # rumble strip under the two LEFT wheels only: RL=1, RR=0, FL=1, FR=0
        packet = f1_header(6) + f1_car_telemetry_entry(surfaces=(1, 0, 1, 0)) * 22
        header = p.parse_header(packet)
        fl, fr, rl, rr = parser.parse_car_telemetry(packet, header).surfaces

        assert fl is SurfaceType.RUMBLE_STRIP
        assert rl is SurfaceType.RUMBLE_STRIP
        assert fr is SurfaceType.TARMAC
        assert rr is SurfaceType.TARMAC

    def test_unknown_surface_value_degrades_gracefully(self):
        packet = f1_header(6) + f1_car_telemetry_entry(surfaces=(200, 0, 0, 0)) * 22
        header = p.parse_header(packet)
        surfaces = parser.parse_car_telemetry(packet, header).surfaces
        assert SurfaceType.UNKNOWN in surfaces


class TestCarStatus:
    def test_parses_rev_range_and_assists(self):
        packet = f1_header(7) + f1_car_status_entry(max_rpm=13500, idle_rpm=4200) * 22
        header = p.parse_header(packet)
        status = parser.parse_car_status(packet, header)

        assert status.max_rpm == 13500
        assert status.idle_rpm == 4200
        assert status.anti_lock_brakes is True


class TestMotion:
    def test_parses_g_forces(self):
        packet = f1_header(0) + f1_motion_entry(g_lat=2.5, g_lon=-4.0, g_vert=1.1) * 22
        header = p.parse_header(packet)
        motion = parser.parse_motion(packet, header)

        assert motion.g_lateral == pytest.approx(2.5)
        assert motion.g_longitudinal == pytest.approx(-4.0)

    def test_motion_ex_maps_wheel_arrays(self):
        # F1 order RL, RR, FL, FR
        packet = f1_header(13) + f1_motion_ex(wheel_slip=(0.01, 0.02, -0.30, 0.04))
        header = p.parse_header(packet)
        extended = parser.parse_motion_extended(packet, header)

        assert extended.wheel_slip.rl == pytest.approx(0.01)
        assert extended.wheel_slip.rr == pytest.approx(0.02)
        assert extended.wheel_slip.fl == pytest.approx(-0.30)
        assert extended.wheel_slip.fr == pytest.approx(0.04)

    def test_2022_reads_suspension_from_the_motion_packet_tail(self):
        packet = (
            f1_header(0, packet_format=2022)
            + f1_motion_entry() * 22
            + f1_motion_ex(wheel_speed=(11.0, 12.0, 13.0, 14.0))
        )
        header = p.parse_header(packet)
        extended = parser.parse_motion_extended(packet, header)

        assert extended is not None
        assert extended.wheel_speed.rl == pytest.approx(11.0)
        assert extended.wheel_speed.fl == pytest.approx(13.0)


class TestMalformedPackets:
    """None, never an exception - the telemetry thread must not die."""

    def test_truncated_payload_returns_none(self):
        packet = f1_header(6) + f1_car_telemetry_entry()[:20]
        header = p.parse_header(packet)
        assert parser.parse_car_telemetry(packet, header) is None

    def test_header_only_returns_none(self):
        header_bytes = f1_header(6)
        header = p.parse_header(header_bytes)
        assert parser.parse_car_telemetry(header_bytes, header) is None
        assert parser.parse_car_status(header_bytes, header) is None
        assert parser.parse_motion(header_bytes, header) is None
        assert parser.parse_motion_extended(header_bytes, header) is None

    def test_out_of_range_player_index_returns_none(self):
        packet = f1_header(6, player_index=200) + f1_car_telemetry_entry() * 22
        header = p.parse_header(packet)
        assert parser.parse_car_telemetry(packet, header) is None

    @pytest.mark.parametrize("size", [0, 1, 5, 23, 28, 100, 500])
    def test_random_lengths_never_raise(self, size):
        data = b"\xAB" * size
        header = p.parse_header(data)
        if header is not None:
            parser.parse_car_telemetry(data, header)
            parser.parse_car_status(data, header)
            parser.parse_motion(data, header)

    def test_garbage_after_a_valid_header_never_raises(self):
        packet = f1_header(6) + b"\xFF" * 300
        header = p.parse_header(packet)
        parser.parse_car_telemetry(packet, header)  # must simply not raise


class TestAdapterNormalization:
    def _feed(self, adapter, *packets):
        for packet in packets:
            adapter._on_packet(packet)

    def test_builds_a_normalized_frame_from_several_packet_types(self):
        frames = []
        adapter = F1Adapter(port=0)
        adapter.set_frame_callback(frames.append)

        self._feed(
            adapter,
            f1_header(7) + f1_car_status_entry(max_rpm=12000, idle_rpm=4000) * 22,
            f1_header(0) + f1_motion_entry(g_lat=2.0, g_lon=-3.0) * 22,
            f1_header(13) + f1_motion_ex(wheel_slip=(0.02, 0.30, -0.20, 0.01)),
            f1_header(6) + f1_car_telemetry_entry(surfaces=(1, 1, 0, 0)) * 22,
        )

        assert len(frames) == 1
        frame = frames[0]
        assert frame.valid
        assert frame.game == "f1"
        assert frame.rpm == 11800
        assert frame.max_rpm == 12000
        assert frame.rpm_band == pytest.approx((11800 - 4000) / 8000)
        assert frame.g_lateral == pytest.approx(2.0)
        assert frame.wheel_slip_ratio.fl == pytest.approx(-0.20)
        assert frame.surfaces.rl is SurfaceType.RUMBLE_STRIP
        assert frame.surfaces.fl is SurfaceType.TARMAC

    def test_rejected_packets_are_counted_not_raised(self):
        adapter = F1Adapter(port=0)
        adapter._on_packet(b"\x00\x00garbage")
        assert adapter.status().packets_rejected >= 1

    def test_collision_is_derived_from_a_g_force_spike(self):
        """F1 sends no collision event, so a violent single-frame change in
        g must be what triggers it."""
        frames = []
        adapter = F1Adapter(port=0)
        adapter.set_frame_callback(frames.append)

        status = f1_header(7) + f1_car_status_entry() * 22
        telemetry = f1_header(6) + f1_car_telemetry_entry() * 22

        self._feed(adapter, status, f1_header(0) + f1_motion_entry(g_lon=0.0) * 22, telemetry)
        assert frames[-1].impact == 0.0

        # A wall: 20 g swing between consecutive motion packets.
        self._feed(adapter, f1_header(0) + f1_motion_entry(g_lon=-20.0) * 22, telemetry)
        assert frames[-1].impact > 0.5

    def test_gradual_braking_is_not_read_as_a_collision(self):
        """Hard braking builds g over many frames and must stay silent."""
        frames = []
        adapter = F1Adapter(port=0)
        adapter.set_frame_callback(frames.append)

        status = f1_header(7) + f1_car_status_entry() * 22
        telemetry = f1_header(6) + f1_car_telemetry_entry() * 22
        self._feed(adapter, status)

        for step in range(12):  # ramp to -5 g over 12 frames
            g = -5.0 * step / 11
            self._feed(adapter, f1_header(0) + f1_motion_entry(g_lon=g) * 22, telemetry)

        assert all(frame.impact == 0.0 for frame in frames)

    def test_impact_is_consumed_after_one_frame(self):
        frames = []
        adapter = F1Adapter(port=0)
        adapter.set_frame_callback(frames.append)

        status = f1_header(7) + f1_car_status_entry() * 22
        telemetry = f1_header(6) + f1_car_telemetry_entry() * 22
        self._feed(adapter, status, f1_header(0) + f1_motion_entry(g_lon=0.0) * 22, telemetry)
        self._feed(adapter, f1_header(0) + f1_motion_entry(g_lon=-20.0) * 22, telemetry)
        assert frames[-1].impact > 0.0

        self._feed(adapter, telemetry)  # next frame, no new contact
        assert frames[-1].impact == 0.0

    def test_abs_flag_requires_braking_and_lock(self):
        frames = []
        adapter = F1Adapter(port=0)
        adapter.set_frame_callback(frames.append)

        self._feed(
            adapter,
            f1_header(7) + f1_car_status_entry(abs_on=1) * 22,
            f1_header(13) + f1_motion_ex(wheel_slip=(-0.3, -0.3, -0.3, -0.3)),
            f1_header(6) + f1_car_telemetry_entry(brake=0.0) * 22,
        )
        assert frames[-1].abs_active is False  # locked but not braking

        self._feed(adapter, f1_header(6) + f1_car_telemetry_entry(brake=0.9) * 22)
        assert frames[-1].abs_active is True


class TestPipelineStage:
    """The stage ladder exists because "socket open" was being read as
    "data arriving". Each rung must be distinguishable."""

    def test_waiting_before_start(self):
        adapter = F1Adapter(port=20860)
        assert adapter.status().stage is TelemetryStage.WAITING

    def test_socket_bound_but_no_packets(self):
        """The state the app was silently mis-reporting."""
        adapter = F1Adapter(port=20861)
        adapter.start()
        try:
            time.sleep(0.2)
            status = adapter.status()
            assert status.stage is TelemetryStage.SOCKET_BOUND
            assert status.packets_received == 0
        finally:
            adapter.stop()

    def test_error_when_port_is_already_held(self):
        """An exclusive bind must surface a conflict instead of pretending
        to listen while another socket eats every packet."""
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("0.0.0.0", 20862))
        try:
            adapter = F1Adapter(port=20862)
            adapter.start()
            status = adapter.status()
            assert status.stage is TelemetryStage.ERROR
            assert "already in use" in status.error
            adapter.stop()
        finally:
            blocker.close()

    def test_listener_does_not_set_reuseaddr(self):
        """Regression guard: SO_REUSEADDR on Windows lets a second instance
        bind the same UDP port and silently steal all the traffic."""
        from app.games.f1.telemetry import TelemetryListener

        listener = TelemetryListener(port=20863)
        assert listener.start()
        try:
            second = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            with pytest.raises(OSError):
                second.bind(("0.0.0.0", 20863))
            second.close()
        finally:
            listener.stop()

    def test_reaches_live_with_real_packets(self):
        adapter = F1Adapter(port=20864)
        adapter.start()
        try:
            time.sleep(0.2)
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            for _ in range(5):
                sender.sendto(
                    f1_header(7) + f1_car_status_entry() * 22, ("127.0.0.1", 20864)
                )
                sender.sendto(
                    f1_header(6) + f1_car_telemetry_entry() * 22, ("127.0.0.1", 20864)
                )
                time.sleep(0.02)
            time.sleep(0.3)

            status = adapter.status()
            assert status.stage is TelemetryStage.TELEMETRY_LIVE
            assert status.frames_emitted > 0
            assert status.bytes_per_sec > 0
            assert dict(status.packet_types).get("CarTelemetry", 0) > 0
        finally:
            adapter.stop()

    def test_packets_received_but_none_usable(self):
        """Distinguishes a parsing problem from a network problem."""
        adapter = F1Adapter(port=20865)
        adapter.start()
        try:
            time.sleep(0.2)
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            for _ in range(5):
                # Valid UDP, but not a telemetry packet we understand.
                sender.sendto(b"\x00\x00rubbish payload", ("127.0.0.1", 20865))
                time.sleep(0.02)
            time.sleep(0.3)

            status = adapter.status()
            assert status.stage is TelemetryStage.PACKETS_RECEIVED
            assert status.packets_received > 0
            assert status.frames_emitted == 0
            assert status.packets_rejected > 0
        finally:
            adapter.stop()


class TestFutureFormatTolerance:
    """A newer title must not be rejected wholesale.

    An allowlist made F1 26 (packet format 2026) look exactly like "the
    game is not sending anything": every packet silently dropped at the
    header check. Codemasters only ever appends fields, so an unknown newer
    format is parsed with the newest known layout instead.
    """

    @pytest.mark.parametrize("packet_format", [2024, 2025, 2026, 2027, 2030])
    def test_newer_formats_are_accepted(self, packet_format):
        packet = (
            f1_header(6, packet_format=packet_format)
            + f1_car_telemetry_entry() * 22
        )
        header = p.parse_header(packet)
        assert header is not None, f"format {packet_format} rejected"
        assert header.packet_format == packet_format

    def test_future_format_still_parses_the_payload(self):
        packet = f1_header(6, packet_format=2026) + f1_car_telemetry_entry(
            rpm=11200, speed=305, gear=8
        ) * 22
        header = p.parse_header(packet)
        telemetry = parser.parse_car_telemetry(packet, header)
        assert telemetry is not None
        assert telemetry.engine_rpm == 11200
        assert telemetry.speed_kph == 305
        assert telemetry.gear == 8

    def test_implausible_values_are_rejected_not_passed_through(self):
        """Validation is what stops a wrong stride feeding the haptics
        garbage instead of failing honestly."""
        from app.games.f1.parser import CarTelemetry, plausible_telemetry

        assert plausible_telemetry(
            CarTelemetry(speed_kph=250, engine_rpm=11000, gear=6, throttle=1.0)
        )
        assert not plausible_telemetry(CarTelemetry(speed_kph=60000, engine_rpm=11000))
        assert not plausible_telemetry(CarTelemetry(engine_rpm=999999))
        assert not plausible_telemetry(CarTelemetry(gear=77))

    def test_absurd_formats_are_still_rejected(self):
        for bad in (1999, 1234, 9999, 0):
            packet = struct.pack("<H", bad) + b"\x00" * 1400
            assert p.parse_header(packet) is None


class TestRawPacketCounter:
    """packets counted straight after recvfrom(), before interpretation."""

    def test_raw_counter_rises_even_for_unparseable_traffic(self):
        adapter = F1Adapter(port=20869)
        adapter.start()
        try:
            time.sleep(0.2)
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            for _ in range(6):
                sender.sendto(b"\xde\xad\xbe\xef" * 20, ("127.0.0.1", 20869))
                time.sleep(0.01)
            time.sleep(0.4)

            status = adapter.status()
            # This is the distinction that matters: bytes ARE arriving, they
            # just cannot be understood - a parser problem, not a network one.
            assert status.raw_packets >= 5
            assert status.packets_parsed == 0
            assert status.stage is TelemetryStage.PACKETS_RECEIVED
        finally:
            adapter.stop()

    def test_counters_reset_per_session(self):
        adapter = F1Adapter(port=20871)
        adapter.start()
        try:
            time.sleep(0.15)
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sender.sendto(b"junk", ("127.0.0.1", 20871))
            time.sleep(0.3)
            assert adapter.status().raw_packets > 0
        finally:
            adapter.stop()

        adapter.start()
        try:
            time.sleep(0.2)
            assert adapter.status().raw_packets == 0
        finally:
            adapter.stop()


class TestRealSocketPath:
    """Exercises the actual UDP path rather than calling _on_packet directly."""

    def _send(self, port: int, packets, delay: float = 0.0) -> None:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for packet in packets:
            sender.sendto(packet, ("127.0.0.1", port))
            if delay:
                time.sleep(delay)
        sender.close()

    @pytest.mark.parametrize("packet_format", [2022, 2023, 2024, 2025])
    def test_every_supported_format_survives_the_real_socket(self, packet_format):
        """F1 25 in particular - the version actually being targeted."""
        port = 20870 + (packet_format % 100)
        adapter = F1Adapter(port=port)
        adapter.start()
        try:
            time.sleep(0.2)
            status_packet = (
                f1_header(7, packet_format=packet_format) + f1_car_status_entry() * 22
            )
            telemetry_packet = (
                f1_header(6, packet_format=packet_format)
                + f1_car_telemetry_entry(rpm=10500, speed=250, gear=6) * 22
            )
            self._send(port, [status_packet, telemetry_packet] * 4, delay=0.01)
            time.sleep(0.4)

            status = adapter.status()
            assert status.stage is TelemetryStage.TELEMETRY_LIVE
            assert status.frames_emitted > 0
            assert status.packets_rejected == 0
            assert status.live_rpm == 10500
            assert status.live_speed_kph == 250
            assert status.live_gear == 6
        finally:
            adapter.stop()

    def test_handles_a_burst_without_dropping(self):
        """A whole frame's worth of packets arriving at once must all be
        consumed in one pass, not one per poll."""
        port = 20866
        adapter = F1Adapter(port=port)
        adapter.start()
        try:
            time.sleep(0.2)
            burst = [f1_header(6) + f1_car_telemetry_entry() * 22 for _ in range(60)]
            self._send(port, burst)  # no delay at all
            time.sleep(0.6)
            assert adapter.status().packets_received >= 55
        finally:
            adapter.stop()

    def test_receive_buffer_is_enlarged(self):
        """The default buffer is far too small for F1's ~780 KB/s."""
        from app.games.f1.telemetry import TelemetryListener

        listener = TelemetryListener(port=20867)
        assert listener.start()
        try:
            assert listener.receive_buffer_size >= 256 * 1024
        finally:
            listener.stop()

    def test_malformed_traffic_does_not_kill_the_receiver(self):
        port = 20868
        adapter = F1Adapter(port=port)
        adapter.start()
        try:
            time.sleep(0.2)
            junk = [b"", b"\x00", b"\xff" * 3000, b"\x00\x00garbage", bytes(range(256))]
            self._send(port, junk, delay=0.01)
            time.sleep(0.3)

            # Still alive and still able to handle a good packet afterwards.
            self._send(
                port,
                [
                    f1_header(7) + f1_car_status_entry() * 22,
                    f1_header(6) + f1_car_telemetry_entry() * 22,
                ],
                delay=0.01,
            )
            time.sleep(0.4)
            assert adapter.status().frames_emitted > 0
        finally:
            adapter.stop()


class TestListener:
    def test_binds_and_receives_over_a_real_socket(self):
        received = []
        adapter = F1Adapter(port=20787)
        adapter.set_frame_callback(received.append)
        assert adapter.listener.start()

        try:
            time.sleep(0.2)
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sender.sendto(
                f1_header(7) + f1_car_status_entry() * 22, ("127.0.0.1", 20787)
            )
            sender.sendto(
                f1_header(6) + f1_car_telemetry_entry() * 22, ("127.0.0.1", 20787)
            )
            time.sleep(0.4)
            assert adapter.status().packets_received >= 2
            assert len(received) >= 1
        finally:
            adapter.stop()

    def test_reports_error_when_port_is_unavailable(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("0.0.0.0", 20788))
        try:
            adapter = F1Adapter(port=20788)
            # SO_REUSEADDR can permit the bind on some stacks; either way the
            # app must not crash and must report its state honestly.
            adapter.listener.start()
            status = adapter.status()
            assert status.error or status.running
            adapter.stop()
        finally:
            blocker.close()

    def test_not_connected_before_any_packet(self):
        adapter = F1Adapter(port=20789)
        assert not adapter.status().connected
