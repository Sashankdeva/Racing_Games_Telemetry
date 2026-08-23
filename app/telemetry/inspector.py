"""Telemetry Inspector - per-packet and per-field validation.

Answers the five questions that matter for every field:

  1. Is it present?          did any packet carrying it arrive
  2. Parsed correctly?       did the decode produce a usable value
  3. Normalized correctly?   does the frame value match the parsed one
  4. Does it change?         or is it pinned at a constant / zero
  5. Same in the UI?         the UI reads this same frame, so a mismatch
                             here localises to the widget binding

Point 4 is the one that catches the failure we actually hit before, where a
field looked fine because it held a plausible constant. A value that never
moves across a whole session is reported as SUSPECT rather than OK - it may
be legitimately static (gear in a pit box) but it deserves a second look.

Nothing here re-implements parsing. It observes the real adapter's output,
so what it reports is what the application actually sees.
"""

from __future__ import annotations

import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field

from app.core.models import TelemetryFrame
from app.games.f1 import packets as p

#: Readable names for packet ids.
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


@dataclass(slots=True)
class PacketStat:
    """Live statistics for one packet type."""

    packet_id: int
    name: str
    count: int = 0
    bytes_total: int = 0
    last_seen: float = 0.0
    sizes: Counter = field(default_factory=Counter)
    formats: Counter = field(default_factory=Counter)
    rejected: int = 0
    _stamps: deque = field(default_factory=lambda: deque(maxlen=120))

    def mark(self, size: int, packet_format: int) -> None:
        now = time.monotonic()
        self.count += 1
        self.bytes_total += size
        self.last_seen = now
        self.sizes[size] += 1
        if packet_format:
            self.formats[packet_format] += 1
        self._stamps.append(now)

    @property
    def rate(self) -> float:
        """Packets per second over the recent window."""
        if len(self._stamps) < 2:
            return 0.0
        span = self._stamps[-1] - self._stamps[0]
        return (len(self._stamps) - 1) / span if span > 0 else 0.0

    @property
    def age(self) -> float:
        return time.monotonic() - self.last_seen if self.last_seen else float("inf")

    @property
    def common_size(self) -> int:
        return self.sizes.most_common(1)[0][0] if self.sizes else 0

    @property
    def common_format(self) -> int:
        return self.formats.most_common(1)[0][0] if self.formats else 0


@dataclass(slots=True)
class FieldStat:
    """Observation history for one normalized field."""

    key: str
    label: str
    #: Where it comes from, shown so a missing field points at a packet.
    source: str = ""
    samples: int = 0
    distinct: int = 0
    first_value: object = None
    last_value: object = None
    minimum: float | None = None
    maximum: float | None = None
    ever_nonzero: bool = False
    _seen: set = field(default_factory=set)

    def observe(self, value) -> None:
        self.samples += 1
        if self.samples == 1:
            self.first_value = value
        self.last_value = value

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric = float(value)
            self.minimum = numeric if self.minimum is None else min(self.minimum, numeric)
            self.maximum = numeric if self.maximum is None else max(self.maximum, numeric)
            if numeric != 0.0:
                self.ever_nonzero = True
            key = round(numeric, 4)
        else:
            if value not in (None, "", False):
                self.ever_nonzero = True
            key = value

        # Cap the distinct set so a long session cannot grow without bound.
        if len(self._seen) < 512:
            self._seen.add(key)
        self.distinct = len(self._seen)

    @property
    def changing(self) -> bool:
        return self.distinct > 1

    @property
    def verdict(self) -> str:
        """OK / STATIC / ABSENT / NO DATA - deliberately blunt."""
        if self.samples == 0:
            return "NO DATA"
        if not self.ever_nonzero:
            return "ABSENT"
        if not self.changing:
            return "STATIC"
        return "OK"

    @property
    def range_text(self) -> str:
        if self.minimum is None or self.maximum is None:
            return str(self.last_value)
        if self.minimum == self.maximum:
            return f"{self.minimum:g}"
        return f"{self.minimum:g} .. {self.maximum:g}"


#: The fields the brief calls out, with the packet each depends on. Listing
#: the source means "NO DATA" immediately says which packet is missing.
TRACKED_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("rpm", "RPM", "CarTelemetry"),
    ("speed_kph", "Speed", "CarTelemetry"),
    ("gear", "Gear", "CarTelemetry"),
    ("throttle", "Throttle", "CarTelemetry"),
    ("brake", "Brake", "CarTelemetry"),
    ("steering", "Steering", "CarTelemetry"),
    ("drs_active", "DRS", "CarTelemetry"),
    ("ers_store_percent", "ERS store", "CarStatus"),
    ("ers_mode", "ERS mode", "CarStatus"),
    ("fuel_in_tank", "Fuel", "CarStatus"),
    ("current_lap", "Lap", "LapData"),
    ("position", "Position", "LapData"),
    ("delta_to_car_ahead_s", "Gap ahead", "LapData"),
    ("last_lap_time_s", "Last lap", "LapData"),
    ("tyre_compound", "Tyre compound", "CarStatus"),
    ("tyre_age_laps", "Tyre age", "CarStatus"),
    ("tyre_wear_avg", "Tyre wear", "CarDamage"),
    ("tyre_temp_avg", "Tyre temperature", "CarTelemetry"),
    ("tyre_pressure_avg", "Tyre pressure", "CarTelemetry"),
    ("max_rpm", "Max RPM", "CarStatus"),
    ("weather", "Weather", "Session"),
    ("track_temperature", "Track temp", "Session"),
)


def _field_value(frame: TelemetryFrame, key: str):
    """Read a tracked field, including the per-wheel averages."""
    if key == "tyre_wear_avg":
        return round(frame.tyre_wear.avg, 2)
    if key == "tyre_temp_avg":
        return round(frame.tyre_surface_temp.avg, 1)
    if key == "tyre_pressure_avg":
        return round(frame.tyre_pressure.avg, 2)
    return getattr(frame, key, None)


class TelemetryInspector:
    """Observes packets and frames; owns no parsing of its own."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._packets: dict[int, PacketStat] = {}
        self._fields: dict[str, FieldStat] = {
            key: FieldStat(key=key, label=label, source=source)
            for key, label, source in TRACKED_FIELDS
        }
        self._frames = 0
        self._unparseable = 0
        self._started = time.monotonic()
        #: First bytes of the most recent unparseable packet, for evidence.
        self.last_bad_packet: bytes = b""

    # ------------------------------------------------------------------
    def observe_packet(self, data: bytes) -> None:
        """Called for every raw packet, before the adapter consumes it."""
        header = p.parse_header(data)
        with self._lock:
            if header is None:
                self._unparseable += 1
                self.last_bad_packet = data[:32]
                return
            stat = self._packets.get(header.packet_id)
            if stat is None:
                stat = PacketStat(
                    packet_id=header.packet_id,
                    name=PACKET_NAMES.get(header.packet_id, f"id {header.packet_id}"),
                )
                self._packets[header.packet_id] = stat
            stat.mark(len(data), header.packet_format)

    def observe_frame(self, frame: TelemetryFrame) -> None:
        """Called for every normalized frame the adapter emits."""
        if not frame.valid:
            return
        with self._lock:
            self._frames += 1
            for key, stat in self._fields.items():
                stat.observe(_field_value(frame, key))

    def reset(self) -> None:
        with self._lock:
            self._packets.clear()
            for key, label, source in TRACKED_FIELDS:
                self._fields[key] = FieldStat(key=key, label=label, source=source)
            self._frames = 0
            self._unparseable = 0
            self._started = time.monotonic()
            self.last_bad_packet = b""

    # ------------------------------------------------------------------
    @property
    def frames(self) -> int:
        return self._frames

    @property
    def unparseable(self) -> int:
        return self._unparseable

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def packet_stats(self) -> list[PacketStat]:
        with self._lock:
            return sorted(self._packets.values(), key=lambda s: s.packet_id)

    def field_stats(self) -> list[FieldStat]:
        with self._lock:
            return [self._fields[key] for key, _, _ in TRACKED_FIELDS]

    def problem_fields(self) -> list[FieldStat]:
        """Fields that are absent, static, or never seen - the ones worth
        looking at before trusting the pipeline."""
        return [s for s in self.field_stats() if s.verdict != "OK"]

    def formats_seen(self) -> Counter:
        counter: Counter = Counter()
        for stat in self.packet_stats():
            counter.update(stat.formats)
        return counter
