"""Left navigation sidebar with a live status footer."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.ui import theme
from app.ui.widgets.meters import ActivityDot


class Sidebar(QWidget):
    """Vertical nav. Emits the index of the page to show."""

    pageSelected = Signal(int)

    def __init__(self, items: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(212)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_brand())
        layout.addSpacing(6)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for index, name in enumerate(items):
            button = QPushButton(name)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._group.addButton(button, index)
            layout.addWidget(button)

        self._group.idClicked.connect(self.pageSelected.emit)
        layout.addStretch(1)
        layout.addWidget(self._build_footer())

    def _build_brand(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(18, 20, 18, 14)
        layout.setSpacing(2)

        subtitle = QLabel("RACING")
        subtitle.setObjectName("BrandSubtitle")
        layout.addWidget(subtitle)

        title = QLabel("Haptic Engine")
        title.setObjectName("BrandTitle")
        layout.addWidget(title)
        return container

    def _build_footer(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(18, 12, 18, 16)
        layout.setSpacing(8)

        self._profile_label = QLabel("Default")
        self._profile_label.setStyleSheet(
            f"font-size: 11px; font-weight: 600; color: {theme.TEXT_DIM};"
        )
        layout.addWidget(self._profile_label)

        activity = QHBoxLayout()
        activity.setSpacing(6)
        self._left_dot = ActivityDot()
        self._right_dot = ActivityDot()
        activity.addWidget(self._left_dot)
        activity.addWidget(self._right_dot)

        self._status = QLabel("Idle")
        self._status.setStyleSheet(f"font-size: 11px; color: {theme.TEXT_FAINT};")
        activity.addWidget(self._status)
        activity.addStretch(1)
        layout.addLayout(activity)
        return container

    def select(self, index: int) -> None:
        button = self._group.button(index)
        if button is not None:
            button.setChecked(True)

    def update_status(self, profile: str, left: float, right: float, status: str) -> None:
        self._profile_label.setText(profile)
        self._left_dot.set_level(left)
        self._right_dot.set_level(right)
        self._status.setText(status)
