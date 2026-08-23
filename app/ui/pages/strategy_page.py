"""Strategy - the recommended plan, the alternatives, and why.

Renders `StrategyPlan`. No strategy maths here: candidates, scores, the pit
window and the reasoning all come from `app.domain.strategy`.

When the engine cannot produce a plan, this page says why in plain terms
rather than showing an empty card - "not enough laps to measure
degradation" is a useful answer, a blank panel is not.
"""

from __future__ import annotations

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
from app.domain.lap_analysis import Confidence
from app.domain.strategy import Risk, StrategyKind, StrategyRecommendation
from app.ui import theme
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, StatBlock

RISK_COLOURS = {
    Risk.LOW: theme.LIVE,
    Risk.MEDIUM: theme.WARN,
    Risk.HIGH: theme.DANGER,
    Risk.UNKNOWN: theme.TEXT_FAINT,
}

CONFIDENCE_COLOURS = {
    Confidence.NO_DATA: theme.TEXT_FAINT,
    Confidence.INSUFFICIENT: theme.TEXT_FAINT,
    Confidence.LOW: theme.WARN,
    Confidence.MEDIUM: theme.WARN,
    Confidence.HIGH: theme.LIVE,
}


class StrategyPage(Page):
    title = "Strategy"
    subtitle = "Recommended plan, alternatives, and the reasoning behind them"

    MAX_HISTORY = 20

    def build(self) -> None:
        self.body.addWidget(self._build_recommended())

        columns = QHBoxLayout()
        columns.setSpacing(16)
        columns.addWidget(self._build_alternative(), 1)
        columns.addWidget(self._build_baseline(), 1)
        self.body.addLayout(columns)

        self.body.addWidget(self._build_history(), 1)

    # ------------------------------------------------------------------
    def _build_recommended(self) -> QWidget:
        card = Card("Recommended strategy")

        grid = QGridLayout()
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(14)
        self._pit = StatBlock("Pit", "-")
        self._compound = StatBlock("Compound", "-")
        self._stops = StatBlock("Stops", "-")
        self._gain = StatBlock("Expected Gain", "-", "s")
        self._risk = StatBlock("Risk", "-")
        self._confidence = StatBlock("Confidence", "-")
        for index, widget in enumerate(
            (self._pit, self._compound, self._stops,
             self._gain, self._risk, self._confidence)
        ):
            grid.addWidget(widget, index // 3, index % 3)
        card.body.addLayout(grid)

        self._window = QLabel("")
        self._window.setObjectName("Hint")
        card.body.addWidget(self._window)

        why_label = QLabel("WHY")
        why_label.setObjectName("StatLabel")
        card.body.addWidget(why_label)

        self._why = QLabel("No plan yet.")
        self._why.setWordWrap(True)
        self._why.setStyleSheet(f"font-size: 13px; color: {theme.TEXT};")
        card.body.addWidget(self._why)

        self._assumptions = QLabel("")
        self._assumptions.setObjectName("Hint")
        self._assumptions.setWordWrap(True)
        card.body.addWidget(self._assumptions)

        self._source = QLabel("")
        self._source.setObjectName("Mono")
        self._source.setWordWrap(True)
        self._source.setStyleSheet(f"font-size: 10px; color: {theme.TEXT_FAINT};")
        card.body.addWidget(self._source)
        return card

    def _simple_card(self, title: str, hint: str):
        card = Card(title, hint=hint)
        headline = QLabel("-")
        headline.setStyleSheet(
            f"font-size: 18px; font-weight: 600; color: {theme.TEXT};"
        )
        card.body.addWidget(headline)
        detail = QLabel("")
        detail.setObjectName("Hint")
        detail.setWordWrap(True)
        card.body.addWidget(detail)
        return card, headline, detail

    def _build_alternative(self) -> QWidget:
        card, self._alt_headline, self._alt_detail = self._simple_card(
            "Alternative", "The next best plan the engine scored."
        )
        return card

    def _build_baseline(self) -> QWidget:
        card, self._base_headline, self._base_detail = self._simple_card(
            "Current strategy", "The baseline every alternative is measured against."
        )
        return card

    def _build_history(self) -> QWidget:
        card = Card(
            "Strategy changes",
            hint="Recorded only when the recommendation materially moved.",
        )
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Lap", "From", "To", "Why"])
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        card.body.addWidget(self._table)

        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(card)
        return holder

    # ------------------------------------------------------------------
    def _clear(self, reason: str) -> None:
        for widget in (
            self._pit, self._compound, self._stops,
            self._gain, self._risk, self._confidence,
        ):
            widget.set_value("-")
        self._window.setText("")
        self._why.setText(reason or "No plan yet.")
        self._assumptions.setText("")
        self._source.setText("")
        self._alt_headline.setText("-")
        self._alt_detail.setText("")
        self._base_headline.setText("-")
        self._base_detail.setText("")

    def _show_recommended(self, best: StrategyRecommendation) -> None:
        if best.kind is StrategyKind.PIT:
            self._pit.set_value(f"L{best.pit_lap}")
            self._compound.set_value(best.next_compound or "-")
            self._gain.set_value(f"{best.time_delta_s:+.1f}")
        else:
            self._pit.set_value("STAY OUT")
            self._compound.set_value(best.current_compound or "-")
            self._gain.set_value("-")
        self._stops.set_value(str(best.number_of_stops))

        self._risk.set_value(best.risk.value)
        self._confidence.set_value(best.confidence.value)
        self._why.setText(best.reason)

        self._assumptions.setText(
            "Assumptions: " + "  ".join(best.assumptions) if best.assumptions else ""
        )
        self._source.setText(
            "   ".join(f"{key}={value}" for key, value in best.source_data.items())
        )

    def refresh(self, report: DiagnosticsReport) -> None:
        plan = self.app.strategy_plan(report)

        if not plan.available or plan.recommended is None:
            self._clear(plan.reason)
            self._show_history()
            return

        self._show_recommended(plan.recommended)

        if plan.pit_window:
            first, last = plan.pit_window
            text = f"Pit window: laps {first}-{last}." if last > first else (
                f"Pit window: lap {first}."
            )
            if plan.stale:
                text += "  (telemetry stale - plan not recalculated)"
            self._window.setText(text)
        else:
            self._window.setText(
                "Telemetry stale - plan not recalculated." if plan.stale else ""
            )

        if plan.alternative is not None:
            self._alt_headline.setText(plan.alternative.summary())
            self._alt_detail.setText(
                f"{plan.alternative.time_delta_s:+.1f}s   "
                f"risk {plan.alternative.risk.value}   "
                f"{plan.alternative.confidence.value}"
            )
        else:
            self._alt_headline.setText("-")
            self._alt_detail.setText("No second candidate could be modelled.")

        if plan.baseline is not None:
            self._base_headline.setText(plan.baseline.summary())
            self._base_detail.setText(plan.baseline.reason)

        if plan.unmodelled:
            existing = self._alt_detail.text()
            self._alt_detail.setText(
                f"{existing}\nNo session data for: {', '.join(plan.unmodelled)}."
            )

        self._show_history()

    def _show_history(self) -> None:
        changes = self.app.strategy.history[: self.MAX_HISTORY]
        if self._table.rowCount() != len(changes):
            self._table.setRowCount(len(changes))

        for row, change in enumerate(changes):
            values = (str(change.lap), change.previous, change.current, change.reason)
            for column, text in enumerate(values):
                item = self._table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self._table.setItem(row, column, item)
                if item.text() != text:
                    item.setText(text)
