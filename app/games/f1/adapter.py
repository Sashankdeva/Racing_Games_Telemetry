"""F1 adapter: native packets -> normalized TelemetryFrame.

Telemetry arrives split across several packet types at different rates, so
this class keeps a running picture of the car and emits a complete frame
whenever the car-telemetry packet lands (the fastest and most useful one).

Contact detection deserves a note. The F1 UDP spec has no collision event,
so contact is *derived* rather than read: a large single-frame jump in
combined lateral and longitudinal g, confirmed by an increase in wing or
floor damage. Vertical g is excluded because kerbs and crests spike it
constantly. The result is exposed as `impact` and clearly labelled as
derived rather than measured.
"""

from __future__ import annotations

import math
import struct
import threading
import time

from app.core.logging import RateLimitedLogger, get_logger
from app.core.models import Surfaces, TelemetryFrame, Wheels
from app.games.base import AdapterStatus, GameAdapter, RateTracker, TelemetryStage
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
        # Frames/s, tracked separately from packets/s. The two differ by
        # roughly 7x because F1 sends several packet types per tick, and
        # conflating them makes a healthy feed look alarming.
        self._frame_rate = RateTracker()
        self._reset_state()

        self._packets_rejected = 0
        self._packet_format = 0
        self._frames_emitted = 0
        self._packets_parsed = 0
        self._warned_format = False
        self._expected_formats: tuple[int, ...] = ()
        self._warned_mismatch = False
        #: Optional observer called for every raw packet, before parsing.
        #: Used by the recorder and inspector; never affects decoding.
        self._packet_observer = None
        #: Packets fed from a replay rather than the socket.
        self._replay_packets = 0
        self._replay_active = False
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
        formats = options.get("expected_formats")
        if formats is not None:
            # Advisory only. The parser stays range-tolerant, so a surprise
            # version still works - this just lets the UI say "you selected
            # F1 26 but the game is sending 2025".
            self._expected_formats = tuple(int(f) for f in formats)
            self._warned_mismatch = False

    def set_packet_observer(self, observer) -> None:
        """Register a callback for raw packets. Observers must not raise."""
        self._packet_observer = observer

    def feed(self, data: bytes) -> None:
        """Inject a packet from a replay.

        Goes through the identical path as a live packet, which is what
        makes replay a faithful reproduction rather than a simulation.
        """
        self._replay_active = True
        self._replay_packets += 1
        self._on_packet(data)

    def end_replay(self) -> None:
        self._replay_active = False

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
            frame_rate=self._frame_rate.rate(),
            last_packet_age=0.0 if math.isinf(age) else age,
            detail=detail,
            error=self.listener.error,
            stage=self._stage(),
            bytes_per_sec=self.listener.bytes_per_sec,
            raw_packets=self.listener.raw_packets + self._replay_packets,
            packets_parsed=self._packets_parsed,
            frames_emitted=self._frames_emitted,
            packet_types=self._packet_type_summary(),
            last_sender=self.listener.last_sender,
            detected_port=self.listener.detected_port,
            receive_buffer_kb=self.listener.receive_buffer_size // 1024,
            format_mismatch=bool(
                self._expected_formats
                and self._packet_format
                and self._packet_format not in self._expected_formats
            ),
            packet_format=self._packet_format,
            **self._live_values(),
            **self._raw_values(),
        )

    def _live_values(self) -> dict[str, float | int]:
        """The most recent normalized frame, for display.

        Read straight from the adapter, so the UI can show real changing
        telemetry independently of anything downstream.
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
        if not self.listener.running and not self._replay_active:
            return TelemetryStage.WAITING
        if self.listener.raw_packets + self._replay_packets == 0:
            return TelemetryStage.SOCKET_BOUND
        if self._packets_parsed == 0:
            return TelemetryStage.PACKETS_RECEIVED
        if self._frames_emitted == 0:
            return TelemetryStage.PACKETS_PARSED
        if not self.listener.connected and not self._replay_active:
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
        self._frame_rate.reset()
        # Strides are learned per session: a different game, mode or session
        # may use a different array size, so never carry them across.
        parser.reset_stride_cache()
        with self._lock:
            self._telemetry: parser.CarTelemetry | None = None
            self._status: parser.CarStatus | None = None
            self._motion: parser.MotionData | None = None
            self._motion_ex: parser.MotionExtended | None = None
            self._pit_status = 0
            self._lap: parser.LapData | None = None
            self._session: parser.SessionData | None = None
            self._damage: parser.DamageData | None = None
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
            self._replay_packets = 0
            self._player_index = 0
            self._prev_gear = 0
            self._current_gear = 0
            self._last_shift = "none"
            self._shift_count = 0
            self._best_lap = 0.0

    # ------------------------------------------------------------------
    # packet handling
    # ------------------------------------------------------------------
    def _on_packet(self, data: bytes) -> None:
        observer = self._packet_observer
        if observer is not None:
            try:
                observer(data)
            except Exception:  # noqa: BLE001 - observers never break telemetry
                _rate_log.error("observer", "Packet observer failed")

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
        if (
            self._expected_formats
            and header.packet_format not in self._expected_formats
            and not self._warned_mismatch
        ):
            self._warned_mismatch = True
            _log.warning(
                "Game mode expects packet format %s but the game is sending "
                "%d. Telemetry still parses; check the selected game mode.",
                "/".join(str(f) for f in self._expected_formats),
                header.packet_format,
            )

        packet_id = header.packet_id
        with self._lock:
            self._packet_counts[packet_id] = self._packet_counts.get(packet_id, 0) + 1

        if packet_id == p.PACKET_CAR_TELEMETRY:
            self._handle_car_telemetry(data, header)
        elif packet_id == p.PACKET_CAR_STATUS:
            self._store("_status", parser.parse_car_status_full(data, header))
        elif packet_id == p.PACKET_MOTION:
            self._handle_motion(data, header)
        elif packet_id == p.PACKET_MOTION_EX:
            self._store("_motion_ex", parser.parse_motion_extended(data, header))
        elif packet_id == p.PACKET_LAP_DATA:
            lap = parser.parse_lap_data(data, header)
            if lap is not None:
                with self._lock:
                    self._lap = lap
                    self._pit_status = lap.pit_status
        elif packet_id == p.PACKET_SESSION:
            self._store("_session", parser.parse_session(data, header))
        elif packet_id == p.PACKET_CAR_DAMAGE:
            self._handle_damage(data, header)
        # Remaining packet ids are not consumed yet and are ignored.

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
        detail = parser.parse_car_damage_full(data, header)
        if detail is not None:
            with self._lock:
                self._damage = detail
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

        self._frame_rate.mark()
        self._emit(frame)

    def _build_frame(self) -> TelemetryFrame:
        """Assemble a frame. Caller must hold the lock."""
        telemetry = self._telemetry
        status = self._status
        motion = self._motion
        motion_ex = self._motion_ex
        lap = self._lap
        session = self._session
        damage = self._damage

        # Best lap is tracked here because F1 reports only the last lap.
        if lap and lap.last_lap_time_s > 0:
            if self._best_lap <= 0 or lap.last_lap_time_s < self._best_lap:
                self._best_lap = lap.last_lap_time_s

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
            # --- tyres ---
            tyre_surface_temp=telemetry.tyre_surface_temp,
            tyre_inner_temp=telemetry.tyre_inner_temp,
            tyre_pressure=telemetry.tyre_pressure,
            brake_temp=telemetry.brake_temp,
            tyre_wear=damage.tyre_wear if damage else Wheels(),
            tyre_compound=status.tyre_compound if status else "",
            tyre_age_laps=status.tyre_age_laps if status else -1,
            # --- lap / position ---
            position=lap.position if lap else 0,
            current_lap=lap.current_lap if lap else 0,
            total_laps=session.total_laps if session else 0,
            last_lap_time_s=lap.last_lap_time_s if lap else 0.0,
            current_lap_time_s=lap.current_lap_time_s if lap else 0.0,
            best_lap_time_s=self._best_lap,
            sector=lap.sector if lap else 0,
            sector1_time_s=lap.sector1_time_s if lap else 0.0,
            sector2_time_s=lap.sector2_time_s if lap else 0.0,
            lap_distance_m=lap.lap_distance_m if lap else 0.0,
            delta_to_car_ahead_s=lap.delta_to_car_ahead_s if lap else 0.0,
            delta_to_leader_s=lap.delta_to_leader_s if lap else 0.0,
            lap_invalid=lap.lap_invalid if lap else False,
            penalties_s=lap.penalties_s if lap else 0,
            # --- fuel / ERS ---
            fuel_in_tank=status.fuel_in_tank if status else 0.0,
            fuel_capacity=status.fuel_capacity if status else 0.0,
            fuel_remaining_laps=status.fuel_remaining_laps if status else 0.0,
            ers_store_percent=(
                100.0 * status.ers_store_joules / p.ERS_MAX_JOULES if status else 0.0
            ),
            ers_mode=status.ers_mode if status else "",
            ers_deployed_lap=status.ers_deployed_lap if status else 0.0,
            ers_harvested_lap=status.ers_harvested_lap if status else 0.0,
            # --- session / conditions ---
            session_type=session.session_type if session else "",
            weather=session.weather if session else "",
            air_temperature=session.air_temperature if session else 0.0,
            track_temperature=session.track_temperature if session else 0.0,
            session_time_left_s=session.session_time_left_s if session else 0.0,
            # --- damage ---
            front_left_wing_damage=damage.front_left_wing if damage else 0,
            front_right_wing_damage=damage.front_right_wing if damage else 0,
            rear_wing_damage=damage.rear_wing if damage else 0,
            floor_damage=damage.floor if damage else 0,
            diffuser_damage=damage.diffuser if damage else 0,
            sidepod_damage=damage.sidepod if damage else 0,
            gearbox_damage=damage.gearbox if damage else 0,
            engine_damage=damage.engine if damage else 0,
        )
