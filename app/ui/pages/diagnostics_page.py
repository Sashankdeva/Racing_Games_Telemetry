"""Diagnostics - everything needed to work out why telemetry is not working.

Laid out in the order you would actually troubleshoot: is the socket up,
are packets arriving, do they parse, do the decoded values agree at every
stage of the pipeline, and finally the log.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.logging import clear_logs, recent_logs, set_verbose
from app.diagnostics.metrics import DiagnosticsReport
from app.games.base import TelemetryStage
from app.ui import theme
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, FieldRow, StatusPill, ToggleSwitch


class _Row(QWidget):
    """Label / value pair with a status dot."""

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        caption = QLabel(label)
        caption.setStyleSheet(f"font-size: 12px; color: {theme.TEXT_DIM};")
        caption.setMinimumWidth(150)
        layout.addWidget(caption)

        self._pill = StatusPill("-", theme.IDLE)
        layout.addWidget(self._pill, 1)

    def set(self, text: str, colour: str) -> None:
        self._pill.set_state(text, colour)


class DiagnosticsPage(Page):
    title = "Diagnostics"
    subtitle = "Telemetry health, packet statistics and field-level tracing"

    def build(self) -> None:
        self.body.addWidget(self._build_right())
        self.body.addWidget(self._build_logs(), 1)

    def _build_right(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        self._rows: dict[str, _Row] = {}
        telemetry = Card("Telemetry")
        for key, label in (
            ("adapter", "Active adapter"),
            ("stage", "Pipeline stage"),
            ("listener", "Listener"),
            ("raw", "Raw packets (recvfrom)"),
            ("packets", "Packets received"),
            ("parsed", "Packets parsed"),
            ("packet_rate", "Packet rate (all types)"),
            ("frame_rate", "Update rate (frames)"),
            ("byte_rate", "Bytes/sec"),
            ("frames", "Frames emitted"),
            ("rejected", "Rejected packets"),
            ("age", "Last packet"),
            ("format", "Packet format"),
        ):
            row = _Row(label)
            self._rows[key] = row
            telemetry.body.addWidget(row)
        layout.addWidget(telemetry)

        live = Card(
            "Telemetry: RAW vs NORMALIZED vs UI",
            hint="The same three values at three stages of the pipeline. If "
                 "RAW is wrong the parser is at fault; if RAW is right but "
                 "NORMALIZED is wrong the adapter is; if both are right but "
                 "UI is wrong the state binding is.",
        )
        self._live_label = QLabel("No telemetry")
        self._live_label.setObjectName("Mono")
        self._live_label.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        live.body.addWidget(self._live_label)
        layout.addWidget(live)

        types = Card("Packet Types", hint="Which packet ids the game is sending.")
        self._packet_types_label = QLabel("None received")
        self._packet_types_label.setObjectName("Mono")
        self._packet_types_label.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        types.body.addWidget(self._packet_types_label)
        layout.addWidget(types)


        layout.addStretch(1)
        return container

    def _build_logs(self) -> QWidget:
        card = Card("Log")

        controls = QHBoxLayout()
        controls.setSpacing(12)

        self._verbose_toggle = ToggleSwitch()
        self._verbose_toggle.setChecked(self.app.settings.verbose_logging)
        self._verbose_toggle.toggled.connect(self._on_verbose)
        controls.addWidget(
            FieldRow(
                "Verbose logging",
                self._verbose_toggle,
                "Adds debug detail. Normal mode stays quiet on purpose.",
            ),
            1,
        )

        clear = QPushButton("Clear")
        clear.setObjectName("Ghost")
        clear.setCursor(Qt.CursorShape.PointingHandCursor)
        clear.clicked.connect(self._on_clear)
        controls.addWidget(clear)
        card.body.addLayout(controls)

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMinimumHeight(200)
        self._log_view.setMaximumBlockCount(600)
        card.body.addWidget(self._log_view, 1)

        self._log_count = 0
        return card

    # ------------------------------------------------------------------
    def _on_verbose(self, enabled: bool) -> None:
        set_verbose(enabled)
        self.app.settings.verbose_logging = enabled
        self.app.settings.save()

    def _on_clear(self) -> None:
        clear_logs()
        self._log_view.clear()
        self._log_count = 0

    def on_shown(self) -> None:
        self._verbose_toggle.setChecked(self.app.settings.verbose_logging)
        self._log_view.clear()
        self._log_count = 0

    # ------------------------------------------------------------------
    def refresh(self, report: DiagnosticsReport) -> None:
        rows = self._rows
        ok, bad, warn, idle = theme.LIVE, theme.DANGER, theme.WARN, theme.IDLE

        # --- telemetry ---
        adapter = report.adapter
        if adapter is None:
            for key in (
                "adapter", "stage", "listener", "raw", "packets", "parsed",
                "packet_rate", "frame_rate", "byte_rate", "frames", "rejected",
                "age", "format",
            ):
                rows[key].set("-", idle)
            self._packet_types_label.setText("None received")
            self._live_label.setText("No telemetry")
        else:
            rows["adapter"].set(adapter.display_name, idle)

            stage = adapter.stage
            stage_colour = {
                TelemetryStage.ERROR: bad,
                TelemetryStage.WAITING: idle,
                TelemetryStage.SOCKET_BOUND: warn,
                TelemetryStage.PACKETS_RECEIVED: warn,
                TelemetryStage.PACKETS_PARSED: warn,
                TelemetryStage.TELEMETRY_VALID: warn,
                TelemetryStage.TELEMETRY_LIVE: ok,
            }[stage]
            rows["stage"].set(f"{int(stage)}/6  {stage.label}", stage_colour)

            if not adapter.running:
                rows["listener"].set("Stopped", idle)
            elif adapter.connected:
                rows["listener"].set("Connected", ok)
            else:
                rows["listener"].set("Listening - no data", warn)

            rows["byte_rate"].set(
                f"{adapter.bytes_per_sec:,.0f} B/s",
                ok if adapter.bytes_per_sec > 0 else idle,
            )
            rows["frames"].set(
                str(adapter.frames_emitted),
                ok if adapter.frames_emitted else idle,
            )
            if adapter.packet_types:
                self._packet_types_label.setText(
                    "\n".join(f"{name:<22}{count}" for name, count in adapter.packet_types)
                )
            else:
                self._packet_types_label.setText("None received")

            rows["raw"].set(
                str(adapter.raw_packets),
                ok if adapter.raw_packets else idle,
            )
            rows["parsed"].set(
                str(adapter.packets_parsed),
                ok if adapter.packets_parsed else (bad if adapter.raw_packets else idle),
            )
            if adapter.frames_emitted:
                ratio = (
                    adapter.live_rpm / adapter.live_max_rpm
                    if adapter.live_max_rpm > 0 else 0.0
                )
                frame = report.frame
                agree = (
                    abs(adapter.raw_rpm - adapter.live_rpm) < 1
                    and abs(adapter.raw_speed_kph - adapter.live_speed_kph) < 1
                    and adapter.raw_gear == adapter.live_gear
                )
                self._live_label.setText(
                    "\n".join((
                        f"{'':<10}{'RAW':>10}{'NORMALIZED':>12}{'UI/ENGINE':>12}",
                        f"{'RPM':<10}{adapter.raw_rpm:>10.0f}{adapter.live_rpm:>12.0f}"
                        f"{frame.rpm:>12.0f}",
                        f"{'Speed kph':<10}{adapter.raw_speed_kph:>10.0f}"
                        f"{adapter.live_speed_kph:>12.0f}{frame.speed_kph:>12.0f}",
                        f"{'Gear':<10}{adapter.raw_gear:>10}{adapter.live_gear:>12}"
                        f"{frame.gear:>12}",
                        f"{'Throttle %':<10}{adapter.raw_throttle * 100:>10.0f}"
                        f"{adapter.live_throttle * 100:>12.0f}{'-':>12}",
                        f"{'Brake %':<10}{adapter.raw_brake * 100:>10.0f}"
                        f"{adapter.live_brake * 100:>12.0f}{'-':>12}",
                        f"{'Max RPM':<10}{adapter.raw_max_rpm:>10.0f}"
                        f"{adapter.live_max_rpm:>12.0f}{frame.max_rpm:>12.0f}",
                        "",
                        f"redline {ratio * 100:.0f}%   player car index "
                        f"{adapter.player_car_index}",
                        f"raw/normalized agree: {'YES' if agree else 'NO - MISMATCH'}",
                        "",
                        f"gear trace: {adapter.prev_gear} -> {adapter.current_gear}"
                        f"   last shift: {adapter.last_shift}"
                        f"   shifts: {adapter.shift_count}",
                    ))
                )
            else:
                self._live_label.setText("No telemetry")

            rows["packets"].set(str(adapter.packets_received), idle)
            rows["packet_rate"].set(
                f"{adapter.packet_rate:.0f}/s",
                ok if adapter.packet_rate > 0 else idle,
            )
            # ~60/s is the healthy figure: one frame per CarTelemetry packet.
            rows["frame_rate"].set(
                f"{adapter.frame_rate:.0f}/s",
                ok if adapter.frame_rate > 0 else idle,
            )
            rows["rejected"].set(
                str(adapter.packets_rejected),
                warn if adapter.packets_rejected else idle,
            )
            rows["age"].set(
                f"{adapter.last_packet_age:.2f}s ago" if adapter.packets_received else "never",
                idle,
            )
            rows["format"].set(adapter.detail or adapter.error or "-", bad if adapter.error else idle)


        self._append_new_logs()

    def _append_new_logs(self) -> None:
        records = recent_logs()
        if len(records) <= self._log_count:
            # The ring buffer wrapped or was cleared elsewhere; resync.
            if len(records) < self._log_count:
                self._log_count = 0
                self._log_view.clear()
            else:
                return

        for record in records[self._log_count :]:
            self._log_view.appendPlainText(
                f"{record.clock}  {record.level:<7} {record.logger.removeprefix('app.')}  {record.message}"
            )
        self._log_count = len(records)
