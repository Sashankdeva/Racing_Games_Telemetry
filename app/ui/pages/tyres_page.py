"""Tyres - live tyre state and per-stint degradation.

Phase 2. Degradation is shown per stint, never per session: a fresh set
resets the clock, so a session-wide slope averages across tyre changes and
describes nothing real.

Where the data does not support a figure, this page says INSUFFICIENT DATA
rather than showing a number that looks precise.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.telemetry_state import TelemetryStatus
from app.diagnostics.metrics import DiagnosticsReport
from app.domain.lap_analysis import Confidence, format_lap_time
from app.ui import theme
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, StatBlock
from app.ui.widgets.meters import TyreGrid

CONFIDENCE_COLOURS = {
    Confidence.NO_DATA: theme.TEXT_FAINT,
    Confidence.INSUFFICIENT: theme.TEXT_FAINT,
    Confidence.LOW: theme.WARN,
    Confidence.MEDIUM: theme.WARN,
    Confidence.HIGH: theme.LIVE,
}


class TyresPage(Page):
    title = "Tyres"
    subtitle = "Tyre state and stint degradation, measured from completed laps"

    def build(self) -> None:
        top = QHBoxLayout()
        top.setSpacing(16)
        top.addWidget(self._build_current(), 2)
        top.addWidget(self._build_corners(), 3)
        self.body.addLayout(top)

        self.body.addWidget(self._build_stints(), 1)

    # ------------------------------------------------------------------
    def _build_current(self) -> QWidget:
        card = Card("Current stint")

        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(14)
        self._compound = StatBlock("Compound", "-")
        self._age = StatBlock("Tyre Age", "-", "laps")
        self._stint_laps = StatBlock("Stint Length", "-", "laps")
        self._wear = StatBlock("Wear", "-", "%")
        for index, widget in enumerate(
            (self._compound, self._age, self._stint_laps, self._wear)
        ):
            grid.addWidget(widget, index // 2, index % 2)
        card.body.addLayout(grid)

        self._degradation = StatBlock("Degradation", "-")
        card.body.addWidget(self._degradation)

        self._confidence = QLabel(Confidence.NO_DATA.value)
        self._confidence.setStyleSheet(
            f"font-size: 11px; font-weight: 700; letter-spacing: 0.8px; "
            f"color: {theme.TEXT_FAINT};"
        )
        card.body.addWidget(self._confidence)

        self._note = QLabel("Complete laps on a set of tyres to measure degradation.")
        self._note.setObjectName("Hint")
        self._note.setWordWrap(True)
        card.body.addWidget(self._note)
        return card

    def _build_corners(self) -> QWidget:
        card = Card(
            "Per corner",
            hint="Live from telemetry: surface temperature, pressure and wear.",
        )
        self._grid = TyreGrid()
        card.body.addWidget(self._grid)
        self._corner_note = QLabel("")
        self._corner_note.setObjectName("Hint")
        self._corner_note.setWordWrap(True)
        card.body.addWidget(self._corner_note)
        return card

    def _build_stints(self) -> QWidget:
        card = Card(
            "Stints",
            hint="Each set of tyres is measured on its own - a fresh set resets the clock.",
        )
        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(
            ["#", "Compound", "Laps", "Age", "Clean", "Best", "Degradation", "Confidence"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        card.body.addWidget(self._table)

        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(card)
        return holder

    # ------------------------------------------------------------------
    def refresh(self, report: DiagnosticsReport) -> None:
        self._show_current()
        self._show_corners(report)
        self._show_stints()

    def _show_current(self) -> None:
        state = self.app.tyres

        self._compound.set_value(state.compound or "-")
        self._age.set_value(str(state.age_laps) if state.age_laps >= 0 else "-")
        self._stint_laps.set_value(str(state.stint_laps) if state.stint_laps else "-")
        self._wear.set_value(f"{state.wear_pct:.1f}" if state.wear_pct else "-")
        self._degradation.set_value(state.describe_degradation())

        confidence = state.degradation_confidence
        self._confidence.setText(confidence.value)
        self._confidence.setStyleSheet(
            f"font-size: 11px; font-weight: 700; letter-spacing: 0.8px; "
            f"color: {CONFIDENCE_COLOURS[confidence]};"
        )

        if not self.app.stints:
            self._note.setText("Complete laps on a set of tyres to measure degradation.")
            return

        stint = self.app.stints[-1]
        parts = [f"Stint {stint.number}: {stint.label()}."]
        if stint.started_used:
            parts.append(
                f"Started on a scrubbed set ({stint.start_age_laps} laps on it)."
            )
        laps_word = "lap" if stint.clean_laps == 1 else "laps"
        if confidence.is_usable:
            parts.append(
                f"Measured over {stint.clean_laps} clean {laps_word} of this stint."
            )
        else:
            parts.append(
                f"{stint.clean_laps} clean {laps_word} so far - not enough to fit a "
                "degradation trend."
            )
        self._note.setText(" ".join(parts))

    def _show_corners(self, report: DiagnosticsReport) -> None:
        if report.status is TelemetryStatus.NO_DATA:
            self._grid.clear()
            self._corner_note.setText("No telemetry received yet.")
            return
        frame = report.frame
        if report.stale:
            # The tyres did not vanish when the packets stopped.
            self._corner_note.setText(
                f"STALE - last update {report.age:.1f}s ago."
            )
            self._grid.set_values(
                frame.tyre_surface_temp, frame.tyre_pressure, frame.tyre_wear
            )
            return
        self._grid.set_values(frame.tyre_surface_temp, frame.tyre_pressure, frame.tyre_wear)
        if not any(frame.tyre_pressure.as_tuple()):
            # F1 26 moved the thermal block; the parser refuses to guess.
            self._corner_note.setText(
                "Tyre temperature and pressure are UNAVAILABLE for this packet format."
            )
        else:
            self._corner_note.setText("")

    def _show_stints(self) -> None:
        stints = self.app.stints
        if self._table.rowCount() != len(stints):
            self._table.setRowCount(len(stints))

        for row, stint in enumerate(stints):
            confidence = stint.degradation_confidence
            values = (
                str(stint.number),
                stint.compound or "-",
                f"L{stint.first_lap}-{stint.last_lap}",
                str(stint.current_age_laps) if stint.current_age_laps >= 0 else "-",
                str(stint.clean_laps),
                format_lap_time(stint.best_lap_s),
                stint.describe_degradation(),
                confidence.value,
            )
            for column, text in enumerate(values):
                item = self._table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    self._table.setItem(row, column, item)
                if item.text() != text:
                    item.setText(text)

            self._table.item(row, 7).setForeground(QColor(CONFIDENCE_COLOURS[confidence]))
            # A figure without usable confidence must not read as a result.
            self._table.item(row, 6).setForeground(
                QColor(theme.TEXT if confidence.is_usable else theme.TEXT_FAINT)
            )
