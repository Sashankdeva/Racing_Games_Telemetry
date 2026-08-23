"""Driver Coach - where time is going, and whether it is getting better.

Renders what `app.domain.driver_coach` produced. No analysis here.

The page leads with a single focus, because a driver reading a list mid-
session reads nothing. Everything else - lap comparison, per-sector trend,
and the history of problems - sits below it.
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
from app.domain.driver_coach import EvidenceKind, Severity, Status
from app.ui import theme
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, StatBlock

SEVERITY_COLOURS = {
    Severity.INFO: theme.LIVE,
    Severity.ADVISORY: theme.WARN,
    Severity.WARNING: theme.DANGER,
}

STATUS_COLOURS = {
    Status.ACTIVE: theme.WARN,
    Status.IMPROVING: theme.LIVE,
    Status.RESOLVED: theme.TEXT_FAINT,
}

TREND_COLOURS = {
    "IMPROVING": theme.LIVE,
    "DECLINING": theme.DANGER,
    "STABLE": theme.TEXT,
    "UNKNOWN": theme.TEXT_FAINT,
}


class CoachPage(Page):
    title = "Driver"
    subtitle = "Where time is going, and whether it is improving"

    MAX_HISTORY = 20

    def build(self) -> None:
        top = QHBoxLayout()
        top.setSpacing(16)
        top.addWidget(self._build_focus(), 3)
        top.addWidget(self._build_comparison(), 2)
        self.body.addLayout(top)

        self.body.addWidget(self._build_trends())
        self.body.addWidget(self._build_history(), 1)

    # ------------------------------------------------------------------
    def _build_focus(self) -> QWidget:
        card = Card("Current focus")

        self._headline = QLabel("Complete a few laps to begin coaching.")
        self._headline.setWordWrap(True)
        self._headline.setStyleSheet(
            f"font-size: 17px; font-weight: 600; color: {theme.TEXT};"
        )
        card.body.addWidget(self._headline)

        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(12)
        self._region = StatBlock("Region", "-")
        self._loss = StatBlock("Potential Loss", "-", "s")
        self._repeat = StatBlock("Repeated", "-")
        self._confidence = StatBlock("Confidence", "-")
        for index, widget in enumerate(
            (self._region, self._loss, self._repeat, self._confidence)
        ):
            grid.addWidget(widget, index // 2, index % 2)
        card.body.addLayout(grid)

        evidence_label = QLabel("EVIDENCE")
        evidence_label.setObjectName("StatLabel")
        card.body.addWidget(evidence_label)

        self._evidence = QLabel("")
        self._evidence.setObjectName("Hint")
        self._evidence.setWordWrap(True)
        card.body.addWidget(self._evidence)

        self._kind = QLabel("")
        self._kind.setStyleSheet(
            f"font-size: 10px; font-weight: 700; letter-spacing: 0.8px; "
            f"color: {theme.TEXT_FAINT};"
        )
        card.body.addWidget(self._kind)
        return card

    def _build_comparison(self) -> QWidget:
        card = Card(
            "Last lap vs your best sectors",
            hint="Measured against your own bests, not a reference lap.",
        )
        self._comparison_rows: list[tuple[QLabel, QLabel]] = []

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        for column, heading in enumerate(("", "Time", "Delta")):
            label = QLabel(heading)
            label.setObjectName("StatLabel")
            grid.addWidget(label, 0, column)

        for index in range(3):
            name = QLabel(f"S{index + 1}")
            name.setObjectName("StatLabel")
            grid.addWidget(name, index + 1, 0)

            time_label = QLabel("-")
            time_label.setObjectName("Mono")
            grid.addWidget(time_label, index + 1, 1)

            delta_label = QLabel("-")
            delta_label.setObjectName("Mono")
            grid.addWidget(delta_label, index + 1, 2)
            self._comparison_rows.append((time_label, delta_label))

        card.body.addLayout(grid)
        return card

    def _build_trends(self) -> QWidget:
        card = Card(
            "Sector trend",
            hint="Recent laps against the ones before them.",
        )
        row = QHBoxLayout()
        row.setSpacing(24)
        self._trends = []
        for index in range(3):
            block = StatBlock(f"Sector {index + 1}", "-")
            self._trends.append(block)
            row.addWidget(block)
        card.body.addLayout(row)
        return card

    def _build_history(self) -> QWidget:
        card = Card(
            "Problems this session",
            hint="Kept for the whole session, including after telemetry stops.",
        )
        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["Area", "First seen", "Seen", "Peak loss", "Now", "Status"]
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
        coach = self.app.coach
        self._show_focus(coach.focus)
        self._show_comparison(coach)
        self._show_trends(coach)
        self._show_history(coach)

    def _show_focus(self, focus) -> None:
        if focus is None:
            self._headline.setText(
                "Nothing to work on right now - complete a few more laps."
            )
            self._headline.setStyleSheet(
                f"font-size: 17px; font-weight: 600; color: {theme.TEXT};"
            )
            for widget in (self._region, self._loss, self._repeat, self._confidence):
                widget.set_value("-")
            self._evidence.setText("")
            self._kind.setText("")
            return

        self._headline.setText(focus.observation)
        self._headline.setStyleSheet(
            f"font-size: 17px; font-weight: 600; "
            f"color: {SEVERITY_COLOURS[focus.severity]};"
        )
        self._region.set_value(focus.corner_or_region)
        self._loss.set_value(f"{focus.time_loss_s:.3f}")
        self._repeat.set_value(f"{focus.repeat_count} laps")
        self._confidence.set_value(focus.confidence.value)
        self._evidence.setText(focus.evidence)

        # Say plainly whether this is measured or a correlation.
        kind = focus.evidence_kind
        wording = (
            "MEASURED DIRECTLY" if kind is EvidenceKind.OBSERVED
            else "CORRELATION - NOT PROOF OF CAUSE"
        )
        self._kind.setText(wording)
        self._kind.setStyleSheet(
            f"font-size: 10px; font-weight: 700; letter-spacing: 0.8px; "
            f"color: {theme.LIVE if kind is EvidenceKind.OBSERVED else theme.WARN};"
        )

    def _show_comparison(self, coach) -> None:
        rows = coach.lap_comparison(self.app.lap_analysis)
        by_sector = {row["sector"]: row for row in rows}

        for index, (time_label, delta_label) in enumerate(self._comparison_rows):
            row = by_sector.get(index + 1)
            if row is None:
                time_label.setText("-")
                delta_label.setText("-")
                delta_label.setStyleSheet("")
                continue
            time_label.setText(f"{row['time_s']:.3f}")
            if row["is_best"]:
                delta_label.setText("BEST")
                colour = theme.LIVE
            else:
                delta_label.setText(f"{row['delta_s']:+.3f}")
                colour = theme.DANGER if row["delta_s"] > 0 else theme.LIVE
            delta_label.setStyleSheet(f"color: {colour}; font-weight: 600;")

    def _show_trends(self, coach) -> None:
        for index, block in enumerate(self._trends):
            trend, delta, confidence = coach.sector_trend(index + 1)
            if trend == "UNKNOWN":
                block.set_value("-")
                continue
            # Every derived number carries its confidence - a trend shown
            # bare invites more trust than the sample count supports.
            block.set_value(f"{trend}  {delta:+.3f}s  ({confidence.value})")

    def _show_history(self, coach) -> None:
        problems = coach.problems[: self.MAX_HISTORY]
        if self._table.rowCount() != len(problems):
            self._table.setRowCount(len(problems))

        for row, problem in enumerate(problems):
            values = (
                f"Sector {problem.sector} {problem.category.value.title()}"
                if problem.sector
                else problem.category.value.title(),
                f"L{problem.first_detected_lap}",
                str(problem.occurrences),
                f"{problem.peak_loss_s:.3f}s",
                f"{problem.current_loss_s:.3f}s",
                problem.status.value,
            )
            for column, text in enumerate(values):
                item = self._table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self._table.setItem(row, column, item)
                if item.text() != text:
                    item.setText(text)
            self._table.item(row, 5).setForeground(
                QColor(STATUS_COLOURS[problem.status])
            )
