"""F1 adapter: native packets -> normalized TelemetryFrame.

Telemetry arrives split across several packet types at different rates, so
this class keeps a running picture of the car and emits a complete frame
whenever the car-telemetry packet lands (the fastest and most useful one).

Collision handling deserves a note. The F1 UDP spec has no collision event,
so contact is *derived* rather than read:

  * primary signal - a large single-frame jump in combined lateral and
    longitudinal g. Hard braking builds g over many frames; hitting a wall
    changes it violently in one, so jerk separates the two cleanly.
  * confirmation - an increase in wing or floor damage. This packet is slow
    (a few Hz) so it cannot be the trigger, but it reliably raises
    confidence that real contact happened.

Vertical g is deliberately excluded: kerbs and crests spike it constantly
and they already have their own effects.
"""

from __future__ import annotations

import math
import struct
import threading
import time

from app.core.logging import RateLimitedLogger, get_logger
from app.core.models import Surfaces, TelemetryFrame, Wheels
from app.games.base import AdapterStatus, GameAdapter, TelemetryStage
from app.games.f1 import packets as p
from app.games.f1 import parser
from app.games.f1.telemetry import DEFAULT_PORT, TelemetryListener

#: Packet id -> readable name, surfaced in Diagnostics.
PACKET_NAMES = {
    p.PACKET_MOTION: "Motion",
    p.PACKET_SESSION: "Session",
    p.PACKET_LAP_DATA: "LapData",
    p.PACKET_EVENT: "Event",
    p.PACKET_PARTICIPANTS: "Participants",
    p.PACKET_CAR_SETUPS: "CarSetups",
    p.PACKET_CAR_TELEMETRY: "CarTelemetry",
    p.PACKET_CAR_STATUS: "CarStatus",
    p.PACKET_FINAL_CLASSIFICATION: "FinalClassification",
    p.PACKET_LOBBY_INFO: "LobbyInfo",
    p.PACKET_CAR_DAMAGE: "CarDamage",
    p.PACKET_SESSION_HISTORY: "SessionHistory",
    p.PACKET_TYRE_SETS: "TyreSets",
    p.PACKET_MOTION_EX: "MotionEx",
}

_log = get_logger(__name__)
_rate_log = RateLimitedLogger(_log)

#: Single-frame g change at which contact starts registering.
IMPACT_JERK_MIN = 3.0
#: Single-frame g change treated as a maximum-severity impact.
IMPACT_JERK_MAX = 14.0
#: Impact level contributed by fresh bodywork damage.
DAMAGE_IMPACT = 0.75
#: Slip below this (wheel slower than car) counts as ABS intervention.
ABS_SLIP_THRESHOLD = -0.08


class F1Adapter(GameAdapter):
    game_id = "f1"
    display_name = "F1 (Codemasters / EA)"
    supported = True
    description = (
        "UDP telemetry for F1 22, 23, 24 and 25. Enable telemetry in "
        "Settings > Telemetry Settings in-game and match the UDP port."
    )

    def __init__(self, port: int = DEFAULT_PORT) -> None:
        super().__init__()
        self.listener = TelemetryListener(port=port)
        self.listener.set_callback(self._on_packet)

        self._lock = threading.Lock()
        self._reset_state()

        self._packets_rejected = 0
        self._packet_format = 0
        self._frames_emitted = 0
        self._packets_parsed = 0
        self._warned_format = False
        self._packet_counts: dict[int, int] = {}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def configure(self, **options) -> None:
        port = options.get("port")
        if port is not None:
            self.listener.set_port(int(port))
        timeout = options.get("connection_timeout")
        if timeout is not None:
            self.listener.connection_timeout = float(timeout)

    def start(self) -> None:
        self._reset_state()
        self.listener.start()

    def stop(self) -> None:
        self.listener.stop()
        self._reset_state()

    def status(self) -> AdapterStatus:
        age = self.listener.last_packet_age
        detail = ""
        if self._packet_format:
            detail = f"Packet format {self._packet_format}"
        elif self.listener.running:
            detail = "Waiting for telemetry"
        return AdapterStatus(
            game_id=self.game_id,
            display_name=self.display_name,
            running=self.listener.running,
            connected=self.listener.connected,
            packets_received=self.listener.packets_received,
            packets_rejected=self._packets_rejected,
            packet_rate=self.listener.packet_rate,
            last_packet_age=0.0 if math.isinf(age) else age,
            detail=detail,
            error=self.listener.error,
            stage=self._stage(),
            bytes_per_sec=self.listener.bytes_per_sec,
            raw_packets=self.listener.raw_packets,
            packets_parsed=self._packets_parsed,
            frames_emitted=self._frames_emitted,
            packet_types=self._packet_type_summary(),
            last_sender=self.listener.last_sender,
            detected_port=self.listener.detected_port,
            receive_buffer_kb=self.listener.receive_buffer_size // 1024,
            **self._live_values(),
            **self._raw_values(),
        )

    def _live_values(self) -> dict[str, float | int]:
        """The most recent normalized frame, for display.

        Read straight from the adapter rather than from the engine, so the
        UI can show real changing telemetry even if the haptic engine is
        stopped, muted, or the emergency stop is latched.
        """
        with self._lock:
            frame = self._last_frame
        if frame is None:
            return dict(
                live_rpm=0.0, live_max_rpm=0.0, live_speed_kph=0.0,
                live_gear=0, live_throttle=0.0, live_brake=0.0,
            )
        return dict(
            live_rpm=frame.rpm,
            live_max_rpm=frame.max_rpm,
            live_speed_kph=frame.speed_kph,
            live_gear=frame.gear,
            live_throttle=frame.throttle,
            live_brake=frame.brake,
        )

    def _raw_values(self) -> dict:
        """Values exactly as decoded from the packet, plus the gear trace.

        Reported separately from the normalized ones so a discrepancy
        immediately says whether the parser or the adapter is at fault.
        """
        with self._lock:
            raw = self._telemetry
            status = self._status
            return dict(
                raw_rpm=raw.engine_rpm if raw else 0.0,
                raw_speed_kph=raw.speed_kph if raw else 0.0,
                raw_gear=raw.gear if raw else 0,
                raw_throttle=raw.throttle if raw else 0.0,
                raw_brake=raw.brake if raw else 0.0,
                raw_max_rpm=status.max_rpm if status else 0.0,
                player_car_index=self._player_index,
                prev_gear=self._prev_gear,
                current_gear=self._current_gear,
                last_shift=self._last_shift,
                shift_count=self._shift_count,
            )

    def _stage(self) -> TelemetryStage:
        """Where the pipeline has actually reached.

        Each rung distinguishes a different class of problem, so the UI can
        say "socket is open but nothing is arriving" (a game-configuration
        issue) rather than the ambiguous "waiting".
        """
        if self.listener.error:
            return TelemetryStage.ERROR
        if not self.listener.running:
            return TelemetryStage.WAITING
        if self.listener.raw_packets == 0:
            return TelemetryStage.SOCKET_BOUND
        if self._packets_parsed == 0:
            return TelemetryStage.PACKETS_RECEIVED
        if self._frames_emitted == 0:
            return TelemetryStage.PACKETS_PARSED
        if not self.listener.connected:
            return TelemetryStage.TELEMETRY_VALID
        return TelemetryStage.TELEMETRY_LIVE

    def _packet_type_summary(self) -> tuple[tuple[str, int], ...]:
        with self._lock:
            counts = dict(self._packet_counts)
        return tuple(
            (PACKET_NAMES.get(pid, f"id {pid}"), count)
            for pid, count in sorted(counts.items())
        )

    @property
    def port(self) -> int:
        return self.listener.port

    def _reset_state(self) -> None:
        with self._lock:
            self._telemetry: parser.CarTelemetry | None = None
            self._status: parser.CarStatus | None = None
            self._motion: parser.MotionData | None = None
            self._motion_ex: parser.MotionExtended | None = None
            self._pit_status = 0
            self._damage_total: int | None = None
            self._prev_g_lat = 0.0
            self._prev_g_lon = 0.0
            self._g_primed = False
            self._pending_impact = 0.0
            self._frames_emitted = 0
            self._packets_parsed = 0
            self._warned_format = False
            self._packet_counts = {}
            self._last_frame = None
            self._player_index = 0
            self._prev_gear = 0
            self._current_gear = 0
            self._last_shift = "none"
            self._shift_count = 0

    # ------------------------------------------------------------------
    # packet handling
    # ------------------------------------------------------------------
    def _on_packet(self, data: bytes) -> None:
        header = p.parse_header(data)
        if header is None:
            self._packets_rejected += 1
            # Dump the leading bytes of the first few rejects. Without this a
            # version mismatch is indistinguishable from silence, which is
            # exactly how an unsupported packet format hides.
            if self._packets_rejected <= 3:
                claimed = (
                    struct.unpack_from("<H", data, 0)[0] if len(data) >= 2 else -1
                )
                _log.warning(
                    "Unparseable packet: %d bytes, claimed format %d, first 16 bytes %s",
                    len(data), claimed, data[:16].hex(" "),
                )
            _rate_log.debug(
                "bad_header", "Rejected packet with unrecognised header (%d bytes)", len(data)
            )
            return

        # Count anything whose header decoded; payload success is tracked
        # separately by the individual parse steps.
        self._packets_parsed += 1
        if header.packet_format not in p.KNOWN_FORMATS and not self._warned_format:
            self._warned_format = True
            _log.warning(
                "Packet format %d is newer than any verified layout; parsing "
                "with the %d layout and ignoring trailing fields.",
                header.packet_format, max(p.KNOWN_FORMATS),
            )

        self._packet_format = header.packet_format
        self._player_index = header.player_car_index

        packet_id = header.packet_id
        with self._lock:
            self._packet_counts[packet_id] = self._packet_counts.get(packet_id, 0) + 1

        if packet_id == p.PACKET_CAR_TELEMETRY:
            self._handle_car_telemetry(data, header)
        elif packet_id == p.PACKET_CAR_STATUS:
            self._store("_status", parser.parse_car_status(data, header))
        elif packet_id == p.PACKET_MOTION:
            self._handle_motion(data, header)
        elif packet_id == p.PACKET_MOTION_EX:
            self._store("_motion_ex", parser.parse_motion_extended(data, header))
        elif packet_id == p.PACKET_LAP_DATA:
            pit = parser.parse_pit_status(data, header)
            if pit is not None:
                with self._lock:
                    self._pit_status = pit
        elif packet_id == p.PACKET_CAR_DAMAGE:
            self._handle_damage(data, header)
        # Other packet ids carry no haptic value and are ignored by design.

    def _store(self, attribute: str, value) -> None:
        if value is None:
            self._packets_rejected += 1
            return
        with self._lock:
            setattr(self, attribute, value)

    def _handle_motion(self, data: bytes, header: p.PacketHeader) -> None:
        motion = parser.parse_motion(data, header)
        if motion is None:
            self._packets_rejected += 1
            return

        with self._lock:
            if self._g_primed:
                delta = math.hypot(
                    motion.g_lateral - self._prev_g_lat,
                    motion.g_longitudinal - self._prev_g_lon,
                )
                if delta > IMPACT_JERK_MIN:
                    severity = (delta - IMPACT_JERK_MIN) / (IMPACT_JERK_MAX - IMPACT_JERK_MIN)
                    self._pending_impact = max(self._pending_impact, min(1.0, severity))
            else:
                self._g_primed = True

            self._prev_g_lat = motion.g_lateral
            self._prev_g_lon = motion.g_longitudinal
            self._motion = motion

        # 2022 keeps suspension data in the tail of this same packet.
        if header.is_legacy_layout:
            self._store("_motion_ex", parser.parse_motion_extended(data, header))

    def _handle_damage(self, data: bytes, header: p.PacketHeader) -> None:
        damage = parser.parse_car_damage(data, header)
        if damage is None:
            self._packets_rejected += 1
            return
        with self._lock:
            previous = self._damage_total
            total = damage.total
            if previous is not None and total > previous:
                self._pending_impact = max(self._pending_impact, DAMAGE_IMPACT)
            self._damage_total = total

    # ------------------------------------------------------------------
    # frame assembly
    # ------------------------------------------------------------------
    def _handle_car_telemetry(self, data: bytes, header: p.PacketHeader) -> None:
        telemetry = parser.parse_car_telemetry(data, header)
        if telemetry is None:
            self._packets_rejected += 1
            return

        with self._lock:
            # Adapter-level gear trace, independent of the haptic effect, so
            # "is the game even reporting gear changes" can be answered
            # without involving the effect at all.
            if self._current_gear != telemetry.gear:
                self._prev_gear = self._current_gear
                self._current_gear = telemetry.gear
                if self._prev_gear > 0 and telemetry.gear > 0:
                    self._last_shift = (
                        "UPSHIFT" if telemetry.gear > self._prev_gear else "DOWNSHIFT"
                    )
                    self._last_shift += f" {self._prev_gear}->{telemetry.gear}"
                    self._shift_count += 1

            self._telemetry = telemetry
            frame = self._build_frame()
            # Impact is a one-shot impulse: consume it so a single contact
            # cannot retrigger the collision effect on later frames.
            self._pending_impact = 0.0
            self._frames_emitted += 1
            self._last_frame = frame

        self._emit(frame)

    def _build_frame(self) -> TelemetryFrame:
        """Assemble a frame. Caller must hold the lock."""
        telemetry = self._telemetry
        status = self._status
        motion = self._motion
        motion_ex = self._motion_ex

        assert telemetry is not None  # only called from _handle_car_telemetry

        surfaces = (
            Surfaces(*telemetry.surfaces)
            if len(telemetry.surfaces) == 4
            else Surfaces()
        )

        max_rpm = status.max_rpm if status else 0.0
        idle_rpm = status.idle_rpm if status else 0.0
        rpm_ratio = telemetry.engine_rpm / max_rpm if max_rpm > 0 else 0.0

        slip = motion_ex.wheel_slip if motion_ex else Wheels()
        abs_engaging = (
            bool(status and status.anti_lock_brakes)
            and telemetry.brake > 0.2
            and min(slip.as_tuple()) < ABS_SLIP_THRESHOLD
        )

        return TelemetryFrame(
            timestamp=time.perf_counter(),
            game=self.game_id,
            valid=True,
            paused=False,
            in_pits=self._pit_status != 0,
            speed_kph=telemetry.speed_kph,
            rpm=telemetry.engine_rpm,
            max_rpm=max_rpm,
            idle_rpm=idle_rpm,
            gear=telemetry.gear,
            throttle=telemetry.throttle,
            brake=telemetry.brake,
            clutch=telemetry.clutch,
            steering=telemetry.steer,
            drs_active=telemetry.drs,
            wheel_speed=motion_ex.wheel_speed if motion_ex else Wheels(),
            wheel_slip_ratio=slip,
            suspension_position=motion_ex.suspension_position if motion_ex else Wheels(),
            suspension_velocity=motion_ex.suspension_velocity if motion_ex else Wheels(),
            suspension_acceleration=(
                motion_ex.suspension_acceleration if motion_ex else Wheels()
            ),
            surfaces=surfaces,
            g_lateral=motion.g_lateral if motion else 0.0,
            g_longitudinal=motion.g_longitudinal if motion else 0.0,
            g_vertical=motion.g_vertical if motion else 0.0,
            abs_active=abs_engaging,
            tc_active=bool(status.traction_control) if status else None,
            rev_limiter_active=(
                telemetry.rev_lights_percent >= 100
                and rpm_ratio >= 0.99
                and telemetry.throttle > 0.5
            ),
            impact=self._pending_impact,
        )
