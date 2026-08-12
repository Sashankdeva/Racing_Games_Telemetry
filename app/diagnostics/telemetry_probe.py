"""Stage-by-stage diagnosis of the telemetry pipeline.

Answers one question: where exactly does the data stop?

    UDP socket bound
      -> packets arriving
        -> header accepted
          -> payload parsed
            -> frame emitted
              -> reaching engine state

Each stage has its own counter, so a failure is localised instead of
guessed at. The probe drives the *real* parser and the *real* adapter - it
deliberately does not reimplement them, so anything it proves applies to
the running application.

What this adds over the app's own Diagnostics page:

  * It names the process holding the port when the bind fails, which is
    usually just the app itself.
  * Header rejection reasons broken down by packet format, so a version
    mismatch is named rather than showing up as a silent zero. Both the
    probe and the app bind exclusively - SO_REUSEADDR on Windows lets a
    second process bind the same UDP port with no error and then silently
    receive nothing, so it is never used.
"""

from __future__ import annotations

import re
import socket
import struct
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field

from app.games.f1 import packets as p
from app.games.f1 import parser
from app.games.f1.adapter import F1Adapter

#: Human-readable names for the packet ids we care about.
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


@dataclass
class ProbeStats:
    # --- stage 1: socket ---
    bound: bool = False
    bind_error: str = ""
    port: int = 0
    bind_address: str = ""
    #: Who already holds the port, when the bind fails.
    holder_pid: int = 0
    holder_name: str = ""
    holder_command: str = ""

    @property
    def holder_is_this_app(self) -> bool:
        return "app.main" in self.holder_command or "racing" in self.holder_name.lower()

    # --- stage 2: reception ---
    packets: int = 0
    bytes_total: int = 0
    first_packet_time: float = 0.0
    last_packet_time: float = 0.0
    senders: Counter = field(default_factory=Counter)

    # --- stage 3: header validation ---
    headers_ok: int = 0
    headers_rejected: int = 0
    reject_reasons: Counter = field(default_factory=Counter)
    formats_seen: Counter = field(default_factory=Counter)
    packet_ids: Counter = field(default_factory=Counter)
    sizes_by_id: dict[int, Counter] = field(default_factory=dict)

    # --- stage 4/5: parse + adapter ---
    parse_ok: Counter = field(default_factory=Counter)
    parse_failed: Counter = field(default_factory=Counter)
    frames_emitted: int = 0
    last_frame = None

    def elapsed(self) -> float:
        if not self.first_packet_time:
            return 0.0
        return max(1e-6, self.last_packet_time - self.first_packet_time)

    @property
    def packets_per_sec(self) -> float:
        return self.packets / self.elapsed() if self.packets > 1 else 0.0

    @property
    def bytes_per_sec(self) -> float:
        return self.bytes_total / self.elapsed() if self.packets > 1 else 0.0

    @property
    def last_packet_ms(self) -> float:
        if not self.last_packet_time:
            return float("inf")
        return (time.perf_counter() - self.last_packet_time) * 1000.0


class TelemetryProbe:
    """Binds a port exclusively and reports what happens at every stage."""

    def __init__(self, port: int = 20777, bind_address: str = "0.0.0.0") -> None:
        self.stats = ProbeStats(port=port, bind_address=bind_address)
        self._socket: socket.socket | None = None
        self._adapter = F1Adapter(port=port)
        self._adapter.set_frame_callback(self._on_frame)

    # ------------------------------------------------------------------
    def _on_frame(self, frame) -> None:
        self.stats.frames_emitted += 1
        self.stats.last_frame = frame

    def bind(self) -> bool:
        """Bind WITHOUT SO_REUSEADDR so a port conflict is a hard error."""
        stats = self.stats
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.2)
            sock.bind((stats.bind_address, stats.port))
        except OSError as exc:
            stats.bound = False
            stats.bind_error = f"{exc.__class__.__name__}: {exc}"
            # Naming the process is the difference between a dead end and an
            # actionable answer - most often it is simply the app itself.
            identify_port_holder(stats)
            return False
        self._socket = sock
        stats.bound = True
        return True

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    # ------------------------------------------------------------------
    def poll(self, budget: float = 0.2) -> None:
        """Drain whatever has arrived, running each stage in turn."""
        if self._socket is None:
            return
        deadline = time.perf_counter() + budget
        while time.perf_counter() < deadline:
            try:
                data, sender = self._socket.recvfrom(4096)
            except socket.timeout:
                return
            except OSError:
                return
            self._ingest(data, sender)

    def _ingest(self, data: bytes, sender) -> None:
        stats = self.stats
        now = time.perf_counter()

        # stage 2 - reception
        stats.packets += 1
        stats.bytes_total += len(data)
        stats.last_packet_time = now
        if not stats.first_packet_time:
            stats.first_packet_time = now
        stats.senders[sender[0]] += 1

        # stage 3 - header validation
        header = p.parse_header(data)
        if header is None:
            stats.headers_rejected += 1
            stats.reject_reasons[self._reject_reason(data)] += 1
            return

        stats.headers_ok += 1
        stats.formats_seen[header.packet_format] += 1
        stats.packet_ids[header.packet_id] += 1
        stats.sizes_by_id.setdefault(header.packet_id, Counter())[len(data)] += 1

        # stage 4 - payload parsing (only the packets we actually consume)
        self._try_parse(data, header)

        # stage 5 - the real adapter, which emits normalized frames
        self._adapter._on_packet(data)

    def _try_parse(self, data: bytes, header) -> None:
        stats = self.stats
        checks = {
            p.PACKET_CAR_TELEMETRY: parser.parse_car_telemetry,
            p.PACKET_CAR_STATUS: parser.parse_car_status,
            p.PACKET_MOTION: parser.parse_motion,
            p.PACKET_MOTION_EX: parser.parse_motion_extended,
        }
        handler = checks.get(header.packet_id)
        if handler is None:
            return
        name = PACKET_NAMES.get(header.packet_id, str(header.packet_id))
        try:
            result = handler(data, header)
        except Exception as exc:  # noqa: BLE001 - report, never crash the probe
            stats.parse_failed[f"{name}: {exc.__class__.__name__}"] += 1
            return
        if result is None:
            stats.parse_failed[f"{name}: returned None (size {len(data)})"] += 1
        else:
            stats.parse_ok[name] += 1

    @staticmethod
    def _reject_reason(data: bytes) -> str:
        if len(data) < 6:
            return f"too short ({len(data)} bytes)"
        packet_format = struct.unpack_from("<H", data, 0)[0]
        if not p.is_supported_format(packet_format):
            return (
                f"packet format {packet_format} outside the accepted range "
                f"{p.MIN_PACKET_FORMAT}-{p.MAX_PACKET_FORMAT}"
            )
        return f"header too short for format {packet_format} ({len(data)} bytes)"


# ----------------------------------------------------------------------
# who holds the port
# ----------------------------------------------------------------------
def identify_port_holder(stats: ProbeStats) -> None:
    """Fill in holder_* by asking Windows which process owns the UDP port.

    Best effort only: a failure here must never break the diagnosis, so
    every error is swallowed and the fields simply stay empty.
    """
    try:
        netstat = subprocess.run(
            ["netstat", "-ano", "-p", "UDP"],
            capture_output=True, text=True, timeout=8, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return

    pattern = re.compile(rf"UDP\s+\S*:{stats.port}\s+\S+\s+(\d+)")
    match = pattern.search(netstat)
    if not match:
        return

    pid = int(match.group(1))
    stats.holder_pid = pid

    try:
        tasklist = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=8, check=False,
        ).stdout.strip()
        if tasklist and "," in tasklist:
            stats.holder_name = tasklist.split(",")[0].strip('" ')
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        wmic = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
            capture_output=True, text=True, timeout=12, check=False,
        ).stdout.strip()
        stats.holder_command = wmic
    except (OSError, subprocess.SubprocessError):
        pass


# ----------------------------------------------------------------------
# text report
# ----------------------------------------------------------------------
def format_report(stats: ProbeStats) -> str:
    lines: list[str] = []
    add = lines.append

    add("=" * 66)
    add("TELEMETRY PIPELINE DIAGNOSIS")
    add("=" * 66)

    # --- stage 1 ---
    add("")
    add(f"[1] UDP SOCKET      {stats.bind_address}:{stats.port}")
    if stats.bound:
        add("    bound          : YES (exclusive)")
    else:
        add("    bound          : NO")
        add(f"    error          : {stats.bind_error}")
        if stats.holder_pid:
            add(f"    held by        : PID {stats.holder_pid}  {stats.holder_name}")
            if stats.holder_command:
                add(f"    command        : {stats.holder_command}")
        add("")
        if stats.holder_is_this_app:
            add("    -> This is the Racing Haptic Engine itself. The probe needs")
            add("       exclusive use of the port, so close the app and re-run:")
            add("")
            add("         python -m app.main --diagnose --diagnose-seconds 15")
            add("")
            add("       The same figures are also on the app's Diagnostics page")
            add("       while it runs - no need to close it just to read them.")
        elif stats.holder_pid:
            add("    -> Another program is using this telemetry port. Close it,")
            add("       or point both it and the game at a different port.")
        else:
            add("    -> Another process already holds this port.")
        add("=" * 66)
        return "\n".join(lines)

    # --- stage 2 ---
    add("")
    add("[2] PACKET RECEPTION")
    add(f"    packets        : {stats.packets}")
    add(f"    bytes          : {stats.bytes_total}")
    add(f"    packets/sec    : {stats.packets_per_sec:.1f}")
    add(f"    bytes/sec      : {stats.bytes_per_sec:,.0f}")
    if stats.packets:
        add(f"    last packet    : {stats.last_packet_ms:.0f} ms ago")
        add(f"    senders        : {', '.join(stats.senders)}")
    else:
        add("    -> ZERO packets. The socket is bound but the game is not")
        add("       sending here. Check the game's UDP IP address and port,")
        add("       that UDP telemetry is enabled, and that no other copy of")
        add("       this app is running.")
        add("=" * 66)
        return "\n".join(lines)

    # --- stage 3 ---
    add("")
    add("[3] HEADER VALIDATION")
    add(f"    accepted       : {stats.headers_ok}")
    add(f"    rejected       : {stats.headers_rejected}")
    for reason, count in stats.reject_reasons.most_common():
        add(f"      - {reason}  x{count}")
    if stats.formats_seen:
        formats = ", ".join(f"{fmt} (x{n})" for fmt, n in stats.formats_seen.most_common())
        add(f"    packet formats : {formats}")

    if stats.packet_ids:
        add("")
        add("    packet types received:")
        for packet_id, count in sorted(stats.packet_ids.items()):
            name = PACKET_NAMES.get(packet_id, "unknown")
            sizes = stats.sizes_by_id.get(packet_id, Counter())
            size_text = ", ".join(f"{s}B" for s, _ in sizes.most_common(2))
            add(f"      id {packet_id:>2}  {name:<20} x{count:<6} [{size_text}]")

    # --- stage 4 ---
    add("")
    add("[4] PAYLOAD PARSING")
    if stats.parse_ok:
        for name, count in stats.parse_ok.most_common():
            add(f"    OK   {name:<22} x{count}")
    if stats.parse_failed:
        for reason, count in stats.parse_failed.most_common():
            add(f"    FAIL {reason}  x{count}")
    if not stats.parse_ok and not stats.parse_failed:
        add("    (no packets of the types we consume have arrived)")

    # --- stage 5 ---
    add("")
    add("[5] NORMALIZED FRAMES")
    add(f"    frames emitted : {stats.frames_emitted}")
    frame = stats.last_frame
    if frame is None:
        add("    -> no frame produced. CarTelemetry (id 6) is what triggers a")
        add("       frame; if it is absent or failing to parse, nothing is emitted.")
    else:
        add(f"    valid          : {frame.valid}")
        add(f"    RPM            : {frame.rpm:.0f} / {frame.max_rpm:.0f}")
        add(f"    Speed          : {frame.speed_kph:.0f} kph")
        add(f"    Gear           : {frame.gear}")
        add(f"    Throttle       : {frame.throttle * 100:.0f}%")
        add(f"    Brake          : {frame.brake * 100:.0f}%")
        add(f"    Steering       : {frame.steering:+.2f}")
        add(f"    Surfaces       : {[s.name for s in frame.surfaces.as_tuple()]}")
        add(f"    Wheel slip     : {tuple(round(v, 3) for v in frame.wheel_slip_ratio.as_tuple())}")
        add(f"    G lat/lon      : {frame.g_lateral:+.2f} / {frame.g_longitudinal:+.2f}")

    add("")
    add("=" * 66)
    return "\n".join(lines)
