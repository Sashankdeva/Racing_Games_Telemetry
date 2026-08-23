"""History - what happened across sessions.

Renders `SessionRecord` and `HistoryAnalysis`. No analysis here: every
comparison, trend and best is computed by `app.domain.session_history`.

Fields the telemetry never supplied are hidden or marked unavailable
rather than rendered as a wall of dashes.
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
from app.domain.lap_analysis import format_delta, format_lap_time
from app.domain.session_history import SessionRecord, SessionState, Trend
from app.ui import theme
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, StatBlock

TREND_COLOURS = {
    Trend.IMPROVING: theme.LIVE,
    Trend.DECLINING: theme.DANGER,
    Trend.STABLE: theme.TEXT,
    Trend.INSUFFICIENT_DATA: theme.TEXT_FAINT,
}

STATE_COLOURS = {
    SessionState.LIVE: theme.LIVE,
    SessionState.STALE: theme.WARN,
    SessionState.FINISHED: theme.TEXT_FAINT,
}


class HistoryPage(Page):
    title = "History"
    subtitle = "Sessions, personal bests and long-term progression"

    MAX_SESSIONS = 25

    def build(self) -> None:
        top = QHBoxLayout()
        top.setSpacing(16)
        top.addWidget(self._build_summary(), 3)
        top.addWidget(self._build_sectors(), 2)
        self.body.addLayout(top)

        self.body.addWidget(self._build_progression())
        self.body.addWidget(self._build_sessions(), 1)

    # ------------------------------------------------------------------
    def _build_summary(self) -> QWidget:
        card = Card("Current session")

        self._context = QLabel("-")
        self._context.setObjectName("Hint")
        card.body.addWidget(self._context)

        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(14)
        self._latest = StatBlock("Latest Best", "-")
        self._personal = StatBlock("Personal Best", "-")
        self._theoretical = StatBlock("Theoretical Best", "-")
        self._improvement = StatBlock("vs Previous", "-")
        self._laps = StatBlock("Laps", "-")
        self._state = StatBlock("State", "-")
        for index, widget in enumerate(
            (self._latest, self._personal, self._theoretical,
             self._improvement, self._laps, self._state)
        ):
            grid.addWidget(widget, index // 3, index % 3)
        card.body.addLayout(grid)

        self._note = QLabel("Complete a session to build history.")
        self._note.setObjectName("Hint")
        self._note.setWordWrap(True)
        card.body.addWidget(self._note)
        return card

    def _build_sectors(self) -> QWidget:
        card = Card(
            "Sector progress",
            hint="Latest session against the most recent comparable one.",
        )
        self._sector_rows: list[tuple[QLabel, QLabel]] = []

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        for column, heading in enumerate(("", "Best", "Change")):
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

            delta = QLabel("-")
            delta.setObjectName("Mono")
            grid.addWidget(delta, index + 1, 2)
            self._sector_rows.append((best, delta))

        card.body.addLayout(grid)
        return card

    def _build_progression(self) -> QWidget:
        card = Card(
            "Progression",
            hint="Across sessions on this car and track. Needs several "
                 "sessions before it will say anything.",
        )
        row = QHBoxLayout()
        row.setSpacing(24)
        self._pace_trend = StatBlock("Pace", "-")
        self._consistency_trend = StatBlock("Consistency", "-")
        self._s2_trend = StatBlock("Sector 2", "-")
        self._tyre_trend = StatBlock("Tyre Management", "-")
        for widget in (
            self._pace_trend, self._consistency_trend,
            self._s2_trend, self._tyre_trend,
        ):
            row.addWidget(widget)
        card.body.addLayout(row)
        return card

    def _build_sessions(self) -> QWidget:
        card = Card("Recent sessions", hint="Nothing here is ever auto-deleted.")
        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(
            ["Date", "Type", "Car", "Track", "Laps", "Best", "Average", "Quality"]
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
        history = self.app.session_history()
        current = self.app.history.record

        self._show_summary(history, current)
        self._show_sectors(history, current)
        self._show_progression(history, current)
        self._show_sessions(history)

    def _show_summary(self, history, current: SessionRecord | None) -> None:
        settings = self.app.mode_settings
        car = self.app.cars.get(settings.selected_car)
        track = self.app.tracks.get(settings.selected_track)
        self._context.setText(
            f"{self.app.game.display_name}   "
            f"{car.name if car else 'Unknown car'}   "
            f"{track.name if track else 'Unknown track'}"
        )

        state = self.app.history.state
        self._state.set_value(state.value)

        if current is None:
            self._laps.set_value("-")
            self._latest.set_value("-")
        else:
            self._laps.set_value(
                f"{current.laps_completed} ({len(current.valid_laps)} valid)"
            )
            self._latest.set_value(format_lap_time(current.best_lap_s or 0.0))

        compatible = history.compatible(
            car_id=settings.selected_car, track_id=settings.selected_track
        )
        best = history.personal_best(compatible)
        theoretical = history.theoretical_best(compatible)
        self._personal.set_value(format_lap_time(best or 0.0))
        self._theoretical.set_value(format_lap_time(theoretical or 0.0))

        if current is None:
            self._improvement.set_value("-")
            self._note.setText("Complete a session to build history.")
            return

        comparison = history.compare(current)
        if comparison.available:
            self._improvement.set_value(format_delta(comparison.improvement_s))
            largest = comparison.largest_gain
            note = (
                f"Previous best {format_lap_time(comparison.previous_best_s)}, "
                f"now {format_lap_time(comparison.current_best_s)}."
            )
            if largest is not None:
                note += (
                    f" Largest gain in sector {largest.sector} "
                    f"({largest.delta_s:+.3f}s)."
                )
            self._note.setText(note)
        else:
            self._improvement.set_value("-")
            self._note.setText(comparison.reason.capitalize() + ".")

    def _show_sectors(self, history, current: SessionRecord | None) -> None:
        comparison = history.compare(current) if current else None

        for index, (best_label, delta_label) in enumerate(self._sector_rows):
            sector = index + 1
            best = current.best_sector(sector) if current else None
            best_label.setText(format_lap_time(best) if best else "-")

            row = None
            if comparison is not None and comparison.available:
                row = next(
                    (s for s in comparison.sectors if s.sector == sector), None
                )
            if row is None or not row.available:
                delta_label.setText("-")
                delta_label.setStyleSheet("")
                continue
            delta_label.setText(f"{row.delta_s:+.3f}")
            delta_label.setStyleSheet(
                f"color: {theme.LIVE if row.delta_s < 0 else theme.DANGER}; "
                f"font-weight: 600;"
            )

    def _show_progression(self, history, current: SessionRecord | None) -> None:
        settings = self.app.mode_settings
        compatible = history.compatible(
            car_id=settings.selected_car, track_id=settings.selected_track
        )
        progression = history.progression(compatible)

        for widget, trend, delta in (
            (self._pace_trend, progression.pace, progression.pace_delta_s),
            (self._consistency_trend, progression.consistency,
             progression.consistency_delta_s),
            (self._s2_trend, progression.sectors[1], 0.0),
            (self._tyre_trend, progression.tyre_management, 0.0),
        ):
            if trend is Trend.INSUFFICIENT_DATA:
                widget.set_value("INSUFFICIENT DATA")
            elif delta:
                widget.set_value(f"{trend.value}  {delta:+.3f}s")
            else:
                widget.set_value(trend.value)

    def _show_sessions(self, history) -> None:
        import datetime

        sessions = history.sessions[: self.MAX_SESSIONS]
        if self._table.rowCount() != len(sessions):
            self._table.setRowCount(len(sessions))

        for row, record in enumerate(sessions):
            when = (
                datetime.datetime.fromtimestamp(record.started_at).strftime(
                    "%Y-%m-%d %H:%M"
                )
                if record.started_at
                else "-"
            )
            values = (
                when,
                record.session_type,
                record.car_id or "-",
                record.track_id or "-",
                str(record.laps_completed),
                format_lap_time(record.best_lap_s or 0.0),
                format_lap_time(record.average_lap_s or 0.0),
                record.telemetry_quality,
            )
            for column, text in enumerate(values):
                item = self._table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self._table.setItem(row, column, item)
                if item.text() != text:
                    item.setText(text)

            quality = record.telemetry_quality
            self._table.item(row, 7).setForeground(
                QColor(
                    theme.LIVE if quality == "GOOD"
                    else theme.WARN if quality == "MIXED"
                    else theme.TEXT_FAINT
                )
            )
