"""Live visualisation: motor meters, the haptic scope, and an RPM bar.

These are the widgets that have to be readable at a glance while racing, so
they favour large shapes and colour transitions over numbers. All of them
are fed by polling an EngineSnapshot - none subscribe to the haptic thread.
"""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from app.ui import theme


def _intensity_colour(value: float) -> QColor:
    """Teal -> amber -> red as intensity climbs."""
    if value <= 0.5:
        t = value / 0.5
        return _blend(QColor(theme.METER_LOW), QColor(theme.METER_MID), t)
    t = (value - 0.5) / 0.5
    return _blend(QColor(theme.METER_MID), QColor(theme.METER_HIGH), t)


def _blend(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        int(a.red() + (b.red() - a.red()) * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue() + (b.blue() - a.blue()) * t),
    )


class MotorMeter(QWidget):
    """Horizontal bar for one motor, with a decaying peak marker.

    The peak marker matters: raw output changes far faster than the eye can
    track, so without it a strong 30 ms transient would flash past unseen.
    """

    PEAK_DECAY = 0.9

    def __init__(self, label: str = "LEFT", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._label = label
        self._value = 0.0
        self._peak = 0.0
        self.setMinimumHeight(38)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, value: float) -> None:
        self._value = max(0.0, min(1.0, value))
        self._peak = max(self._value, self._peak * self.PEAK_DECAY)
        if self._peak < 0.001:
            self._peak = 0.0
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        width = self.width()
        bar_top = 16
        bar_height = self.height() - bar_top
        radius = bar_height / 2

        painter.setPen(QColor(theme.TEXT_FAINT))
        font = painter.font()
        font.setPointSize(8)
        font.setBold(True)
        painter.setFont(font)
        # Wide enough for the longest label ("RIGHT MOTOR") without clipping.
        painter.drawText(
            0, 0, width - 60, 12,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._label,
        )
        painter.drawText(
            width - 60, 0, 60, 12,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"{self._value * 100:.0f}%",
        )

        track = QRectF(0, bar_top, width, bar_height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.SURFACE_ALT))
        painter.drawRoundedRect(track, radius, radius)

        if self._value > 0.001:
            fill_width = max(bar_height, width * self._value)
            fill = QRectF(0, bar_top, fill_width, bar_height)
            gradient = QLinearGradient(0, 0, fill_width, 0)
            gradient.setColorAt(0.0, QColor(theme.METER_LOW))
            gradient.setColorAt(0.55, QColor(theme.METER_MID))
            gradient.setColorAt(1.0, _intensity_colour(self._value))
            painter.setBrush(gradient)
            painter.drawRoundedRect(fill, radius, radius)

        if self._peak > 0.01:
            x = max(bar_height, width * self._peak)
            pen = QPen(QColor(theme.TEXT), 2)
            painter.setPen(pen)
            painter.drawLine(QPointF(x - 1, bar_top + 2), QPointF(x - 1, bar_top + bar_height - 2))


class HapticScope(QWidget):
    """Scrolling history of both motors - the live haptic visualisation.

    Shows shape over time, which is what actually distinguishes effects: a
    gear shift spike, the sawtooth of kerbs and the dense band of high-rev
    engine all look different here even when their peak levels match.
    """

    def __init__(self, history: int = 220, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._left: deque[float] = deque([0.0] * history, maxlen=history)
        self._right: deque[float] = deque([0.0] * history, maxlen=history)
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def push(self, left: float, right: float) -> None:
        self._left.append(max(0.0, min(1.0, left)))
        self._right.append(max(0.0, min(1.0, right)))
        self.update()

    def clear(self) -> None:
        for buffer in (self._left, self._right):
            for _ in range(len(buffer)):
                buffer.append(0.0)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect().adjusted(0, 0, -1, -1)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.BG))
        painter.drawRoundedRect(rect, 8, 8)

        # Reference gridlines at 25/50/75%.
        grid = QPen(QColor(theme.BORDER), 1, Qt.PenStyle.DashLine)
        painter.setPen(grid)
        for fraction in (0.25, 0.5, 0.75):
            y = rect.bottom() - rect.height() * fraction
            painter.drawLine(rect.left() + 6, int(y), rect.right() - 6, int(y))

        self._draw_trace(painter, rect, self._left, QColor(theme.METER_LOW))
        self._draw_trace(painter, rect, self._right, QColor(theme.ACCENT))

        painter.setPen(QColor(theme.TEXT_FAINT))
        font = painter.font()
        font.setPointSize(8)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect.adjusted(10, 6, -10, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, "LEFT")
        painter.setPen(QColor(theme.ACCENT))
        painter.drawText(rect.adjusted(10, 6, -10, 0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, "RIGHT")

    def _draw_trace(self, painter: QPainter, rect, samples: deque[float], colour: QColor) -> None:
        if not samples:
            return

        count = len(samples)
        left = rect.left() + 6
        usable_width = rect.width() - 12
        bottom = rect.bottom() - 6
        usable_height = rect.height() - 24
        step = usable_width / max(1, count - 1)

        path = QPainterPath()
        fill = QPainterPath()
        fill.moveTo(left, bottom)

        for index, value in enumerate(samples):
            x = left + index * step
            y = bottom - value * usable_height
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
            fill.lineTo(x, y)

        fill.lineTo(left + (count - 1) * step, bottom)
        fill.closeSubpath()

        shade = QColor(colour)
        shade.setAlpha(38)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shade)
        painter.drawPath(fill)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(colour, 1.6))
        painter.drawPath(path)


class RpmBar(QWidget):
    """Segmented rev bar that turns red near the limiter."""

    SEGMENTS = 22

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ratio = 0.0
        self.setMinimumHeight(26)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_ratio(self, ratio: float) -> None:
        ratio = max(0.0, min(1.0, ratio))
        if abs(ratio - self._ratio) > 0.002:
            self._ratio = ratio
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        gap = 3
        total = self.width()
        segment_width = (total - gap * (self.SEGMENTS - 1)) / self.SEGMENTS
        lit = int(self._ratio * self.SEGMENTS + 0.5)

        for index in range(self.SEGMENTS):
            x = index * (segment_width + gap)
            position = index / (self.SEGMENTS - 1)
            if index < lit:
                if position > 0.88:
                    colour = QColor(theme.DANGER)
                elif position > 0.68:
                    colour = QColor(theme.ACCENT)
                elif position > 0.4:
                    colour = QColor(theme.WARN)
                else:
                    colour = QColor(theme.LIVE)
            else:
                colour = QColor(theme.SURFACE_ALT)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            painter.drawRoundedRect(QRectF(x, 0, segment_width, self.height()), 2, 2)


class ActivityDot(QWidget):
    """Compact pulse indicator used in the sidebar footer."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._level = 0.0
        self.setFixedSize(10, 10)

    def set_level(self, level: float) -> None:
        level = max(0.0, min(1.0, level))
        if abs(level - self._level) > 0.01:
            self._level = level
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colour = _intensity_colour(self._level) if self._level > 0.01 else QColor(theme.IDLE)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        painter.drawEllipse(self.rect())
