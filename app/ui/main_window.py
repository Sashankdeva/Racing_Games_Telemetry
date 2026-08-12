"""Main window: sidebar, page stack, and the single refresh timer.

One timer drives the whole UI and it only refreshes the page that is
actually visible, so hidden pages cost nothing. The timer runs at 30 Hz -
fast enough for the meters to look live, slow enough to stay far away from
the 120 Hz haptic thread it is merely observing.

The UI never touches the haptic loop: it polls an immutable snapshot.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QStackedWidget,
    QSystemTrayIcon,
    QWidget,
)

from app.core.application import Application
from app.core.logging import get_logger
from app.ui import theme
from app.ui.pages import PAGE_CLASSES
from app.ui.widgets.nav import Sidebar

_log = get_logger(__name__)

UI_REFRESH_HZ = 30


class MainWindow(QMainWindow):
    def __init__(self, app: Application) -> None:
        super().__init__()
        self.app = app
        self._quitting = False

        self.setWindowTitle("Racing Haptic Engine")
        self.setMinimumSize(1180, 760)
        self.resize(1380, 880)
        self.setWindowIcon(_build_icon())

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._pages = [cls(app) for cls in PAGE_CLASSES]

        self.sidebar = Sidebar([page.title for page in self._pages])
        self.sidebar.pageSelected.connect(self._on_page_selected)
        layout.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        for page in self._pages:
            self.stack.addWidget(page)
        layout.addWidget(self.stack, 1)

        self.setCentralWidget(central)

        self._tray = self._build_tray()

        self._timer = QTimer(self)
        self._timer.setInterval(int(1000 / UI_REFRESH_HZ))
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

        self.sidebar.select(0)
        self._on_page_selected(0)

    # ------------------------------------------------------------------
    def _build_tray(self) -> QSystemTrayIcon | None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None

        tray = QSystemTrayIcon(_build_icon(), self)
        tray.setToolTip("Racing Haptic Engine")

        menu = QMenu()
        show_action = QAction("Show Window", self)
        show_action.triggered.connect(self._restore_window)
        menu.addAction(show_action)

        stop_action = QAction("Emergency Stop", self)
        stop_action.triggered.connect(self.app.emergency_stop)
        menu.addAction(stop_action)

        menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        return tray

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._restore_window()

    def _restore_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------------
    def _on_page_selected(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        page = self._pages[index]
        page.on_shown()
        page.refresh(self.app.report())

    def reload_pages(self) -> None:
        """Re-read the active profile into every page (after a profile switch)."""
        for page in self._pages:
            page.on_shown()

    def _refresh(self) -> None:
        try:
            report = self.app.report()
        except Exception:  # noqa: BLE001 - a UI hiccup must not stop the timer
            _log.exception("Diagnostics collection failed")
            return

        engine = report.engine
        if engine.emergency_stop:
            status = "EMERGENCY STOP"
        elif not report.controller_connected:
            status = "No controller"
        elif report.adapter and report.adapter.connected:
            status = "Telemetry live"
        elif engine.running:
            status = "Running"
        else:
            status = "Idle"

        self.sidebar.update_status(
            self.app.profiles.active.name, engine.left, engine.right, status
        )

        current = self.stack.currentWidget()
        if current is not None:
            try:
                current.refresh(report)
            except Exception:  # noqa: BLE001
                _log.exception("Page refresh failed for %s", type(current).__name__)

    # ------------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """Close to tray if configured, otherwise shut down properly."""
        if not self._quitting and self.app.settings.minimize_to_tray and self._tray is not None:
            event.ignore()
            self.hide()
            self._tray.showMessage(
                "Racing Haptic Engine",
                "Still running in the background. Right-click the tray icon to quit.",
                QSystemTrayIcon.MessageIcon.Information,
                2500,
            )
            return

        self._timer.stop()
        self.app.shutdown()
        if self._tray is not None:
            self._tray.hide()
        event.accept()

    def _quit(self) -> None:
        self._quitting = True
        self.close()
        from PySide6.QtWidgets import QApplication

        QApplication.quit()


def _build_icon() -> QIcon:
    """Draw the app icon in code so there is no binary asset to ship."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)

    from PySide6.QtGui import QColor

    painter.setBrush(QColor(theme.SURFACE_ALT))
    painter.drawRoundedRect(2, 2, 60, 60, 14, 14)

    # Three bars rising left to right - the engine "revving up" motif.
    painter.setBrush(QColor(theme.LIVE))
    painter.drawRoundedRect(14, 36, 9, 14, 4, 4)
    painter.setBrush(QColor(theme.WARN))
    painter.drawRoundedRect(27, 26, 9, 24, 4, 4)
    painter.setBrush(QColor(theme.ACCENT))
    painter.drawRoundedRect(40, 14, 9, 36, 4, 4)
    painter.end()

    return QIcon(pixmap)
