"""Logging setup plus an in-memory ring buffer the Diagnostics page reads.

Normal mode stays quiet (INFO to file, WARNING to console). Verbose mode
unlocks DEBUG. High-frequency loops must use the rate-limited helpers here
rather than logging every tick.
"""

from __future__ import annotations

import logging
import logging.handlers
import threading
import time
from collections import deque
from dataclasses import dataclass

from app.core.paths import ensure_dirs, logs_dir

_LOG_FORMAT = "%(asctime)s  %(levelname)-7s  %(name)-28s  %(message)s"
_DATE_FORMAT = "%H:%M:%S"

_configured = False
_ring: "RingBufferHandler | None" = None


@dataclass(frozen=True, slots=True)
class LogRecordView:
    """Flattened record for UI display - no logging internals leak upward."""

    timestamp: float
    level: str
    logger: str
    message: str

    @property
    def clock(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.timestamp))


class RingBufferHandler(logging.Handler):
    """Keeps the most recent N records so Diagnostics can show them."""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self._records: deque[LogRecordView] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            view = LogRecordView(
                timestamp=record.created,
                level=record.levelname,
                logger=record.name,
                message=record.getMessage(),
            )
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            self._records.append(view)

    def snapshot(self) -> list[LogRecordView]:
        with self._lock:
            return list(self._records)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


def setup_logging(verbose: bool = False, to_file: bool = True) -> None:
    """Install handlers. Safe to call more than once."""
    global _configured, _ring

    root = logging.getLogger("app")
    level = logging.DEBUG if verbose else logging.INFO
    root.setLevel(level)

    if _configured:
        # Just retune the level for a verbose-mode toggle at runtime.
        for handler in root.handlers:
            if isinstance(handler, RingBufferHandler):
                handler.setLevel(level)
        return

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(formatter)
    root.addHandler(console)

    _ring = RingBufferHandler()
    _ring.setLevel(level)
    _ring.setFormatter(formatter)
    root.addHandler(_ring)

    if to_file:
        try:
            ensure_dirs()
            file_handler = logging.handlers.RotatingFileHandler(
                logs_dir() / "racing_haptic_engine.log",
                maxBytes=1_000_000,
                backupCount=3,
                encoding="utf-8",
            )
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError:
            root.warning("Could not open log file; continuing without file logging")

    root.propagate = False
    _configured = True


def set_verbose(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.getLogger("app").setLevel(level)
    if _ring is not None:
        _ring.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith("app") else f"app.{name}")


def recent_logs() -> list[LogRecordView]:
    return _ring.snapshot() if _ring else []


def clear_logs() -> None:
    if _ring:
        _ring.clear()


class RateLimitedLogger:
    """Wraps a logger so hot loops can report problems without flooding.

    Identical messages are collapsed to at most one per interval, and the
    suppressed count is reported when the message next gets through.
    """

    def __init__(self, logger: logging.Logger, interval: float = 5.0) -> None:
        self._logger = logger
        self._interval = interval
        self._last: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}
        self._lock = threading.Lock()

    def _should_emit(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            last = self._last.get(key, 0.0)
            if now - last >= self._interval:
                self._last[key] = now
                count = self._suppressed.pop(key, 0)
                return True, count
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return False, 0

    def warning(self, key: str, message: str, *args) -> None:
        emit, suppressed = self._should_emit(key)
        if emit:
            suffix = f" (+{suppressed} suppressed)" if suppressed else ""
            self._logger.warning(message + suffix, *args)

    def error(self, key: str, message: str, *args) -> None:
        emit, suppressed = self._should_emit(key)
        if emit:
            suffix = f" (+{suppressed} suppressed)" if suppressed else ""
            self._logger.error(message + suffix, *args)

    def debug(self, key: str, message: str, *args) -> None:
        emit, suppressed = self._should_emit(key)
        if emit:
            suffix = f" (+{suppressed} suppressed)" if suppressed else ""
            self._logger.debug(message + suffix, *args)
