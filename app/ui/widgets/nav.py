"""Left navigation sidebar with a live status footer."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QButtonGroup,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.ui import theme


class Sidebar(QWidget):
    """Vertical nav. Emits the index of the page to show."""

    pageSelected = Signal(int)
    modeChanged = Signal(str)

    def __init__(self, items: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(212)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_brand())
        layout.addWidget(self._build_mode_selector())
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

        subtitle = QLabel("F1")
        subtitle.setObjectName("BrandSubtitle")
        layout.addWidget(subtitle)

        title = QLabel("Race Engineer")
        title.setObjectName("BrandTitle")
        layout.addWidget(title)
        return container

    def _build_mode_selector(self) -> QWidget:
        """Game mode picker.

        Lives in the sidebar rather than buried in Settings because it
        changes what the whole application means - which telemetry layout
        is expected, which car/track database is loaded, and which settings
        apply.
        """
        from app.games.modes import GameMode

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(18, 0, 18, 10)
        layout.setSpacing(4)

        caption = QLabel("GAME")
        caption.setObjectName("StatLabel")
        layout.addWidget(caption)

        self._mode_combo = QComboBox()
        for mode in GameMode:
            self._mode_combo.addItem(mode.label, mode.value)
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        layout.addWidget(self._mode_combo)
        return container

    def _on_mode_changed(self) -> None:
        value = self._mode_combo.currentData()
        if value and not getattr(self, "_loading_mode", False):
            self.modeChanged.emit(value)

    def set_mode(self, mode_value: str) -> None:
        """Set without emitting - used when loading the stored mode."""
        index = self._mode_combo.findData(mode_value)
        if index < 0:
            return
        self._loading_mode = True
        self._mode_combo.setCurrentIndex(index)
        self._loading_mode = False

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

        self._detail = QLabel("")
        self._detail.setStyleSheet(
            f"font-size: 11px; color: {theme.TEXT_FAINT};"
        )
        layout.addWidget(self._detail)

        self._status = QLabel("Idle")
        self._status.setStyleSheet(f"font-size: 11px; color: {theme.TEXT_FAINT};")
        layout.addWidget(self._status)
        return container

    def select(self, index: int) -> None:
        button = self._group.button(index)
        if button is not None:
            button.setChecked(True)

    def update_status(self, headline: str, detail: str, status: str) -> None:
        self._profile_label.setText(headline)
        self._detail.setText(detail)
        self._status.setText(status)
