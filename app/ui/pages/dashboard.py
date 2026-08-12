"""Dashboard - the page you can actually read mid-race.

Everything on it is either a state you need to trust (is it connected, is
telemetry live, is it running) or live feedback (motor levels, active
effects). Controls are limited to the two things worth reaching for while
driving: master strength and the emergency stop.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.diagnostics.metrics import DiagnosticsReport
from app.games.base import TelemetryStage
from app.haptics.effects import EFFECTS_BY_ID
from app.ui import theme
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, LabeledSlider, StatBlock, StatusPill
from app.ui.widgets.meters import HapticScope, MotorMeter, RpmBar


class DashboardPage(Page):
    title = "Dashboard"
    subtitle = "Live status and haptic output"

    def build(self) -> None:
        self.body.addWidget(self._build_status_row())

        columns = QHBoxLayout()
        columns.setSpacing(16)
        columns.addWidget(self._build_output_card(), 3)
        columns.addWidget(self._build_side_column(), 2)
        # Stretch 1 with no trailing spacer: the output card and scope grow
        # to fill the window instead of hugging the top of the page.
        self.body.addLayout(columns, 1)

    # ------------------------------------------------------------------
    def _build_status_row(self) -> QWidget:
        card = Card("System Status")
        grid = QGridLayout()
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(12)

        self._controller_pill = StatusPill("Checking...", theme.IDLE)
        self._connection_pill = StatusPill("-", theme.IDLE)
        self._game_pill = StatusPill("-", theme.IDLE)
        self._telemetry_pill = StatusPill("-", theme.IDLE)

        entries = (
            ("CONTROLLER", self._controller_pill),
            ("CONNECTION", self._connection_pill),
            ("GAME", self._game_pill),
            ("TELEMETRY", self._telemetry_pill),
        )
        for column, (label, pill) in enumerate(entries):
            caption = QLabel(label)
            caption.setObjectName("StatLabel")
            grid.addWidget(caption, 0, column)
            grid.addWidget(pill, 1, column)
        for column in range(len(entries)):
            grid.setColumnStretch(column, 1)

        card.body.addLayout(grid)
        return card

    def _build_output_card(self) -> QWidget:
        card = Card("Haptic Output")

        self._left_meter = MotorMeter("LEFT MOTOR")
        self._right_meter = MotorMeter("RIGHT MOTOR")
        card.body.addWidget(self._left_meter)
        card.body.addWidget(self._right_meter)

        self._scope = HapticScope()
        card.body.addWidget(self._scope, 1)

        self._active_label = QLabel("No active effects")
        self._active_label.setObjectName("Hint")
        self._active_label.setWordWrap(True)
        card.body.addWidget(self._active_label)
        return card

    def _build_side_column(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        # --- vehicle ---
        vehicle = Card("Vehicle")
        self._rpm_bar = RpmBar()
        vehicle.body.addWidget(self._rpm_bar)

        stats = QHBoxLayout()
        stats.setSpacing(20)
        self._speed_stat = StatBlock("Speed", "0", "kph")
        self._rpm_stat = StatBlock("RPM", "0")
        self._gear_stat = StatBlock("Gear", "N")
        for stat in (self._speed_stat, self._rpm_stat, self._gear_stat):
            stats.addWidget(stat)
        vehicle.body.addLayout(stats)
        layout.addWidget(vehicle)

        # --- master strength ---
        control = Card("Master Strength", hint=f"Profile: {self.app.profiles.active.name}")
        self._profile_hint = control.findChild(QLabel, "CardHint")
        self._master_slider = LabeledSlider(
            "Overall haptic strength",
            0.0, 1.5,
            self.app.profiles.active.master.intensity,
            step=0.05,
            decimals=2,
        )
        self._master_slider.valueChanged.connect(self._on_master_changed)
        control.body.addWidget(self._master_slider)

        rates = QHBoxLayout()
        rates.setSpacing(20)
        self._rate_stat = StatBlock("Haptic Rate", "0", "Hz")
        self._packet_stat = StatBlock("Packets", "0", "/s")
        rates.addWidget(self._rate_stat)
        rates.addWidget(self._packet_stat)
        control.body.addLayout(rates)
        layout.addWidget(control)

        # --- emergency stop ---
        self._stop_button = QPushButton("EMERGENCY STOP")
        self._stop_button.setObjectName("EmergencyStop")
        self._stop_button.setCheckable(True)
        self._stop_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stop_button.setMinimumHeight(62)
        self._stop_button.clicked.connect(self._on_stop_clicked)
        layout.addWidget(self._stop_button)

        layout.addStretch(1)
        return container

    # ------------------------------------------------------------------
    def _on_master_changed(self, value: float) -> None:
        profile = self.app.profiles.active
        profile.master.intensity = value
        self.app.apply_profile(profile)

    def _on_stop_clicked(self) -> None:
        if self._stop_button.isChecked():
            self.app.emergency_stop()
        else:
            self.app.clear_emergency_stop()

    def on_shown(self) -> None:
        profile = self.app.profiles.active
        self._master_slider.set_value(profile.master.intensity)
        if self._profile_hint is not None:
            self._profile_hint.setText(f"Profile: {profile.name}")

    # ------------------------------------------------------------------
    def refresh(self, report: DiagnosticsReport) -> None:
        engine = report.engine

        if report.controller_connected:
            self._controller_pill.set_state("Connected", theme.LIVE)
            self._connection_pill.set_state(f"XInput - Slot {report.controller_index}", theme.LIVE)
        else:
            self._controller_pill.set_state("Disconnected", theme.DANGER)
            self._connection_pill.set_state("No device", theme.IDLE)

        adapter = report.adapter
        if adapter is None:
            self._game_pill.set_state("No adapter", theme.IDLE)
            self._telemetry_pill.set_state("-", theme.IDLE)
        else:
            # Report the pipeline stage rather than a single "connected"
            # flag: a bound socket receiving nothing is a completely
            # different problem from packets that fail to parse.
            stage = adapter.stage
            colour = {
                TelemetryStage.ERROR: theme.DANGER,
                TelemetryStage.WAITING: theme.IDLE,
                TelemetryStage.SOCKET_BOUND: theme.WARN,
                TelemetryStage.PACKETS_RECEIVED: theme.WARN,
                TelemetryStage.TELEMETRY_VALID: theme.WARN,
                TelemetryStage.TELEMETRY_LIVE: theme.LIVE,
            }[stage]

            if stage is TelemetryStage.TELEMETRY_LIVE:
                self._game_pill.set_state("Connected", theme.LIVE)
            elif stage is TelemetryStage.ERROR:
                self._game_pill.set_state("Error", theme.DANGER)
            elif not adapter.running:
                self._game_pill.set_state(f"{adapter.display_name} - stopped", theme.IDLE)
            else:
                self._game_pill.set_state("Waiting for game", theme.WARN)

            if stage is TelemetryStage.TELEMETRY_LIVE:
                self._telemetry_pill.set_state(f"Live - {adapter.packet_rate:.0f}/s", colour)
            else:
                self._telemetry_pill.set_state(stage.label, colour)
            self._packet_stat.set_value(f"{adapter.packet_rate:.0f}")

        self._left_meter.set_value(engine.left)
        self._right_meter.set_value(engine.right)
        self._scope.push(engine.left, engine.right)

        if engine.active_effects:
            names = [
                EFFECTS_BY_ID[eid].name if eid in EFFECTS_BY_ID else eid
                for eid in engine.active_effects
            ]
            self._active_label.setText("Active: " + "  -  ".join(names))
        else:
            self._active_label.setText("No active effects")

        # Prefer the adapter's own latest frame: it proves telemetry is
        # arriving even when the engine is stopped or the emergency stop is
        # latched, which the engine snapshot alone cannot show.
        if adapter is not None and adapter.frames_emitted > 0:
            rpm, max_rpm = adapter.live_rpm, adapter.live_max_rpm
            speed, gear = adapter.live_speed_kph, adapter.live_gear
        else:
            rpm, max_rpm = engine.rpm, engine.max_rpm
            speed, gear = engine.speed_kph, engine.gear

        self._rpm_bar.set_ratio(rpm / max_rpm if max_rpm > 0 else 0.0)
        self._speed_stat.set_value(f"{speed:.0f}")
        self._rpm_stat.set_value(f"{rpm:.0f}")
        self._gear_stat.set_value(_gear_text(gear))
        self._rate_stat.set_value(f"{engine.tick_rate:.0f}")

        if engine.emergency_stop != self._stop_button.isChecked():
            self._stop_button.setChecked(engine.emergency_stop)
        self._stop_button.setText(
            "STOPPED - CLICK TO RESUME" if engine.emergency_stop else "EMERGENCY STOP"
        )


def _gear_text(gear: int) -> str:
    if gear < 0:
        return "R"
    if gear == 0:
        return "N"
    return str(gear)
