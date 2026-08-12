"""UDP listener for F1 telemetry.

Runs on its own thread, entirely independent of the UI and the haptic loop:
neither can stall reception, and reception cannot stall them.

Design decisions that matter for reliability:

  * Exclusive bind, no SO_REUSEADDR. On Windows that flag lets a second
    process bind the same UDP port with no error, after which the OS
    delivers every packet to only one of them - the loser reports
    "listening" forever while receiving nothing. An exclusive bind turns
    that silent failure into a reported one.
  * Bind to 0.0.0.0 so packets are accepted on loopback and on every LAN
    interface, whichever address the game was pointed at.
  * SO_BROADCAST, so the game's "UDP Broadcast Mode" is also received.
  * A large receive buffer. F1 sends roughly 600 packets/sec at 60 Hz,
    about 780 KB/s; the default buffer is far too small to absorb a burst
    while the thread is busy, and overflow shows up as silent packet loss.
  * Non-blocking drain. Each wake-up empties the socket completely rather
    than taking one datagram per poll, so a burst is consumed in one pass.
  * Optional port auto-detection: if the configured port stays silent, a
    background scan watches the other ports F1 commonly uses and reports
    where the game is actually sending.
"""

from __future__ import annotations

import errno
import select
import socket
import threading
import time
from typing import Callable

from app.core.logging import RateLimitedLogger, get_logger
from app.games.base import RateTracker

_log = get_logger(__name__)
_rate_log = RateLimitedLogger(_log)

DEFAULT_PORT = 20777
#: F1's largest packet is ~1350 bytes; this leaves generous headroom.
RECV_BUFFER = 4096
#: Socket receive buffer. ~1 s of slack at F1's full 60 Hz packet rate.
SOCKET_RCVBUF = 1 << 20
POLL_TIMEOUT = 0.2
#: Ports F1 titles and common relays are known to use.
CANDIDATE_PORTS = (20777, 20778, 20779, 20780, 20781)

PacketCallback = Callable[[bytes], None]


class TelemetryListener:
    def __init__(
        self,
        port: int = DEFAULT_PORT,
        bind_address: str = "0.0.0.0",
        connection_timeout: float = 2.0,
        auto_detect_port: bool = True,
    ) -> None:
        self.port = port
        self.bind_address = bind_address
        self.connection_timeout = connection_timeout
        self.auto_detect_port = auto_detect_port

        self._callback: PacketCallback | None = None
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()

        self._rate = RateTracker()
        self._packets = 0
        self._raw_packets = 0
        self._bytes = 0
        self._window_started = 0.0
        self._last_packet_time = 0.0
        self._last_sender = ""
        self._error = ""
        self._rcvbuf = 0
        self._lock = threading.Lock()

        self._scanner: _PortScanner | None = None
        self._detected_port = 0

    # --- configuration ----------------------------------------------------
    def set_callback(self, callback: PacketCallback | None) -> None:
        self._callback = callback

    def set_port(self, port: int) -> None:
        """Change the listen port, restarting the socket if already running."""
        if port == self.port:
            return
        was_running = self.running
        if was_running:
            self.stop()
        self.port = port
        if was_running:
            self.start()

    # --- state ------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running.is_set()

    @property
    def bound(self) -> bool:
        return self._socket is not None

    @property
    def packets_received(self) -> int:
        return self._packets

    @property
    def raw_packets(self) -> int:
        """Datagrams counted straight after recvfrom(), pre-validation."""
        return self._raw_packets

    @property
    def bytes_received(self) -> int:
        return self._bytes

    @property
    def packet_rate(self) -> float:
        return self._rate.rate()

    @property
    def bytes_per_sec(self) -> float:
        if not self._window_started or self._packets < 2:
            return 0.0
        elapsed = time.monotonic() - self._window_started
        return self._bytes / elapsed if elapsed > 0 else 0.0

    @property
    def receive_buffer_size(self) -> int:
        return self._rcvbuf

    @property
    def last_sender(self) -> str:
        with self._lock:
            return self._last_sender

    @property
    def error(self) -> str:
        with self._lock:
            return self._error

    @property
    def detected_port(self) -> int:
        """A port where traffic was seen, if it is not the configured one."""
        return self._detected_port

    @property
    def last_packet_age(self) -> float:
        if self._last_packet_time <= 0.0:
            return float("inf")
        return time.monotonic() - self._last_packet_time

    @property
    def connected(self) -> bool:
        """True while packets are still arriving inside the timeout window."""
        return self.last_packet_age <= self.connection_timeout

    # --- lifecycle --------------------------------------------------------
    def start(self) -> bool:
        if self.running:
            return True

        sock = self._create_socket()
        if sock is None:
            return False

        self._socket = sock
        with self._lock:
            self._error = ""
        # Counters are per listening session, so they always line up with the
        # adapter's own parse/frame counts in diagnostics.
        self.reset_stats()
        self._running.set()
        self._thread = threading.Thread(target=self._loop, name="f1-telemetry", daemon=True)
        self._thread.start()
        _log.info(
            "Listening for F1 telemetry on UDP %s:%d (rcvbuf %d KB)",
            self.bind_address, self.port, self._rcvbuf // 1024,
        )

        if self.auto_detect_port:
            self._scanner = _PortScanner(
                exclude=self.port, on_found=self._on_port_detected
            )
            self._scanner.start()
        return True

    def _create_socket(self) -> socket.socket | None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # Deliberately NOT SO_REUSEADDR - see the module docstring.
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_RCVBUF)
            except OSError:
                pass  # not fatal, just less burst tolerance
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except OSError:
                pass
            sock.setblocking(False)
            sock.bind((self.bind_address, self.port))
        except OSError as exc:
            if getattr(exc, "winerror", None) == 10048 or exc.errno in (48, 98):
                message = (
                    f"UDP port {self.port} is already in use. Another copy of "
                    "this app, or another telemetry tool, is holding it."
                )
            else:
                message = f"Could not bind UDP port {self.port}: {exc}"
            with self._lock:
                self._error = message
            _log.error("%s", message)
            return None

        try:
            self._rcvbuf = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        except OSError:
            self._rcvbuf = 0
        return sock

    def stop(self) -> None:
        self._running.clear()
        if self._scanner is not None:
            self._scanner.stop()
            self._scanner = None
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        self._rate.reset()
        _log.info("Stopped F1 telemetry listener")

    def reset_stats(self) -> None:
        self._packets = 0
        self._raw_packets = 0
        self._bytes = 0
        self._window_started = time.monotonic()
        self._rate.reset()
        self._last_packet_time = 0.0

    def _on_port_detected(self, port: int) -> None:
        if self._detected_port == port:
            return
        self._detected_port = port
        _log.warning(
            "F1 telemetry detected on UDP port %d, but this app is listening "
            "on %d. Change the port in Settings, or point the game at %d.",
            port, self.port, self.port,
        )

    # --- receive loop -----------------------------------------------------
    def _loop(self) -> None:
        sock = self._socket
        if sock is None:
            return

        while self._running.is_set():
            try:
                readable, _, _ = select.select([sock], [], [], POLL_TIMEOUT)
            except (OSError, ValueError):
                if self._running.is_set():
                    _rate_log.error("select", "Telemetry socket select failed")
                continue

            if not readable:
                continue  # normal: the game simply is not sending right now

            # Drain everything queued in one pass so a burst never backs up.
            self._drain(sock)

    def _drain(self, sock: socket.socket) -> None:
        while self._running.is_set():
            try:
                data, sender = sock.recvfrom(RECV_BUFFER)
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                    return
                if self._running.is_set():
                    _rate_log.error("recv", "Telemetry socket error: %s", exc)
                return

            # RAW PACKET COUNTER - incremented immediately after recvfrom(),
            # before any validation or parsing. This is the ground truth for
            # "is anything arriving at all", independent of whether we can
            # make sense of it.
            self._raw_packets += 1

            if not data:
                continue

            self._packets += 1
            self._bytes += len(data)
            self._last_packet_time = time.monotonic()
            self._rate.mark()
            with self._lock:
                self._last_sender = sender[0]

            callback = self._callback
            if callback is None:
                continue
            try:
                callback(data)
            except Exception:  # noqa: BLE001 - a bad packet must not kill the thread
                _rate_log.error("callback", "Telemetry packet handler failed")
                _log.debug("Packet handler exception detail", exc_info=True)


class _PortScanner:
    """Watches the other ports F1 commonly uses.

    Purely diagnostic: it never redirects telemetry on its own, it just
    reports where traffic is actually landing so a port mismatch shows up
    as a clear message instead of silence. Ports it cannot bind are
    skipped, since something else legitimately owning them is not an error.
    """

    def __init__(self, exclude: int, on_found: Callable[[int], None]) -> None:
        self._exclude = exclude
        self._on_found = on_found
        self._running = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._running.set()
        self._thread = threading.Thread(
            target=self._run, name="f1-port-scan", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=1.5)
            self._thread = None

    def _run(self) -> None:
        sockets: list[socket.socket] = []
        by_socket: dict[socket.socket, int] = {}

        for port in CANDIDATE_PORTS:
            if port == self._exclude:
                continue
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setblocking(False)
                sock.bind(("0.0.0.0", port))
            except OSError:
                continue  # in use by something else; not our problem
            sockets.append(sock)
            by_socket[sock] = port

        if not sockets:
            return

        try:
            while self._running.is_set():
                try:
                    readable, _, _ = select.select(sockets, [], [], 0.5)
                except (OSError, ValueError):
                    return
                for sock in readable:
                    try:
                        data, _ = sock.recvfrom(RECV_BUFFER)
                    except OSError:
                        continue
                    if data:
                        self._on_found(by_socket[sock])
        finally:
            for sock in sockets:
                try:
                    sock.close()
                except OSError:
                    pass
