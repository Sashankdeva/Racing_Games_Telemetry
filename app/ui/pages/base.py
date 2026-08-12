"""Page shell.

Every page gets the same header treatment and a scrollable body, and every
page implements `refresh()`. The main window only refreshes the *visible*
page, so hidden pages cost nothing at 30 Hz.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core.application import Application
from app.diagnostics.metrics import DiagnosticsReport


class Page(QWidget):
    title: str = "Page"
    subtitle: str = ""

    def __init__(self, app: Application, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget()
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(32, 26, 32, 18)
        header_layout.setSpacing(4)

        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        title_label = QLabel(self.title)
        title_label.setObjectName("PageTitle")
        title_row.addWidget(title_label)
        title_row.addStretch(1)
        self.header_actions = title_row
        header_layout.addLayout(title_row)

        if self.subtitle:
            subtitle_label = QLabel(self.subtitle)
            subtitle_label.setObjectName("PageSubtitle")
            subtitle_label.setWordWrap(True)
            header_layout.addWidget(subtitle_label)

        outer.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        self.body = QVBoxLayout(container)
        self.body.setContentsMargins(32, 0, 32, 32)
        self.body.setSpacing(16)

        scroll.setWidget(container)
        outer.addWidget(scroll, 1)

        self.build()

    def build(self) -> None:
        """Construct page content. Subclasses override."""

    def refresh(self, report: DiagnosticsReport) -> None:
        """Called on a timer while this page is visible."""

    def on_shown(self) -> None:
        """Called when the page becomes visible - reload from the profile."""
