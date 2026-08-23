"""Telemetry Inspector - validate the pipeline against the real game.

Three panels, in the order you would actually use them:

  Packets   what the game is sending: type, format, rate, size, age
  Fields    per field: present? parsed? changing? - with a blunt verdict
  Capture   record a short session, then replay it deterministically

The field verdicts are the point. A value that never changes is reported as
STATIC rather than OK, because the failure that bit us before looked
perfectly healthy: a plausible constant. ABSENT means the field arrived but
was always zero/empty, which usually means the packet carrying it is not
being sent.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from app.diagnostics.metrics import DiagnosticsReport
from app.telemetry.recording import list_recordings
from app.ui import theme
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, StatBlock

_VERDICT_COLOURS = {
    "OK": theme.LIVE,
    "STATIC": theme.WARN,
    "ABSENT": theme.DANGER,
    "NO DATA": theme.TEXT_FAINT,
}


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
    table.setShowGrid(False)
    table.setAlternatingRowColors(False)
    header = table.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for index in range(1, len(headers)):
        header.setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)
    table.setStyleSheet(
        f"""
        QTableWidget {{
            background-color: {theme.BG};
            border: 1px solid {theme.BORDER};
            border-radius: 8px;
            font-size: 12px;
            color: {theme.TEXT_DIM};
            gridline-color: transparent;
        }}
        QHeaderView::section {{
            background-color: {theme.SURFACE_ALT};
            color: {theme.TEXT_FAINT};
            border: none;
            padding: 6px 8px;
            font-size: 10px;
            font-weight: 700;
        }}
        QTableWidget::item {{ padding: 5px 8px; }}
        """
    )
    return table


class InspectorPage(Page):
    title = "Inspector"
    subtitle = "Validate telemetry against the real game, then record and replay it"

    def build(self) -> None:
        self.body.addWidget(self._build_summary())
        self.body.addWidget(self._build_capture())
        self.body.addWidget(self._build_packets())
        self.body.addWidget(self._build_fields(), 1)

    # ------------------------------------------------------------------
    def _build_summary(self) -> QWidget:
        card = Card("Pipeline")
        row = QHBoxLayout()
        row.setSpacing(20)
        self._source = StatBlock("Source", "-")
        self._format = StatBlock("Packet Format", "-")
        self._rate = StatBlock("Packets", "0", "/s")
        self._frames = StatBlock("Frames", "0")
        self._bad = StatBlock("Unparseable", "0")
        for widget in (self._source, self._format, self._rate, self._frames, self._bad):
            row.addWidget(widget)
        card.body.addLayout(row)

        self._warning = QLabel("")
        self._warning.setStyleSheet(f"font-size: 11px; color: {theme.WARN};")
        self._warning.setWordWrap(True)
        self._warning.setVisible(False)
        card.body.addWidget(self._warning)
        return card

    def _build_capture(self) -> QWidget:
        card = Card(
            "Record & Replay",
            hint="Recording captures raw packets exactly as the game sends them. "
                 "Replay feeds them back through the same parser and adapter used "
                 "live, so a bug reproduces deterministically without the game.",
        )

        row = QHBoxLayout()
        row.setSpacing(8)
        self._record_button = QPushButton("Start Recording")
        self._record_button.setObjectName("Primary")
        self._record_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._record_button.clicked.connect(self._on_record)
        row.addWidget(self._record_button)

        self._record_status = QLabel("Not recording")
        self._record_status.setObjectName("Hint")
        row.addWidget(self._record_status, 1)
        card.body.addLayout(row)

        replay_row = QHBoxLayout()
        replay_row.setSpacing(8)
        self._recordings = QComboBox()
        replay_row.addWidget(self._recordings, 1)

        for label, handler in (
            ("Load", self._on_load),
            ("Play", self._on_play),
            ("Step", self._on_step),
            ("Stop", self._on_stop_replay),
        ):
            button = QPushButton(label)
            button.setObjectName("Ghost")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(handler)
            replay_row.addWidget(button)
        card.body.addLayout(replay_row)

        self._replay_status = QLabel("No recording loaded")
        self._replay_status.setObjectName("Hint")
        card.body.addWidget(self._replay_status)
        return card

    def _build_packets(self) -> QWidget:
        card = Card("Packets Received", hint="What the game is actually sending.")
        self._packet_table = _table(
            ["Packet type", "ID", "Format", "Count", "Rate/s", "Size", "Last seen"]
        )
        self._packet_table.setMinimumHeight(150)
        card.body.addWidget(self._packet_table)
        return card

    def _build_fields(self) -> QWidget:
        card = Card(
            "Field Validation",
            hint="STATIC means the value never changed - it may be legitimate, "
                 "or it may be a field we are decoding from the wrong offset. "
                 "ABSENT means it arrived but was always zero.",
        )
        self._field_table = _table(
            ["Field", "Source packet", "Value", "Range seen", "Distinct", "Verdict"]
        )
        self._field_table.setMinimumHeight(320)
        card.body.addWidget(self._field_table)
        return card

    # ------------------------------------------------------------------
    def _on_record(self) -> None:
        if self.app.recording:
            meta = self.app.stop_recording()
            if meta is not None:
                self._record_status.setText(
                    f"Saved {meta.packet_count} packets ({meta.duration_s:.0f}s)"
                )
            self._reload_recordings()
        else:
            path = self.app.start_recording()
            self._record_status.setText(
                f"Recording to {path.name}" if path else "Could not start recording"
            )

    def _reload_recordings(self) -> None:
        self._recordings.clear()
        for path, meta in list_recordings():
            self._recordings.addItem(
                f"{path.name}   {meta.packet_count} pkts   {meta.duration_s:.0f}s",
                str(path),
            )

    def _on_load(self) -> None:
        value = self._recordings.currentData()
        if not value:
            self._replay_status.setText("No recording selected")
            return
        player = self.app.load_replay(Path(value))
        if player is None:
            self._replay_status.setText("Could not load that recording")
            return
        self._replay_status.setText(
            f"Loaded {player.packet_count} packets ({player.duration:.0f}s). "
            "Play runs at recorded speed; Step advances 20 packets."
        )

    def _on_play(self) -> None:
        if self.app.replay is None:
            self._on_load()
        if self.app.start_replay():
            self._replay_status.setText("Replaying at recorded speed")

    def _on_step(self) -> None:
        player = self.app.replay
        if player is None:
            self._on_load()
            player = self.app.replay
        if player is not None:
            sent = player.step(20)
            self._replay_status.setText(
                f"Stepped {sent} packets  ({player.position}/{player.packet_count})"
                if sent
                else "End of recording"
            )

    def _on_stop_replay(self) -> None:
        self.app.stop_replay()
        self._replay_status.setText("Replay stopped")

    def on_shown(self) -> None:
        self._reload_recordings()

    # ------------------------------------------------------------------
    def refresh(self, report: DiagnosticsReport) -> None:
        inspector = self.app.inspector
        adapter = report.adapter

        if self.app.replaying or (self.app.replay is not None):
            self._source.set_value("REPLAY")
        elif adapter and adapter.running:
            self._source.set_value("LIVE")
        else:
            self._source.set_value("-")

        formats = inspector.formats_seen()
        self._format.set_value(
            "/".join(str(f) for f, _ in formats.most_common(2)) if formats else "-"
        )
        self._rate.set_value(f"{adapter.packet_rate:.0f}" if adapter else "0")
        self._frames.set_value(str(inspector.frames))
        self._bad.set_value(str(inspector.unparseable))

        if adapter is not None and adapter.format_mismatch:
            self._warning.setText(
                f"The game is sending packet format {adapter.detail.split()[-1]}, "
                f"which the selected mode ({self.app.game.display_name}) did not "
                "expect. Telemetry still parses - this is advisory."
            )
            self._warning.setVisible(True)
        elif inspector.unparseable and inspector.last_bad_packet:
            self._warning.setText(
                f"{inspector.unparseable} unparseable packets. First bytes: "
                + inspector.last_bad_packet[:16].hex(" ")
            )
            self._warning.setVisible(True)
        else:
            self._warning.setVisible(False)

        if self.app.recording and self.app.recorder is not None:
            recorder = self.app.recorder
            self._record_button.setText("Stop Recording")
            self._record_status.setText(
                f"Recording {recorder.meta.packet_count} packets "
                f"({recorder.elapsed:.0f}s)"
            )
        else:
            self._record_button.setText("Start Recording")

        self._fill_packets(inspector.packet_stats())
        self._fill_fields(inspector.field_stats())

    def _fill_packets(self, stats) -> None:
        table = self._packet_table
        table.setRowCount(len(stats))
        for row, stat in enumerate(stats):
            age = "-" if stat.age == float("inf") else f"{stat.age:.1f}s ago"
            values = [
                stat.name, str(stat.packet_id),
                str(stat.common_format or "-"), str(stat.count),
                f"{stat.rate:.0f}", f"{stat.common_size}B", age,
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setForeground(Qt.GlobalColor.white)
                table.setItem(row, column, item)

    def _fill_fields(self, stats) -> None:
        from PySide6.QtGui import QColor

        table = self._field_table
        table.setRowCount(len(stats))
        for row, stat in enumerate(stats):
            value = stat.last_value
            if isinstance(value, float):
                shown = f"{value:.3f}".rstrip("0").rstrip(".")
            else:
                shown = str(value) if value is not None else "-"

            values = [
                stat.label, stat.source, shown,
                stat.range_text if stat.samples else "-",
                str(stat.distinct), stat.verdict,
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setForeground(QColor(theme.TEXT))
                elif column == 5:
                    item.setForeground(QColor(_VERDICT_COLOURS.get(stat.verdict, theme.TEXT_DIM)))
                table.setItem(row, column, item)
