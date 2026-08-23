"""Reusable building blocks: cards, status pills, toggles, labelled sliders."""

from __future__ import annotations

from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from app.ui import theme


class Card(QFrame):
    """Rounded surface panel with an optional title and hint."""

    def __init__(
        self,
        title: str = "",
        hint: str = "",
        parent: QWidget | None = None,
        spacing: int = 12,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")

        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(18, 16, 18, 18)
        self._outer.setSpacing(spacing)

        if title:
            header = QHBoxLayout()
            header.setSpacing(8)
            label = QLabel(title.upper())
            label.setObjectName("CardTitle")
            header.addWidget(label)
            header.addStretch(1)
            self._header = header
            self._outer.addLayout(header)

        if hint:
            hint_label = QLabel(hint)
            hint_label.setObjectName("CardHint")
            hint_label.setWordWrap(True)
            self._outer.addWidget(hint_label)

        self.body = QVBoxLayout()
        self.body.setSpacing(spacing)
        self._outer.addLayout(self.body)

    def add_header_widget(self, widget: QWidget) -> None:
        """Place a widget on the right of the card's title row."""
        if hasattr(self, "_header"):
            self._header.addWidget(widget)


class StatusPill(QWidget):
    """Coloured dot plus label - the app's standard state indicator."""

    def __init__(self, text: str = "", colour: str = theme.IDLE, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._colour = QColor(colour)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._dot = _Dot(self._colour)
        layout.addWidget(self._dot)

        self._label = QLabel(text)
        self._label.setStyleSheet(f"font-size: 12px; font-weight: 600; color: {theme.TEXT};")
        layout.addWidget(self._label)
        layout.addStretch(1)

    def set_state(self, text: str, colour: str) -> None:
        self._label.setText(text)
        self._dot.set_colour(QColor(colour))


class _Dot(QWidget):
    def __init__(self, colour: QColor, size: int = 9) -> None:
        super().__init__()
        self._colour = colour
        self._size = size
        self.setFixedSize(size + 4, size + 4)

    def set_colour(self, colour: QColor) -> None:
        if colour != self._colour:
            self._colour = colour
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Soft halo so live states read at a glance without being loud.
        halo = QColor(self._colour)
        halo.setAlpha(55)
        painter.setBrush(halo)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(self.rect())
        painter.setBrush(self._colour)
        inset = 2
        painter.drawEllipse(self.rect().adjusted(inset, inset, -inset, -inset))


class ToggleSwitch(QCheckBox):
    """Sliding on/off switch. Custom-painted - a stock checkbox looks like a
    settings dialog, and this app should not."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(42, 22)
        self._offset = 0.0
        self._animation = QPropertyAnimation(self, b"offset", self)
        self._animation.setDuration(140)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, checked: bool) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._offset)
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()

    def get_offset(self) -> float:
        return self._offset

    def set_offset(self, value: float) -> None:
        self._offset = value
        self.update()

    offset = Property(float, get_offset, set_offset)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        enabled = self.isEnabled()
        track = QColor(theme.ACCENT if self.isChecked() else theme.BORDER)
        if not enabled:
            track.setAlpha(90)

        radius = self.height() / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(self.rect(), radius, radius)

        knob_size = self.height() - 6
        travel = self.width() - knob_size - 6
        x = 3 + travel * self._offset
        knob = QColor(theme.TEXT if enabled else theme.TEXT_FAINT)
        painter.setBrush(knob)
        painter.drawEllipse(int(x), 3, knob_size, knob_size)

    def hitButton(self, pos) -> bool:  # noqa: N802
        return self.rect().contains(pos)


class LabeledSlider(QWidget):
    """Slider with a title, live value readout and optional description.

    Works in float space while Qt's slider works in integers, so all
    conversion is contained here rather than repeated on every page.
    """

    valueChanged = Signal(float)

    def __init__(
        self,
        title: str,
        minimum: float = 0.0,
        maximum: float = 1.0,
        value: float = 0.5,
        step: float = 0.01,
        suffix: str = "",
        description: str = "",
        decimals: int = 2,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._min = minimum
        self._max = maximum
        self._step = step
        self._suffix = suffix
        self._decimals = decimals
        self._emitting = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(8)
        self._title = QLabel(title)
        self._title.setStyleSheet(f"font-size: 12px; font-weight: 600; color: {theme.TEXT};")
        header.addWidget(self._title)
        header.addStretch(1)
        self._value_label = QLabel()
        self._value_label.setObjectName("ValueLabel")
        header.addWidget(self._value_label)
        layout.addLayout(header)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setMinimum(0)
        self._slider.setMaximum(self._steps())
        self._slider.valueChanged.connect(self._on_slider)
        layout.addWidget(self._slider)

        if description:
            hint = QLabel(description)
            hint.setObjectName("Hint")
            hint.setWordWrap(True)
            layout.addWidget(hint)

        self.set_value(value)

    def _steps(self) -> int:
        return max(1, round((self._max - self._min) / self._step))

    def _on_slider(self, position: int) -> None:
        value = self._min + position * self._step
        self._value_label.setText(self._format(value))
        if not self._emitting:
            self.valueChanged.emit(value)

    def _format(self, value: float) -> str:
        return f"{value:.{self._decimals}f}{self._suffix}"

    def value(self) -> float:
        return self._min + self._slider.value() * self._step

    def set_value(self, value: float) -> None:
        """Set without emitting - used when loading a profile into the UI."""
        value = max(self._min, min(self._max, value))
        position = round((value - self._min) / self._step)
        self._emitting = True
        self._slider.setValue(position)
        self._emitting = False
        self._value_label.setText(self._format(self.value()))

    def set_enabled(self, enabled: bool) -> None:
        self._slider.setEnabled(enabled)
        self._title.setEnabled(enabled)
        self._value_label.setEnabled(enabled)


class Divider(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Divider")
        self.setFixedHeight(1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


class StatBlock(QWidget):
    """Small label-over-value stat, used across the dashboard."""

    def __init__(
        self,
        label: str,
        value: str = "-",
        unit: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        self._label = QLabel(label.upper())
        self._label.setObjectName("StatLabel")
        layout.addWidget(self._label)

        row = QHBoxLayout()
        row.setSpacing(4)
        row.setContentsMargins(0, 0, 0, 0)
        self._value = QLabel(value)
        self._value.setObjectName("BigValue")
        row.addWidget(self._value)
        if unit:
            unit_label = QLabel(unit)
            unit_label.setObjectName("BigValueUnit")
            row.addWidget(unit_label, 0, Qt.AlignmentFlag.AlignBottom)
        row.addStretch(1)
        layout.addLayout(row)

    def set_value(self, value: str) -> None:
        if self._value.text() != value:
            self._value.setText(value)

    def set_label(self, label: str) -> None:
        """Captions can be game-specific (DRS vs Manual Override)."""
        self._label.setText(label.upper())


class FieldRow(QWidget):
    """Label on the left, control on the right - the standard settings row."""

    def __init__(
        self,
        label: str,
        control: QWidget,
        description: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        text = QVBoxLayout()
        text.setSpacing(2)
        title = QLabel(label)
        title.setStyleSheet(f"font-size: 12px; font-weight: 600; color: {theme.TEXT};")
        text.addWidget(title)
        if description:
            hint = QLabel(description)
            hint.setObjectName("Hint")
            hint.setWordWrap(True)
            text.addWidget(hint)
        layout.addLayout(text, 1)
        layout.addWidget(control, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
