"""Entry point.

    python -m app.main              launch the GUI
    python -m app.main --headless   run the engine with no UI
    python -m app.main --selftest   verify startup and exit (no window)

Whatever happens - clean exit, crash, or Ctrl+C - the finally block stops
the motors. That guarantee is why every path goes through here.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from app.config.settings import AppSettings
from app.core.application import Application
from app.core.logging import get_logger, setup_logging

_log = get_logger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="racing-haptic-engine",
        description="Racing Haptic Engine - telemetry-driven controller haptics",
    )
    parser.add_argument("--headless", action="store_true", help="run without the UI")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="start every subsystem, report status, then exit",
    )
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    parser.add_argument("--port", type=int, help="override the telemetry UDP port")
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="diagnose the telemetry pipeline stage by stage and exit",
    )
    parser.add_argument(
        "--diagnose-seconds",
        type=float,
        default=10.0,
        help="how long --diagnose should sample for (default 10)",
    )
    return parser.parse_args(argv)


def run_diagnose(port: int, seconds: float) -> int:
    """Stage-by-stage telemetry diagnosis. Does not start the engine or
    touch the controller - it only listens."""

    from app.diagnostics.telemetry_probe import TelemetryProbe, format_report

    probe = TelemetryProbe(port=port)
    print(f"Probing UDP {probe.stats.bind_address}:{port} for {seconds:.0f}s ...\n")

    if not probe.bind():
        print(format_report(probe.stats))
        return 1

    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            probe.poll(0.2)
            stats = probe.stats
            remaining = deadline - time.monotonic()
            print(
                f"\r  packets {stats.packets:<7} "
                f"{stats.packets_per_sec:>5.0f}/s  "
                f"{stats.bytes_per_sec / 1024:>6.1f} KB/s  "
                f"frames {stats.frames_emitted:<7} "
                f"rejected {stats.headers_rejected:<5} "
                f"({remaining:>4.1f}s left)",
                end="",
                flush=True,
            )
        print("\n")
        print(format_report(probe.stats))
    finally:
        probe.close()

    return 0 if probe.stats.frames_emitted > 0 else 1


def _build_application(args: argparse.Namespace) -> Application:
    settings = AppSettings.load()
    if args.verbose:
        settings.verbose_logging = True
    if args.port:
        settings.udp_port = args.port
    settings.clamped()
    return Application(settings)


def run_gui(app: Application) -> int:
    from PySide6.QtWidgets import QApplication

    from app.ui import theme
    from app.ui.main_window import MainWindow

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("Racing Haptic Engine")
    qt_app.setOrganizationName("RacingHapticEngine")
    qt_app.setStyleSheet(theme.STYLESHEET)
    # Keep running when the window is hidden to the tray.
    qt_app.setQuitOnLastWindowClosed(False)

    window = MainWindow(app)
    if app.settings.start_minimized:
        window.hide()
    else:
        window.show()

    # Motors must stop even if Qt tears down without a close event.
    qt_app.aboutToQuit.connect(app.shutdown)
    return qt_app.exec()


def run_headless(app: Application) -> int:
    import threading

    stop = threading.Event()

    def handle_signal(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, handle_signal)
    try:
        signal.signal(signal.SIGTERM, handle_signal)
    except (AttributeError, ValueError):
        pass  # not available on every platform/thread

    report = app.report()
    print("Racing Haptic Engine - headless")
    print(f"  Controller : {report.controller_name} (slot {report.controller_index})")
    if report.adapter:
        print(f"  Telemetry  : {report.adapter.display_name} on UDP {app.settings.udp_port}")
    print(f"  Engine     : {app.engine.tick_rate:.0f} Hz")
    print("  Press Ctrl+C to stop.")

    while not stop.wait(0.5):
        pass
    return 0


def run_selftest(app: Application) -> int:
    """Prove every subsystem starts and stops cleanly, then exit."""
    import time

    time.sleep(0.6)
    report = app.report()
    engine = report.engine

    checks = [
        ("XInput library", report.xinput_available, report.xinput_dll or report.xinput_error),
        ("Controller", report.controller_connected, report.controller_name),
        ("Haptic engine", engine.running, f"{engine.tick_rate:.0f} Hz"),
        (
            "Telemetry listener",
            bool(report.adapter and report.adapter.running),
            report.adapter.display_name if report.adapter else "none",
        ),
        ("Profiles", len(app.profiles.profiles) > 0, f"{len(app.profiles.profiles)} loaded"),
    ]

    print("Racing Haptic Engine - self test\n")
    for name, ok, detail in checks:
        print(f"  [{'OK' if ok else '--'}]  {name:<20} {detail}")

    # A missing controller is expected when nothing is plugged in and must
    # not be reported as a failure of the software.
    required = [ok for name, ok, _ in checks if name != "Controller"]
    print()
    if all(required):
        print("All software subsystems started correctly.")
        return 0
    print("One or more subsystems failed to start.")
    return 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging(verbose=args.verbose)

    # Diagnosis runs before the Application is built: it must not start the
    # engine, and above all it must not bind the port the app would bind.
    if args.diagnose:
        port = args.port or AppSettings.load().udp_port
        return run_diagnose(port, args.diagnose_seconds)

    app = _build_application(args)
    try:
        app.startup()
        if args.selftest:
            return run_selftest(app)
        if args.headless:
            return run_headless(app)
        return run_gui(app)
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001
        _log.exception("Fatal error")
        return 1
    finally:
        # The one guarantee this whole application makes.
        app.shutdown()


if __name__ == "__main__":
    sys.exit(main())
