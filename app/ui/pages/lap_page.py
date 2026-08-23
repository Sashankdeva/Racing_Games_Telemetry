"""Lap Analysis - measured pace, sector by sector.

Phase B is measurement only: this page states what happened and never what
to do about it. The coach page consumes the same analysis later.

Every figure carries its confidence, and a number that has not earned the
right to be shown is a dash rather than a precise-looking guess from two
laps.
"""

from __future__ import annotations

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

from app.diagnostics.metrics import DiagnosticsReport
from app.domain.lap_analysis import (
    Confidence,
    LapAnalysis,
    format_delta,
    format_lap_time,
)
from app.ui import theme
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, StatBlock

CONFIDENCE_COLOURS = {
    Confidence.NO_DATA: theme.TEXT_FAINT,
    Confidence.INSUFFICIENT: theme.TEXT_FAINT,
    Confidence.LOW: theme.WARN,
    Confidence.MEDIUM: theme.WARN,
    Confidence.HIGH: theme.LIVE,
}


class LapAnalysisPage(Page):
    title = "Lap Analysis"
    subtitle = "Lap and sector pace, measured from completed laps"

    def build(self) -> None:
        top = QHBoxLayout()
        top.setSpacing(16)
        top.addWidget(self._build_summary(), 3)
        top.addWidget(self._build_sectors(), 2)
        self.body.addLayout(top)

        self.body.addWidget(self._build_laps(), 1)

    # ------------------------------------------------------------------
    def _build_summary(self) -> QWidget:
        card = Card("Session pace")

        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(14)
        self._best = StatBlock("Best Lap", "-")
        self._theoretical = StatBlock("Theoretical Best", "-")
        self._last = StatBlock("Last Lap", "-")
        self._average = StatBlock("Average Pace", "-")
        self._consistency = StatBlock("Consistency", "-", "s")
        self._available = StatBlock("Time Available", "-", "s")
        for index, widget in enumerate(
            (
                self._best, self._theoretical, self._last,
                self._average, self._consistency, self._available,
            )
        ):
            grid.addWidget(widget, index // 3, index % 3)
        card.body.addLayout(grid)

        self._confidence = QLabel("NO DATA")
        self._confidence.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {theme.TEXT_FAINT}; "
            f"letter-spacing: 0.8px;"
        )
        card.body.addWidget(self._confidence)

        self._note = QLabel("Complete a lap to begin measuring pace.")
        self._note.setObjectName("Hint")
        self._note.setWordWrap(True)
        card.body.addWidget(self._note)
        return card

    def _build_sectors(self) -> QWidget:
        card = Card(
            "Last lap vs session best",
            hint="Theoretical best is the sum of these three, not a prediction.",
        )
        self._sector_rows: list[tuple[QLabel, QLabel, QLabel]] = []

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        for column, heading in enumerate(("", "Best", "Last", "Delta")):
            label = QLabel(heading)
            label.setObjectName("StatLabel")
            grid.addWidget(label, 0, column)

        for index in range(3):
            name = QLabel(f"S{index + 1}")
            name.setObjectName("StatLabel")
            grid.addWidget(name, index + 1, 0)

            best = QLabel("-")
            best.setObjectName("Mono")
            grid.addWidget(best, index + 1, 1)

            last = QLabel("-")
            last.setObjectName("Mono")
            grid.addWidget(last, index + 1, 2)

            delta = QLabel("-")
            delta.setObjectName("Mono")
            grid.addWidget(delta, index + 1, 3)

            self._sector_rows.append((best, last, delta))

        card.body.addLayout(grid)
        return card

    def _build_laps(self) -> QWidget:
        card = Card("Completed laps")
        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(
            ["Lap", "Time", "S1", "S2", "S3", "Compound", "Age", "Delta"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        card.body.addWidget(self._table)

        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(card)
        return holder

    # ------------------------------------------------------------------
    def refresh(self, report: DiagnosticsReport) -> None:
        # Reads the cached analysis; it is recomputed on lap completion, not
        # here, so this stays cheap at UI rate.
        analysis = self.app.lap_analysis
        self._show_summary(analysis)
        self._show_sectors(analysis)
        self._show_laps()

    def _show_summary(self, a: LapAnalysis) -> None:
        self._best.set_value(format_lap_time(a.best_lap_s))
        self._last.set_value(format_lap_time(a.last_lap_s))
        self._average.set_value(format_lap_time(a.average_lap_s))
        self._theoretical.set_value(
            format_lap_time(a.theoretical_best_s) if a.theoretical_available else "-"
        )
        # Consistency from a single lap would read as perfect repeatability.
        self._consistency.set_value(
            f"{a.consistency_s:.3f}" if a.valid_laps > 1 else "-"
        )
        self._available.set_value(
            f"{a.time_available_s:.3f}" if a.time_available_s > 0 else "-"
        )

        self._confidence.setText(a.confidence.value)
        self._confidence.setStyleSheet(
            f"font-size: 11px; font-weight: 700; letter-spacing: 0.8px; "
            f"color: {CONFIDENCE_COLOURS[a.confidence]};"
        )

        if not a.has_pace:
            self._note.setText(
                "Complete a lap to begin measuring pace. "
                "Invalid laps are recorded but never set a best."
            )
            return

        parts = [f"{a.valid_laps} valid of {a.laps_recorded} recorded."]
        if a.delta_to_previous_s:
            direction = "slower than" if a.delta_to_previous_s > 0 else "faster than"
            parts.append(
                f"Last lap {abs(a.delta_to_previous_s):.3f}s {direction} the previous."
            )
        if a.time_available_s > 0:
            parts.append(
                f"Your best sectors add up to {format_lap_time(a.theoretical_best_s)} - "
                f"{a.time_available_s:.3f}s better than your best lap."
            )
        self._note.setText(" ".join(parts))

    def _show_sectors(self, a: LapAnalysis) -> None:
        for index, (best_label, last_label, delta_label) in enumerate(self._sector_rows):
            best = a.best_sectors[index]
            best_label.setText(format_lap_time(best.time_s) if best.available else "-")

            delta = a.sector_deltas[index] if index < len(a.sector_deltas) else None
            if delta is None or not delta.available:
                last_label.setText("-")
                delta_label.setText("-")
                delta_label.setStyleSheet("")
                continue

            last_label.setText(format_lap_time(delta.time_s))
            if delta.is_personal_best:
                delta_label.setText("BEST")
                # LIVE, not ACCENT: the accent is the app's red and reads as
                # a warning next to the red loss deltas beside it.
                colour = theme.LIVE
            else:
                delta_label.setText(format_delta(delta.delta_s))
                colour = theme.DANGER if delta.delta_s > 0 else theme.LIVE
            delta_label.setStyleSheet(f"color: {colour}; font-weight: 600;")

    def _show_laps(self) -> None:
        laps = self.app.session.laps
        best = self.app.lap_analysis.best_lap_s

        if self._table.rowCount() != len(laps):
            self._table.setRowCount(len(laps))

        for row, lap in enumerate(laps):
            delta = (
                format_delta(lap.lap_time_s - best)
                if best > 0 and lap.valid_for_pace and lap.lap_time_s != best
                else ("BEST" if lap.lap_time_s == best and lap.valid_for_pace else "-")
            )
            values = (
                str(lap.lap_number),
                format_lap_time(lap.lap_time_s),
                format_lap_time(lap.sector1_s),
                format_lap_time(lap.sector2_s),
                format_lap_time(lap.sector3_s),
                lap.compound or "-",
                str(lap.tyre_age_laps) if lap.tyre_age_laps >= 0 else "-",
                delta,
            )
            for column, text in enumerate(values):
                item = self._table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self._table.setItem(row, column, item)
                if item.text() != text:
                    item.setText(text)
            # An invalid lap is shown but visibly discounted - it is real,
            # it just never sets a record.
            if lap.invalid:
                for column in range(self._table.columnCount()):
                    self._table.item(row, column).setForeground(QColor(theme.TEXT_FAINT))
