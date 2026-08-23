"""Entry point.

    python -m app.main              launch the GUI
    python -m app.main --headless   run telemetry with no UI
    python -m app.main --selftest   verify startup and exit (no window)
    python -m app.main --diagnose   trace the telemetry pipeline, then exit
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from app.config.settings import AppSettings
from app.core.application import Application
from app.core.logging import get_logger, setup_logging
from app.core.paths import logs_dir, settings_file
from app.games.modes import GameMode

_log = get_logger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="f1-race-engineer",
        description="F1 Race Engineer - live telemetry and strategy assistant",
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
        "--mode",
        choices=[m.value for m in GameMode],
        help="game mode to start in (f1_25 / f1_26)",
    )
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
    it only listens."""

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
    if args.mode:
        settings.game_mode = args.mode
    settings.clamped()
    application = Application(settings)
    if args.port:
        # A CLI override applies to the active mode, not globally.
        application.mode_settings.udp_port = args.port
        application._configure_adapter()
    # --port and --mode are overrides for THIS run. Persisting them would
    # mean a one-off `--port 20800` quietly became the saved port and the
    # app then listened somewhere the game is not sending. Same for
    # --selftest, which is a health check, not a configuration step.
    if args.port or args.mode or args.selftest:
        application.persist_on_exit = False
    return application


def run_gui(app: Application) -> int:
    from PySide6.QtWidgets import QApplication

    from app.ui import theme
    from app.ui.main_window import MainWindow

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("F1 Race Engineer")
    qt_app.setOrganizationName("F1RaceEngineer")
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
    print(f"F1 Race Engineer - headless  [{app.game.display_name}]")
    if report.adapter:
        print(f"  Telemetry : {report.adapter.display_name} on UDP {app.mode_settings.udp_port}")
    print("  Press Ctrl+C to stop.")

    while not stop.wait(0.5):
        pass
    return 0


def run_selftest(app: Application) -> int:
    """Prove every subsystem starts and stops cleanly, then exit."""
    import time

    time.sleep(0.6)
    report = app.report()

    adapter = report.adapter
    checks = [
        (
            "Telemetry listener",
            bool(adapter and adapter.running),
            adapter.display_name if adapter else "none",
        ),
        (
            "UDP socket",
            bool(adapter and int(adapter.stage) >= 2),
            f"port {app.mode_settings.udp_port}" if adapter else "-",
        ),
        (
            "Telemetry state",
            report.telemetry is not None,
            f"stale cutoff {app.telemetry.timeout:.1f}s",
        ),
        (
            "Game mode",
            True,
            f"{app.game.display_name}  expects format "
            + "/".join(str(f) for f in app.game.expected_formats),
        ),
        ("Settings", True, str(settings_file())),
    ]

    print("F1 Race Engineer - self test\n")
    for name, ok, detail in checks:
        print(f"  [{'OK' if ok else '--'}]  {name:<20} {detail}")

    if adapter is not None:
        print(f"\n  Pipeline stage: {int(adapter.stage)}/6  {adapter.stage.label}")
        if int(adapter.stage) < 6:
            print("  (start F1 and drive a session to reach stage 6)")

    print()
    if all(ok for _, ok, _ in checks):
        print("All subsystems started correctly.")
        return 0
    print("One or more subsystems failed to start.")
    return 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging(verbose=args.verbose)

    # Diagnosis runs before the Application is built: it must not start the
    # engine, and above all it must not bind the port the app would bind.
    if args.diagnose:
        from app.config.mode_settings import ModeSettings

        settings = AppSettings.load()
        port = args.port or ModeSettings.load(settings.mode).udp_port
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
    except Exception as exc:  # noqa: BLE001
        _log.exception("Fatal error")
        # Launched from a shortcut there is no console to read, so a bare
        # exit code tells the user nothing at all. Say what happened in
        # one plain sentence and point at the log - the traceback belongs
        # in the file, not on screen.
        _report_fatal(exc, gui=not (args.selftest or args.headless))
        return 1
    finally:
        app.shutdown()


def _report_fatal(exc: BaseException, gui: bool) -> None:
    """Tell the user something useful, without ever showing a traceback."""
    log_path = logs_dir() / "f1_race_engineer.log"
    message = (
        "F1 Race Engineer could not continue.\n\n"
        f"{type(exc).__name__}: {exc}\n\n"
        f"The full details were written to:\n{log_path}"
    )
    print(message, file=sys.stderr)
    if not gui:
        return
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        QApplication.instance() or QApplication([])
        QMessageBox.critical(None, "F1 Race Engineer", message)
    except Exception:  # noqa: BLE001
        # Qt itself may be what failed. The stderr message above stands.
        _log.debug("Could not show the error dialog", exc_info=True)


if __name__ == "__main__":
    sys.exit(main())
