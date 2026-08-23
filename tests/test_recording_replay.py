"""Recording, replay and the telemetry inspector.

The headline test is `test_replay_reproduces_live_frames_exactly`: replay
must feed the same adapter and produce identical normalized frames, or it
is a simulation rather than a reproduction and is worthless for debugging.
"""

from __future__ import annotations

import socket
import struct
import time

import pytest

from app.config.settings import AppSettings
from app.core.application import Application
from app.telemetry.recording import (
    Recorder,
    RecordingMeta,
    list_recordings,
    read_meta,
    read_packets,
)
from app.telemetry.replay import ReplayPlayer
from tests.conftest import f1_car_status_entry, f1_car_telemetry_entry

PORT = 21011


def hdr(packet_id: int, fmt: int = 2026) -> bytes:
    return struct.pack(
        "<HBBBBBQfIIBB", fmt, 26, 1, 0, 1, packet_id, 999, 1.5, 100, 100, 0, 255
    )


LAP = struct.pack(
    "<IIHBHBHHfffBBBBBBB",
    91234, 45000, 28100, 0, 31500, 0, 1200, 5400, 1500.0, 60000.0, 0.0,
    4, 23, 0, 1, 1, 0, 0,
) + b"\x00" * 13
SESSION = struct.pack("<BbbBHBbBHH", 2, 41, 27, 58, 5300, 15, 1, 0, 1800, 3600) + b"\x00" * 600
DAMAGE = struct.pack(
    "<4f4B4B" + "B" * 18, 11.0, 12.0, 13.0, 14.0, *([0] * 8),
    5, 8, 3, 2, 1, 4, 0, 0, 6, 9, *([0] * 8),
)


def car(rpm=9000, speed=200, gear=5):
    return hdr(6) + f1_car_telemetry_entry(
        speed=speed, rpm=rpm, gear=gear, throttle=0.8, brake=0.1
    ) * 22


class TestRecordingFormat:
    def test_roundtrip(self, tmp_path):
        path = tmp_path / "test.f1re"
        recorder = Recorder(path, RecordingMeta(game_mode="f1_26"))
        assert recorder.start()

        payloads = [car(rpm=r) for r in (7000, 8000, 9000)]
        for payload in payloads:
            recorder.write(payload, packet_format=2026, packet_id=6)
        meta = recorder.stop()

        assert meta.packet_count == 3
        assert meta.formats_seen == {"2026": 3}

        read_back = [data for _, data in read_packets(path)]
        assert read_back == payloads, "recorded bytes must be byte-identical"

    def test_header_survives_and_is_readable(self, tmp_path):
        path = tmp_path / "meta.f1re"
        recorder = Recorder(path, RecordingMeta(game_mode="f1_26", note="hello"))
        recorder.start()
        recorder.write(car(), packet_format=2026, packet_id=6)
        recorder.stop()

        meta = read_meta(path)
        assert meta is not None
        assert meta.game_mode == "f1_26"
        assert meta.note == "hello"
        assert meta.packet_count == 1

    def test_truncated_file_stops_cleanly(self, tmp_path):
        path = tmp_path / "cut.f1re"
        recorder = Recorder(path, RecordingMeta())
        recorder.start()
        for _ in range(5):
            recorder.write(car(), packet_format=2026, packet_id=6)
        recorder.stop()

        # Simulate a crash mid-write.
        data = path.read_bytes()
        path.write_bytes(data[: len(data) - 40])

        packets = list(read_packets(path))
        assert 0 < len(packets) < 5  # partial, not an exception

    def test_non_recording_file_is_rejected(self, tmp_path):
        path = tmp_path / "junk.f1re"
        path.write_bytes(b"definitely not a recording")
        assert read_meta(path) is None
        assert list(read_packets(path)) == []

    def test_oversized_packets_are_refused(self, tmp_path):
        recorder = Recorder(tmp_path / "big.f1re", RecordingMeta())
        recorder.start()
        recorder.write(b"x" * 70000, packet_format=2026, packet_id=6)
        assert recorder.stop().packet_count == 0

    def test_listing_finds_recordings(self, tmp_path):
        for index in range(2):
            recorder = Recorder(tmp_path / f"r{index}.f1re", RecordingMeta())
            recorder.start()
            recorder.write(car(), packet_format=2026, packet_id=6)
            recorder.stop()
        assert len(list_recordings(tmp_path)) == 2


class TestReplayPlayer:
    def _recording(self, tmp_path, count=6):
        path = tmp_path / "replay.f1re"
        recorder = Recorder(path, RecordingMeta())
        recorder.start()
        for index in range(count):
            recorder.write(car(rpm=6000 + index * 500), packet_format=2026, packet_id=6)
        recorder.stop()
        return path

    def test_step_emits_packets_in_order(self, tmp_path):
        received = []
        player = ReplayPlayer(self._recording(tmp_path), received.append)
        assert player.load()

        assert player.step(3) == 3
        assert len(received) == 3
        assert player.step(99) == 3  # only 3 remain
        assert player.step() == 0  # exhausted

    def test_sink_failure_does_not_stop_replay(self, tmp_path):
        calls = []

        def bad_sink(data):
            calls.append(data)
            raise RuntimeError("consumer exploded")

        player = ReplayPlayer(self._recording(tmp_path, 4), bad_sink)
        player.load()
        assert player.step(4) == 4
        assert len(calls) == 4

    def test_missing_file_fails_gracefully(self, tmp_path):
        player = ReplayPlayer(tmp_path / "nope.f1re", lambda d: None)
        assert not player.load()
        assert not player.start()


class TestLiveToReplayRoundTrip:
    """The property that makes replay useful for debugging at all."""

    @pytest.fixture
    def app(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path))
        instance = Application(AppSettings())
        instance.mode_settings.udp_port = PORT
        instance._configure_adapter()
        instance.startup()
        time.sleep(0.3)
        yield instance
        instance.shutdown()

    def test_replay_reproduces_live_frames_exactly(self, app):
        path = app.start_recording(note="round trip")
        assert path is not None

        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        live = []
        for index in range(12):
            for packet in (
                hdr(1) + SESSION,
                hdr(7) + f1_car_status_entry(max_rpm=12000, idle_rpm=4000) * 22,
                hdr(2) + LAP * 22,
                hdr(10) + DAMAGE * 22,
                car(rpm=6000 + index * 300, speed=100 + index * 8, gear=2 + index % 6),
            ):
                sender.sendto(packet, ("127.0.0.1", PORT))
            time.sleep(0.02)
            frame = app.telemetry.snapshot().frame
            if frame.valid:
                live.append((round(frame.rpm), round(frame.speed_kph), frame.gear))
        time.sleep(0.3)

        meta = app.stop_recording()
        assert meta.packet_count > 0
        app.stop_telemetry()

        player = app.load_replay(path)
        assert player is not None

        replayed = []
        while player.step(5):
            frame = app.telemetry.snapshot().frame
            if frame.valid:
                replayed.append((round(frame.rpm), round(frame.speed_kph), frame.gear))

        assert live, "no live frames captured"
        assert replayed, "replay produced no frames"
        # Replay must visit the same states; sampling differs so compare sets.
        assert set(live).issubset(set(replayed))

    def test_replay_goes_through_the_real_adapter(self, app):
        """No parallel parsing path - the stage ladder must advance."""
        path = app.start_recording()
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(hdr(7) + f1_car_status_entry() * 22, ("127.0.0.1", PORT))
        sender.sendto(car(), ("127.0.0.1", PORT))
        time.sleep(0.3)
        app.stop_recording()
        app.stop_telemetry()

        player = app.load_replay(path)
        player.step(10)

        status = app.report().adapter
        assert status.frames_emitted > 0
        assert status.packets_rejected == 0
        assert int(status.stage) == 6

    def test_starting_live_stops_replay(self, app):
        """Two sources feeding one state would be unreasonable to debug."""
        path = app.start_recording()
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(car(), ("127.0.0.1", PORT))
        time.sleep(0.25)
        app.stop_recording()

        app.load_replay(path)
        assert app.replay is not None
        app.start_telemetry()
        assert app.replay is None


class TestInspector:
    def test_flags_a_static_field(self):
        """The failure mode that bit us before: a plausible constant."""
        from app.core.models import TelemetryFrame
        from app.telemetry.inspector import TelemetryInspector

        inspector = TelemetryInspector()
        for index in range(20):
            inspector.observe_frame(
                TelemetryFrame(
                    valid=True,
                    rpm=6000 + index * 100,  # changing
                    speed_kph=180.0,  # pinned
                    gear=0,  # never set
                )
            )

        by_key = {stat.key: stat for stat in inspector.field_stats()}
        assert by_key["rpm"].verdict == "OK"
        assert by_key["speed_kph"].verdict == "STATIC"
        assert by_key["gear"].verdict == "ABSENT"

    def test_reports_no_data_before_any_frame(self):
        from app.telemetry.inspector import TelemetryInspector

        inspector = TelemetryInspector()
        assert all(stat.verdict == "NO DATA" for stat in inspector.field_stats())

    def test_counts_packets_by_type_and_format(self):
        from app.telemetry.inspector import TelemetryInspector

        inspector = TelemetryInspector()
        inspector.observe_packet(car())
        inspector.observe_packet(car())
        inspector.observe_packet(hdr(7) + f1_car_status_entry() * 22)

        stats = {stat.name: stat for stat in inspector.packet_stats()}
        assert stats["CarTelemetry"].count == 2
        assert stats["CarStatus"].count == 1
        assert inspector.formats_seen()[2026] == 3

    def test_unparseable_packets_are_counted_with_evidence(self):
        from app.telemetry.inspector import TelemetryInspector

        inspector = TelemetryInspector()
        inspector.observe_packet(b"\xde\xad\xbe\xef" * 8)
        assert inspector.unparseable == 1
        assert inspector.last_bad_packet.startswith(b"\xde\xad")

    def test_problem_fields_lists_only_the_bad_ones(self):
        from app.core.models import TelemetryFrame
        from app.telemetry.inspector import TelemetryInspector

        inspector = TelemetryInspector()
        for index in range(5):
            inspector.observe_frame(TelemetryFrame(valid=True, rpm=5000 + index))

        problems = {stat.key for stat in inspector.problem_fields()}
        assert "rpm" not in problems
        assert "speed_kph" in problems
