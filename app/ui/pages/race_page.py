"""Race - the factual picture around the driver.

Renders `RaceState` and the race event log. No race logic lives here: every
gap, rate, state and trend is computed by `app.domain.race_intelligence`.
Recommendations belong to the Suggestions page - this one only reports what
is happening.

Fields the telemetry cannot support are shown as UNAVAILABLE with the
reason, rather than as a plausible-looking zero.
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
from app.domain.race_intelligence import (
    EventType,
    GapInfo,
    GapTrend,
    RaceState,
)
from app.ui import theme
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, StatBlock

TREND_COLOURS = {
    GapTrend.CLOSING: theme.LIVE,
    GapTrend.OPENING: theme.DANGER,
    GapTrend.STABLE: theme.TEXT,
    GapTrend.UNKNOWN: theme.TEXT_FAINT,
}

#: Gaining a place reads green, losing one red; everything else is neutral
#: so the log can be scanned for the moments that mattered.
EVENT_COLOURS = {
    EventType.OVERTAKE: theme.LIVE,
    EventType.BEEN_OVERTAKEN: theme.DANGER,
    EventType.SAFETY_CAR: theme.WARN,
    EventType.VSC: theme.WARN,
    EventType.ATTACK_DETECTED: theme.ACCENT,
}


class RacePage(Page):
    title = "Race"
    subtitle = "Position, gaps and race state - facts, not recommendations"

    MAX_EVENTS = 40

    def build(self) -> None:
        top = QHBoxLayout()
        top.setSpacing(16)
        top.addWidget(self._build_position(), 2)
        top.addWidget(self._build_neighbours(), 3)
        self.body.addLayout(top)

        middle = QHBoxLayout()
        middle.setSpacing(16)
        middle.addWidget(self._build_situation(), 2)
        middle.addWidget(self._build_trends(), 2)
        self.body.addLayout(middle)

        self.body.addWidget(self._build_events(), 1)

    # ------------------------------------------------------------------
    def _build_position(self) -> QWidget:
        card = Card("Race")
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(14)
        self._position = StatBlock("Position", "-")
        self._lap = StatBlock("Lap", "-")
        self._remaining = StatBlock("Remaining", "-", "laps")
        self._leader = StatBlock("To Leader", "-", "s")
        for index, widget in enumerate(
            (self._position, self._lap, self._remaining, self._leader)
        ):
            grid.addWidget(widget, index // 2, index % 2)
        card.body.addLayout(grid)

        self._phase = QLabel("PHASE UNKNOWN")
        self._phase.setStyleSheet(
            f"font-size: 11px; font-weight: 700; letter-spacing: 0.8px; "
            f"color: {theme.TEXT_FAINT};"
        )
        card.body.addWidget(self._phase)
        return card

    def _neighbour_block(self, title: str, hint: str):
        card = Card(title, hint=hint)
        row = QHBoxLayout()
        row.setSpacing(20)
        gap = StatBlock("Gap", "-", "s")
        rate = StatBlock("Rate", "-", "s/lap")
        row.addWidget(gap)
        row.addWidget(rate)
        card.body.addLayout(row)

        trend = QLabel("UNKNOWN")
        trend.setStyleSheet(
            f"font-size: 12px; font-weight: 700; color: {theme.TEXT_FAINT};"
        )
        card.body.addWidget(trend)

        note = QLabel("")
        note.setObjectName("Hint")
        note.setWordWrap(True)
        card.body.addWidget(note)
        return card, gap, rate, trend, note

    def _build_neighbours(self) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        ahead, self._ahead_gap, self._ahead_rate, self._ahead_trend, self._ahead_note = (
            self._neighbour_block("Ahead", "Smoothed across completed laps.")
        )
        behind, self._behind_gap, self._behind_rate, self._behind_trend, self._behind_note = (
            self._neighbour_block("Behind", "Requires opponent lap data.")
        )
        layout.addWidget(ahead)
        layout.addWidget(behind)
        return container

    def _build_situation(self) -> QWidget:
        card = Card("Situation")
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(14)
        self._attack = StatBlock("Attack", "-")
        self._defence = StatBlock("Defence", "-")
        self._drs = StatBlock("DRS", "-")
        self._traffic = StatBlock("Traffic", "-")
        self._neutralised = StatBlock("Race Control", "-")
        self._confidence = StatBlock("Confidence", "-")
        for index, widget in enumerate(
            (
                self._attack, self._defence, self._drs,
                self._traffic, self._neutralised, self._confidence,
            )
        ):
            grid.addWidget(widget, index // 3, index % 3)
        card.body.addLayout(grid)
        return card

    def _build_trends(self) -> QWidget:
        card = Card("Trends", hint="Derived from lap analysis and the stint model.")
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(14)
        self._pace_trend = StatBlock("Pace", "-")
        self._position_trend = StatBlock("Position", "-")
        self._tyre_trend = StatBlock("Tyre", "-")
        for index, widget in enumerate(
            (self._pace_trend, self._position_trend, self._tyre_trend)
        ):
            grid.addWidget(widget, 0, index)
        card.body.addLayout(grid)
        return card

    def _build_events(self) -> QWidget:
        card = Card("Race events", hint="Preserved for the whole session.")
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Lap", "Event", "Change", "Detail"])
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
    def _show_neighbour(self, info: GapInfo, gap, rate, trend, note) -> None:
        if not info.available:
            gap.set_value("-")
            rate.set_value("-")
            trend.setText(info.availability.value)
            trend.setStyleSheet(
                f"font-size: 12px; font-weight: 700; color: {theme.TEXT_FAINT};"
            )
            note.setText(info.reason)
            return

        gap.set_value(f"{info.gap_s:.3f}" if info.gap_s is not None else "-")
        rate.set_value(
            f"{info.rate_s_per_lap:+.3f}" if info.rate_s_per_lap is not None else "-"
        )
        trend.setText(info.trend.value)
        trend.setStyleSheet(
            f"font-size: 12px; font-weight: 700; color: {TREND_COLOURS[info.trend]};"
        )

        parts = [f"{info.samples} lap sample(s)", info.confidence.value]
        laps = info.laps_to_contact
        if laps is not None:
            parts.append(f"~{laps:.0f} laps to contact")
        note.setText("   ".join(parts))

    def refresh(self, report: DiagnosticsReport) -> None:
        state: RaceState = self.app.race_state(report)

        self._position.set_value(str(state.position) if state.position else "-")
        self._lap.set_value(
            f"{state.lap}/{state.total_laps}"
            if state.lap and state.total_laps
            else (str(state.lap) if state.lap else "-")
        )
        self._remaining.set_value(
            str(state.laps_remaining) if state.laps_remaining is not None else "-"
        )
        self._leader.set_value(
            f"{state.leader_gap_s:.3f}" if state.leader_gap_s else "-"
        )
        self._phase.setText(f"PHASE {state.race_phase.value}")

        self._show_neighbour(
            state.ahead, self._ahead_gap, self._ahead_rate,
            self._ahead_trend, self._ahead_note,
        )
        self._show_neighbour(
            state.behind, self._behind_gap, self._behind_rate,
            self._behind_trend, self._behind_note,
        )

        self._attack.set_value(state.attack_state.value)
        self._defence.set_value(state.defence_state.value)
        self._drs.set_value(f"{state.drs_term}: {state.drs_state.value}")
        self._traffic.set_value(state.traffic_state.value)
        self._neutralised.set_value(state.neutralised.value)
        self._confidence.set_value(state.confidence.value)

        for widget, trend in (
            (self._pace_trend, state.pace_trend),
            (self._position_trend, state.position_trend),
            (self._tyre_trend, state.tyre_trend),
        ):
            widget.set_value(trend.value)

        self._show_events()

    def _show_events(self) -> None:
        events = self.app.race.events[: self.MAX_EVENTS]
        if self._table.rowCount() != len(events):
            self._table.setRowCount(len(events))

        for row, event in enumerate(events):
            change = (
                f"P{event.position_from} -> P{event.position_to}"
                if event.position_from and event.position_to
                else "-"
            )
            values = (
                str(event.lap),
                event.type.value,
                change,
                event.detail or "",
            )
            for column, text in enumerate(values):
                item = self._table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self._table.setItem(row, column, item)
                if item.text() != text:
                    item.setText(text)

            self._table.item(row, 1).setForeground(QColor(EVENT_COLOURS.get(
                event.type, theme.TEXT
            )))
