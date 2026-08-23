"""Live telemetry visualisation.

Widgets that must be readable at a glance while racing, so they favour
large shapes and colour transitions over numbers. All are fed by polling a
telemetry snapshot - none subscribe to the telemetry thread.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from app.ui import theme


def _blend(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        int(a.red() + (b.red() - a.red()) * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue() + (b.blue() - a.blue()) * t),
    )


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
        segment_width = (self.width() - gap * (self.SEGMENTS - 1)) / self.SEGMENTS
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


class InputBar(QWidget):
    """Horizontal bar for a driver input (throttle / brake / steering).

    `bipolar` centres the fill for steering, which swings both ways - a
    left-anchored bar would wrongly imply that full-left is "zero".
    """

    def __init__(
        self,
        label: str,
        colour: str,
        bipolar: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._label = label
        self._colour = QColor(colour)
        self._bipolar = bipolar
        self._value = 0.0
        self.setMinimumHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, value: float) -> None:
        low = -1.0 if self._bipolar else 0.0
        self._value = max(low, min(1.0, value))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        width, top = self.width(), 14
        height = self.height() - top
        radius = height / 2

        painter.setPen(QColor(theme.TEXT_FAINT))
        font = painter.font()
        font.setPointSize(8)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            0, 0, width - 60, 11,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label,
        )
        readout = f"{self._value:+.2f}" if self._bipolar else f"{self._value * 100:.0f}%"
        painter.drawText(
            width - 60, 0, 60, 11,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, readout,
        )

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.SURFACE_ALT))
        painter.drawRoundedRect(QRectF(0, top, width, height), radius, radius)

        if abs(self._value) < 0.005:
            return

        painter.setBrush(self._colour)
        if self._bipolar:
            centre = width / 2
            span = (width / 2) * self._value
            x = centre if span >= 0 else centre + span
            painter.drawRoundedRect(QRectF(x, top, abs(span), height), radius, radius)
        else:
            painter.drawRoundedRect(
                QRectF(0, top, max(height, width * self._value), height), radius, radius
            )


class TyreGrid(QWidget):
    """Four-corner tyre readout: surface temperature, pressure and wear."""

    #: Surface temperature window, used only for colouring.
    COLD_C = 80.0
    HOT_C = 110.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._temp = None
        self._pressure = None
        self._wear = None
        self.setMinimumHeight(124)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_values(self, temp, pressure, wear) -> None:
        self._temp, self._pressure, self._wear = temp, pressure, wear
        self.update()

    def clear(self) -> None:
        self._temp = self._pressure = self._wear = None
        self.update()

    def _temp_colour(self, celsius: float) -> QColor:
        if celsius <= 0:
            return QColor(theme.IDLE)
        if celsius < self.COLD_C:
            return _blend(
                QColor(theme.LIVE), QColor(theme.METER_MID), celsius / max(1.0, self.COLD_C)
            )
        span = max(1.0, self.HOT_C - self.COLD_C)
        return _blend(
            QColor(theme.METER_MID), QColor(theme.DANGER), (celsius - self.COLD_C) / span
        )

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        cell_w = self.width() / 2 - 6
        cell_h = self.height() / 2 - 6

        values = None
        if self._temp is not None:
            values = {
                "FL": (self._temp.fl, self._pressure.fl, self._wear.fl),
                "FR": (self._temp.fr, self._pressure.fr, self._wear.fr),
                "RL": (self._temp.rl, self._pressure.rl, self._wear.rl),
                "RR": (self._temp.rr, self._pressure.rr, self._wear.rr),
            }

        font = painter.font()
        for name, col, row in (("FL", 0, 0), ("FR", 1, 0), ("RL", 0, 1), ("RR", 1, 1)):
            x = col * (cell_w + 12)
            y = row * (cell_h + 12)
            rect = QRectF(x, y, cell_w, cell_h)

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.SURFACE_ALT))
            painter.drawRoundedRect(rect, 6, 6)

            temp = values[name][0] if values else 0.0
            if values:
                painter.setBrush(self._temp_colour(temp))
                painter.drawRoundedRect(QRectF(x, y + cell_h - 4, cell_w, 4), 2, 2)

            painter.setPen(QColor(theme.TEXT_FAINT))
            font.setPointSize(8)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(
                rect.adjusted(8, 5, -8, 0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, name,
            )

            painter.setPen(QColor(theme.TEXT))
            font.setPointSize(11)
            painter.setFont(font)
            painter.drawText(
                rect.adjusted(8, 2, -8, -6),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"{temp:.0f} C" if values else "-",
            )

            if values:
                _, pressure, wear = values[name]
                painter.setPen(QColor(theme.TEXT_DIM))
                font.setPointSize(8)
                font.setBold(False)
                painter.setFont(font)
                painter.drawText(
                    rect.adjusted(8, 0, -8, -8),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                    f"{pressure:.1f} psi   wear {wear:.0f}%",
                )


class BatteryMeter(QWidget):
    """ERS store as a segmented battery, coloured by deploy mode.

    Two visual modes, because the same charge level means different things
    depending on what the car is doing with it:

      * NORMAL    - charge shown in the standard live colour
      * OVERTAKE  - the whole meter switches to the attack colour, so a
                    glance tells the driver energy is being spent hard
                    rather than managed

    Charge level is telemetry; the mode is telemetry. Neither is inferred.
    An empty mode string (game not reporting it) renders as normal, never
    as overtake.
    """

    SEGMENTS = 12
    #: Deploy mode that switches the meter to attack colours. Matched
    #: case-insensitively because the label comes from the game.
    OVERTAKE_MODE = "overtake"
    #: Below this fraction the meter warns regardless of mode - out of
    #: energy is worth noticing whatever the deploy setting says.
    LOW_CHARGE = 0.15

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ratio = 0.0
        self._mode = ""
        self._available = False
        self.setMinimumHeight(22)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_state(self, percent: float, mode: str, available: bool = True) -> None:
        ratio = max(0.0, min(1.0, percent / 100.0))
        mode = mode or ""
        if (
            abs(ratio - self._ratio) > 0.002
            or mode != self._mode
            or available != self._available
        ):
            self._ratio = ratio
            self._mode = mode
            self._available = available
            self.update()

    def clear(self) -> None:
        self.set_state(0.0, "", available=False)

    @property
    def overtaking(self) -> bool:
        return self._mode.strip().lower() == self.OVERTAKE_MODE

    def charge_colour(self) -> QColor:
        """The colour the lit segments are drawn in."""
        if not self._available:
            return QColor(theme.SURFACE_ALT)
        if self.overtaking:
            return QColor(theme.ACCENT)
        if self._ratio <= self.LOW_CHARGE:
            return QColor(theme.WARN)
        return QColor(theme.LIVE)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        gap = 3
        # Leave room for the battery "terminal" on the right so the shape
        # reads as a battery rather than another progress bar.
        terminal = 5
        body_width = self.width() - terminal - 4
        segment_width = (body_width - gap * (self.SEGMENTS - 1)) / self.SEGMENTS
        lit = int(self._ratio * self.SEGMENTS + 0.5)
        colour = self.charge_colour()

        painter.setPen(Qt.PenStyle.NoPen)
        for index in range(self.SEGMENTS):
            x = index * (segment_width + gap)
            painter.setBrush(colour if index < lit else QColor(theme.SURFACE_ALT))
            painter.drawRoundedRect(
                QRectF(x, 0, segment_width, self.height()), 2, 2
            )

        terminal_colour = QColor(colour if self._available else theme.SURFACE_ALT)
        painter.setBrush(terminal_colour)
        painter.drawRoundedRect(
            QRectF(
                body_width + 4,
                self.height() * 0.28,
                terminal,
                self.height() * 0.44,
            ),
            1.5,
            1.5,
        )
