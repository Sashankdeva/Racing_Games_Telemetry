"""Raw packet recording and replay.

Records the *bytes exactly as they arrived*, with arrival offsets. Nothing
is parsed or normalized on the way in, so a recording is ground truth about
what the game actually sent - which is the whole point. If our parser is
wrong, the recording still holds the evidence to prove it.

Replay feeds those bytes back through the identical adapter and parser used
in live mode, so a bug reproduces deterministically without launching the
game.

File layout - one self-contained binary file:

    b"F1RE"          magic
    uint16           container version
    uint32           header JSON length
    <header JSON>    metadata (game mode, dates, counts, formats seen)
    records...       each: float64 offset_s, uint32 length, <length bytes>

The header is JSON so a recording can be inspected without this module.
"""

from __future__ import annotations

import json
import struct
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.core.logging import get_logger
from app.core.paths import data_dir

_log = get_logger(__name__)

MAGIC = b"F1RE"
CONTAINER_VERSION = 1
_HEADER_PREFIX = struct.Struct("<4sHI")
_RECORD = struct.Struct("<dI")

#: Refuse absurd packets so a corrupt file cannot allocate wildly.
MAX_PACKET_BYTES = 65535


@dataclass(slots=True)
class RecordingMeta:
    """Everything needed to understand a recording without parsing it."""

    game_mode: str = ""
    game_label: str = ""
    created: str = ""
    duration_s: float = 0.0
    packet_count: int = 0
    total_bytes: int = 0
    #: Packet formats actually observed, e.g. {"2026": 1200}.
    formats_seen: dict[str, int] = field(default_factory=dict)
    #: Packet ids actually observed, keyed by id as a string.
    packet_ids: dict[str, int] = field(default_factory=dict)
    note: str = ""

    @property
    def packet_rate(self) -> float:
        return self.packet_count / self.duration_s if self.duration_s > 0 else 0.0


def recordings_dir() -> Path:
    return data_dir() / "recordings"


class Recorder:
    """Appends raw packets to an open recording.

    Deliberately dumb and cheap: it is called from the telemetry receive
    path, so it does no parsing beyond peeking at the header bytes for
    metadata, and it never raises into the caller.
    """

    def __init__(self, path: Path, meta: RecordingMeta) -> None:
        self.path = Path(path)
        self.meta = meta
        self._handle = None
        self._start = 0.0
        self._failed = False

    @property
    def active(self) -> bool:
        return self._handle is not None

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self._start if self._start else 0.0

    def start(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Reserve space for the header; it is rewritten on close once
            # the real counts are known.
            self._handle = self.path.open("wb")
            self._write_header(placeholder=True)
        except OSError as exc:
            _log.error("Could not start recording: %s", exc)
            self._handle = None
            return False

        self._start = time.perf_counter()
        self.meta.created = time.strftime("%Y-%m-%d %H:%M:%S")
        _log.info("Recording telemetry to %s", self.path.name)
        return True

    def _write_header(self, placeholder: bool = False) -> None:
        payload = json.dumps(asdict(self.meta)).encode("utf-8")
        if placeholder:
            # Pad so the final header can be written in place without
            # shifting every record after it.
            payload = payload + b" " * max(0, 2048 - len(payload))
        self._header_size = len(payload)
        self._handle.write(_HEADER_PREFIX.pack(MAGIC, CONTAINER_VERSION, len(payload)))
        self._handle.write(payload)

    def write(self, data: bytes, packet_format: int = 0, packet_id: int = -1) -> None:
        """Record one packet. Never raises - recording must not break
        telemetry."""
        if self._handle is None or self._failed:
            return
        if not data or len(data) > MAX_PACKET_BYTES:
            return
        try:
            self._handle.write(_RECORD.pack(self.elapsed, len(data)))
            self._handle.write(data)
        except (OSError, ValueError) as exc:
            self._failed = True
            _log.error("Recording failed, stopping: %s", exc)
            return

        self.meta.packet_count += 1
        self.meta.total_bytes += len(data)
        if packet_format:
            key = str(packet_format)
            self.meta.formats_seen[key] = self.meta.formats_seen.get(key, 0) + 1
        if packet_id >= 0:
            key = str(packet_id)
            self.meta.packet_ids[key] = self.meta.packet_ids.get(key, 0) + 1

    def stop(self) -> RecordingMeta:
        """Finalise the header and close. Safe to call twice."""
        if self._handle is None:
            return self.meta

        self.meta.duration_s = self.elapsed
        try:
            self._handle.flush()
            # Rewrite the header in place; it was padded to leave room.
            self._handle.seek(0)
            payload = json.dumps(asdict(self.meta)).encode("utf-8")
            payload = payload + b" " * max(0, self._header_size - len(payload))
            self._handle.write(
                _HEADER_PREFIX.pack(MAGIC, CONTAINER_VERSION, self._header_size)
            )
            self._handle.write(payload[: self._header_size])
        except OSError as exc:
            _log.error("Could not finalise recording header: %s", exc)
        finally:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None

        _log.info(
            "Recording stopped: %d packets, %.1fs, %s",
            self.meta.packet_count, self.meta.duration_s, self.path.name,
        )
        return self.meta


def read_meta(path: Path) -> RecordingMeta | None:
    """Read a recording's header without loading its packets."""
    try:
        with Path(path).open("rb") as handle:
            prefix = handle.read(_HEADER_PREFIX.size)
            if len(prefix) < _HEADER_PREFIX.size:
                return None
            magic, version, length = _HEADER_PREFIX.unpack(prefix)
            if magic != MAGIC or version > CONTAINER_VERSION:
                return None
            payload = handle.read(length)
    except OSError:
        return None

    try:
        data = json.loads(payload.decode("utf-8").strip() or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    meta = RecordingMeta()
    for key, value in data.items():
        if hasattr(meta, key):
            try:
                setattr(meta, key, value)
            except (TypeError, ValueError):
                pass
    return meta


def read_packets(path: Path):
    """Yield (offset_seconds, packet_bytes).

    Truncated or corrupt tails stop iteration rather than raising - a
    recording cut short by a crash is still worth replaying up to the cut.
    """
    try:
        handle = Path(path).open("rb")
    except OSError as exc:
        _log.error("Cannot open recording %s: %s", path, exc)
        return

    with handle:
        prefix = handle.read(_HEADER_PREFIX.size)
        if len(prefix) < _HEADER_PREFIX.size:
            return
        magic, version, length = _HEADER_PREFIX.unpack(prefix)
        if magic != MAGIC or version > CONTAINER_VERSION:
            _log.error("%s is not a recording this build understands", path)
            return
        handle.read(length)  # skip metadata

        while True:
            head = handle.read(_RECORD.size)
            if len(head) < _RECORD.size:
                return
            offset, size = _RECORD.unpack(head)
            if size == 0 or size > MAX_PACKET_BYTES:
                return
            data = handle.read(size)
            if len(data) < size:
                return  # truncated tail
            yield offset, data


def list_recordings(directory: Path | None = None) -> list[tuple[Path, RecordingMeta]]:
    """Every readable recording, newest first."""
    target = Path(directory) if directory else recordings_dir()
    if not target.exists():
        return []

    found = []
    for path in target.glob("*.f1re"):
        meta = read_meta(path)
        if meta is not None:
            found.append((path, meta))
    found.sort(key=lambda item: item[1].created, reverse=True)
    return found
