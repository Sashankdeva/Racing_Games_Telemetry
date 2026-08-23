"""Regression tests for telemetry health reporting.

Two real defects motivated these:

  * The port scanner bound every candidate port exclusively, for the whole
    session. That denied those ports to a second copy of the app and to any
    other telemetry tool, and if the game had been sending to one of them
    the scanner would have been the process eating the packets.
  * "Packet rate" was the only rate shown, so a healthy 60 Hz feed read as
    ~420/s and looked like a fault. The rate that actually says whether the
    dashboard updates is frames/s.
"""

from __future__ import annotations

import socket
import time

import pytest

from app.games.f1.telemetry import _PortScanner


def _free(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


@pytest.fixture
def candidates(monkeypatch):
    """A private port block, so tests never fight a real game or app."""
    ports = (20860, 20861, 20862)
    monkeypatch.setattr("app.games.f1.telemetry.CANDIDATE_PORTS", ports)
    return ports


class TestPortScanner:
    def test_does_not_bind_while_telemetry_is_healthy(self, candidates):
        """The common case: our port works, so nothing else is touched."""
        scanner = _PortScanner(
            exclude=candidates[0], on_found=lambda p: None, is_needed=lambda: False
        )
        scanner.START_DELAY = 0.05
        scanner.start()
        try:
            time.sleep(0.4)
            for port in candidates[1:]:
                assert _free(port), f"port {port} was bound despite healthy telemetry"
        finally:
            scanner.stop()

    def test_waits_before_borrowing_any_port(self, candidates):
        """A grace period means a working setup never binds these at all."""
        scanner = _PortScanner(
            exclude=candidates[0], on_found=lambda p: None, is_needed=lambda: True
        )
        scanner.START_DELAY = 5.0
        scanner.start()
        try:
            time.sleep(0.3)  # still inside the grace period
            assert _free(candidates[1])
        finally:
            scanner.stop()

    def test_releases_ports_when_telemetry_arrives(self, candidates):
        """The scanner must let go the moment the real port comes alive."""
        needed = True
        scanner = _PortScanner(
            exclude=candidates[0], on_found=lambda p: None, is_needed=lambda: needed
        )
        scanner.START_DELAY = 0.05
        scanner.start()
        try:
            time.sleep(0.4)
            assert not _free(candidates[1]), "scanner never started scanning"

            needed = False  # telemetry just arrived on the real port
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not _free(candidates[1]):
                time.sleep(0.05)
            assert _free(candidates[1]), "scanner kept holding the port"
        finally:
            scanner.stop()

    def test_stop_releases_everything(self, candidates):
        scanner = _PortScanner(
            exclude=candidates[0], on_found=lambda p: None, is_needed=lambda: True
        )
        scanner.START_DELAY = 0.05
        scanner.start()
        time.sleep(0.4)
        scanner.stop()
        for port in candidates[1:]:
            assert _free(port)


class TestCliOverridesDoNotPersist:
    """A one-off `--port`/`--mode` must not become the saved configuration.

    It did: shutdown() saved unconditionally, so `--selftest --port 20807`
    permanently repointed the app at a port no game was sending to - which
    then looks exactly like broken telemetry.
    """

    def _saved_port(self, mode):
        from app.config.mode_settings import ModeSettings

        return ModeSettings.load(mode).udp_port

    def test_port_override_is_not_written_on_exit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path))
        from app.config.settings import AppSettings
        from app.core.application import Application
        from app.games.modes import GameMode

        baseline = Application(AppSettings(game_mode="f1_25"))
        baseline.mode_settings.udp_port = 20777
        baseline.save_mode_settings()
        baseline.shutdown()
        assert self._saved_port(GameMode.F1_25) == 20777

        override = Application(AppSettings(game_mode="f1_25"))
        override.mode_settings.udp_port = 20899  # as --port would set it
        override.persist_on_exit = False
        override.shutdown()

        assert self._saved_port(GameMode.F1_25) == 20777, (
            "a session override leaked into the saved settings"
        )

    def test_normal_runs_still_persist(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path))
        from app.config.settings import AppSettings
        from app.core.application import Application
        from app.games.modes import GameMode

        app = Application(AppSettings(game_mode="f1_25"))
        app.mode_settings.udp_port = 20788
        assert app.persist_on_exit is True
        app.shutdown()

        assert self._saved_port(GameMode.F1_25) == 20788

    def test_selftest_and_overrides_disable_persistence(self):
        """The wiring in main.py, not just the flag itself.

        Goes through the real argument parser rather than a hand-built
        Namespace, so adding a flag cannot silently break this test - or
        hide a genuine regression behind an AttributeError.
        """
        from app.main import _build_application as build_application
        from app.main import _parse_args

        for argv in (["--port", "20800"], ["--mode", "f1_26"], ["--selftest"]):
            app = build_application(_parse_args(argv))
            try:
                assert app.persist_on_exit is False, f"{argv} still persists"
            finally:
                app.shutdown()

        app = build_application(_parse_args([]))
        try:
            assert app.persist_on_exit is True
        finally:
            app.persist_on_exit = False  # do not touch real settings in tests
            app.shutdown()


class TestFieldMovement:
    """A snapshot of one frame cannot tell 'throttle is broken' apart from
    'you were coasting at that instant'. Movement over a sample can, and it
    is what localises a field read at the wrong offset."""

    def _feed(self, probe, throttle_values):
        import struct

        for index, throttle in enumerate(throttle_values):
            header = struct.pack(
                "<HBBBBBQfIIBB", 2025, 25, 1, 0, 1, 6, 1, float(index), index, index, 0, 255
            )
            entry = struct.pack(
                "<HfffBbHBBH4H4B4BH4f4B",
                100 + index, throttle, 0.0, index / 100.0, 0, 4, 9000, 0, 50, 0,
                *[400] * 4, *[90] * 4, *[95] * 4, 110, *[23.0] * 4, *[0] * 4,
            )
            probe._adapter._on_packet(header + entry * 22 + struct.pack("<BBb", 0, 0, 4))

    def test_frozen_field_reads_static_while_neighbours_move(self):
        from app.diagnostics.telemetry_probe import TelemetryProbe

        probe = TelemetryProbe(port=20878)
        self._feed(probe, [0.0] * 40)  # throttle pinned, brake ramping

        movement = probe.stats.field_movement
        assert len(movement["throttle"][3]) == 1, "frozen throttle should show one value"
        assert len(movement["brake"][3]) > 2, "ramping brake should show movement"

    def test_varying_field_reads_as_moving(self):
        from app.diagnostics.telemetry_probe import TelemetryProbe

        probe = TelemetryProbe(port=20879)
        self._feed(probe, [i / 40.0 for i in range(40)])

        assert len(probe.stats.field_movement["throttle"][3]) > 2

    def test_absent_packet_type_is_reported_not_guessed(self):
        """No LapData sent -> its fields must not appear as real values."""
        from app.diagnostics.telemetry_probe import TelemetryProbe, format_report

        probe = TelemetryProbe(port=20880)
        probe.stats.bound = True
        probe.stats.packets = 100
        probe.stats.first_packet_time = 1.0
        probe.stats.last_packet_time = 2.0
        self._feed(probe, [i / 40.0 for i in range(40)])

        report = format_report(probe.stats)
        assert "[6] FIELD MOVEMENT" in report
        lap_line = [ln for ln in report.splitlines() if "current_lap" in ln][0]
        assert "STATIC" in lap_line or "ABSENT" in lap_line


class TestRatesAreDistinct:
    """Packets/s and frames/s measure different things and must not be
    conflated - that conflation is what made 420/s look like a fault."""

    def test_adapter_reports_both(self):
        from app.games.f1.adapter import F1Adapter

        adapter = F1Adapter(port=20863)
        status = adapter.status()
        assert hasattr(status, "packet_rate")
        assert hasattr(status, "frame_rate")

    def test_frame_rate_counts_frames_not_packets(self, monkeypatch):
        """Feed a mixed packet stream: frames/s must track only the
        car-telemetry packets, not the total."""
        import struct

        from app.games.f1.adapter import F1Adapter
        from tests.conftest import (
            f1_car_status_entry,
            f1_car_telemetry_entry,
            f1_header,
        )

        adapter = F1Adapter(port=20864)

        telemetry_pkt = (
            f1_header(6, packet_format=2025)
            + f1_car_telemetry_entry() * 22
            + struct.pack("<BBb", 0, 0, 7)
        )
        status_pkt = f1_header(7, packet_format=2025) + f1_car_status_entry() * 22

        # 1 telemetry packet per 4 others - the shape of a real F1 feed.
        for _ in range(10):
            adapter.feed(telemetry_pkt)
            for _ in range(4):
                adapter.feed(status_pkt)

        result = adapter.status()
        # 50 packets in, but only the 10 car-telemetry ones make a frame.
        assert result.raw_packets == 50
        assert result.frames_emitted == 10
        assert result.frame_rate > 0
        # The rate tracker counted frames, not datagrams: at 5 packets per
        # frame, reading packets as the update rate overstates it 5x.
        assert result.frame_rate == pytest.approx(10 / 2.0, abs=0.01)
