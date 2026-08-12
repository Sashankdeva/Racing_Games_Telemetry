"""Controller page - device status and the Haptic Test Lab.

The Test Lab exists so the hardware can be proven independently of any
game: if a pattern fires here, the whole output chain works, and any
remaining problem is telemetry-side. Patterns are hard-capped in duration
by the scheduler, so nothing here can leave the motors running.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.diagnostics.metrics import DiagnosticsReport
from app.haptics.patterns import PRESETS, MotorTarget, PatternKind, PatternSpec
from app.ui import theme
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, FieldRow, LabeledSlider, StatBlock, StatusPill
from app.ui.widgets.meters import MotorMeter


class ControllerPage(Page):
    title = "Controller"
    subtitle = "Device status, motor tests and the Haptic Test Lab"

    def build(self) -> None:
        columns = QHBoxLayout()
        columns.setSpacing(16)
        columns.addWidget(self._build_left(), 2)
        columns.addWidget(self._build_lab(), 3)
        self.body.addLayout(columns, 1)
        self.body.addStretch(1)

    # ------------------------------------------------------------------
    def _build_left(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        device = Card("Device")
        self._status_pill = StatusPill("Checking...", theme.IDLE)
        device.body.addWidget(self._status_pill)

        self._name_label = QLabel("-")
        self._name_label.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {theme.TEXT};")
        device.body.addWidget(self._name_label)

        self._connection_label = QLabel("-")
        self._connection_label.setObjectName("Hint")
        device.body.addWidget(self._connection_label)

        self._index_spin = QSpinBox()
        self._index_spin.setRange(0, 3)
        self._index_spin.setValue(self.app.settings.controller_index)
        self._index_spin.valueChanged.connect(self.app.set_controller_index)
        device.body.addWidget(
            FieldRow(
                "XInput Slot",
                self._index_spin,
                "Which of the four XInput slots to drive. The Blitz normally lands on 0.",
            )
        )
        layout.addWidget(device)

        # --- quick motor tests ---
        tests = Card("Motor Test", hint="Half-second burst at 80% on the selected motor.")
        row = QHBoxLayout()
        row.setSpacing(8)
        for label, target in (
            ("Left", MotorTarget.LEFT),
            ("Right", MotorTarget.RIGHT),
            ("Both", MotorTarget.BOTH),
        ):
            button = QPushButton(label)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, t=target: self._quick_test(t))
            row.addWidget(button)
        tests.body.addLayout(row)
        layout.addWidget(tests)

        # --- live output ---
        output = Card("Live Output")
        self._left_meter = MotorMeter("LEFT")
        self._right_meter = MotorMeter("RIGHT")
        output.body.addWidget(self._left_meter)
        output.body.addWidget(self._right_meter)

        stats = QHBoxLayout()
        stats.setSpacing(20)
        self._writes_stat = StatBlock("Writes OK", "0")
        self._fails_stat = StatBlock("Failed", "0")
        stats.addWidget(self._writes_stat)
        stats.addWidget(self._fails_stat)
        output.body.addLayout(stats)
        layout.addWidget(output)

        stop = QPushButton("EMERGENCY STOP")
        stop.setObjectName("EmergencyStop")
        stop.setCursor(Qt.CursorShape.PointingHandCursor)
        stop.setMinimumHeight(52)
        stop.clicked.connect(self._on_stop)
        layout.addWidget(stop)

        layout.addStretch(1)
        return container

    # ------------------------------------------------------------------
    def _build_lab(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        presets = Card("Presets", hint="One-click reference patterns.")
        grid = QGridLayout()
        grid.setSpacing(8)
        for index, name in enumerate(PRESETS):
            button = QPushButton(name)
            button.setObjectName("Preset")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, n=name: self._play_preset(n))
            grid.addWidget(button, index // 4, index % 4)
        presets.body.addLayout(grid)
        layout.addWidget(presets)

        lab = Card("Haptic Test Lab", hint="Build a custom pattern and fire it at the motors.")

        self._kind_combo = QComboBox()
        for kind in PatternKind:
            self._kind_combo.addItem(kind.value.replace("_", " ").title(), kind)
        lab.body.addWidget(FieldRow("Pattern", self._kind_combo))

        self._target_combo = QComboBox()
        for target in MotorTarget:
            self._target_combo.addItem(target.value.title(), target)
        self._target_combo.setCurrentIndex(2)  # both
        lab.body.addWidget(FieldRow("Motor", self._target_combo))

        self._intensity = LabeledSlider("Intensity", 0.0, 1.0, 0.7, 0.05)
        self._duration = LabeledSlider("Duration", 0.1, 10.0, 2.0, 0.1, suffix=" s", decimals=1)
        self._pulse_rate = LabeledSlider(
            "Pulse Rate", 0.5, 38.0, 8.0, 0.5, suffix=" Hz", decimals=1,
            description="Above about 35 Hz the motor can no longer track individual pulses.",
        )
        self._attack = LabeledSlider("Attack", 0.0, 1.0, 0.02, 0.01, suffix=" s")
        self._release = LabeledSlider("Release", 0.0, 2.0, 0.10, 0.01, suffix=" s")
        self._sharpness = LabeledSlider("Sharpness", 0.0, 1.0, 0.8, 0.05)

        for slider in (
            self._intensity,
            self._duration,
            self._pulse_rate,
            self._attack,
            self._release,
            self._sharpness,
        ):
            lab.body.addWidget(slider)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        play = QPushButton("Play Pattern")
        play.setObjectName("Primary")
        play.setCursor(Qt.CursorShape.PointingHandCursor)
        play.clicked.connect(self._play_custom)
        buttons.addWidget(play)

        stop = QPushButton("Stop")
        stop.setObjectName("Danger")
        stop.setCursor(Qt.CursorShape.PointingHandCursor)
        stop.clicked.connect(self.app.stop_test_patterns)
        buttons.addWidget(stop)
        buttons.addStretch(1)
        lab.body.addLayout(buttons)

        layout.addWidget(lab)
        layout.addStretch(1)
        return container

    # ------------------------------------------------------------------
    def _quick_test(self, target: MotorTarget) -> None:
        self.app.play_test_pattern(
            PatternSpec(
                kind=PatternKind.CONSTANT,
                intensity=0.8,
                duration=0.5,
                target=target,
                attack=0.02,
                release=0.08,
            )
        )

    def _play_preset(self, name: str) -> None:
        spec = PRESETS.get(name)
        if spec is None:
            return
        target = self._target_combo.currentData() or MotorTarget.BOTH
        self.app.play_test_pattern(
            PatternSpec(
                kind=spec.kind,
                intensity=spec.intensity,
                duration=spec.duration,
                target=target,
                pulse_rate=spec.pulse_rate,
                attack=spec.attack,
                release=spec.release,
                sharpness=spec.sharpness,
            )
        )

    def _play_custom(self) -> None:
        self.app.play_test_pattern(
            PatternSpec(
                kind=self._kind_combo.currentData(),
                intensity=self._intensity.value(),
                duration=self._duration.value(),
                target=self._target_combo.currentData(),
                pulse_rate=self._pulse_rate.value(),
                attack=self._attack.value(),
                release=self._release.value(),
                sharpness=self._sharpness.value(),
            )
        )

    def _on_stop(self) -> None:
        self.app.emergency_stop()
        self.app.clear_emergency_stop()

    def on_shown(self) -> None:
        self._index_spin.setValue(self.app.settings.controller_index)

    def refresh(self, report: DiagnosticsReport) -> None:
        if report.controller_connected:
            self._status_pill.set_state("Connected", theme.LIVE)
        else:
            self._status_pill.set_state("Disconnected", theme.DANGER)

        self._name_label.setText(report.controller_name or "-")
        detail = f"XInput slot {report.controller_index}"
        if report.connected_indices:
            detail += "  -  active slots: " + ", ".join(str(i) for i in report.connected_indices)
        if report.xinput_dll:
            detail += f"  -  {report.xinput_dll}"
        self._connection_label.setText(detail)

        self._left_meter.set_value(report.engine.left)
        self._right_meter.set_value(report.engine.right)
        self._writes_stat.set_value(f"{report.rumble_writes_ok}")
        self._fails_stat.set_value(f"{report.rumble_writes_failed}")
