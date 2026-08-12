"""Settings - how the app runs, as opposed to how the haptics feel.

The Advanced card is collapsed by default and holds the parameters that can
genuinely make things worse if misunderstood: the motor's physical model
and the update rate.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import AppSettings
from app.core.paths import data_dir
from app.diagnostics.metrics import DiagnosticsReport
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, FieldRow, LabeledSlider, ToggleSwitch


class SettingsPage(Page):
    title = "Settings"
    subtitle = "Application behaviour, devices and telemetry"

    def build(self) -> None:
        self._loading = False
        settings = self.app.settings

        columns = QHBoxLayout()
        columns.setSpacing(16)
        columns.addWidget(self._build_left(settings), 1)
        columns.addWidget(self._build_right(settings), 1)
        self.body.addLayout(columns)
        self.body.addStretch(1)

    # ------------------------------------------------------------------
    def _build_left(self, settings: AppSettings) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        general = Card("General")
        self._start_minimized = self._toggle(settings.start_minimized)
        general.body.addWidget(
            FieldRow("Start minimized", self._start_minimized, "Launch directly to the tray.")
        )
        self._minimize_to_tray = self._toggle(settings.minimize_to_tray)
        general.body.addWidget(
            FieldRow(
                "Close to tray",
                self._minimize_to_tray,
                "Keep running in the background when the window is closed.",
            )
        )
        self._start_engine = self._toggle(settings.start_engine_on_launch)
        general.body.addWidget(
            FieldRow("Start engine on launch", self._start_engine, "Begin processing immediately.")
        )
        self._auto_telemetry = self._toggle(settings.auto_start_telemetry)
        general.body.addWidget(
            FieldRow(
                "Auto-start telemetry",
                self._auto_telemetry,
                "Start listening for the game as soon as the app opens.",
            )
        )
        layout.addWidget(general)

        controller = Card("Controller")
        self._controller_index = QSpinBox()
        self._controller_index.setRange(0, 3)
        self._controller_index.setValue(settings.controller_index)
        self._controller_index.valueChanged.connect(self._on_changed)
        controller.body.addWidget(
            FieldRow("XInput slot", self._controller_index, "Which slot to drive (0-3).")
        )

        self._auto_detect = self._toggle(settings.auto_detect_controller)
        controller.body.addWidget(
            FieldRow(
                "Auto-detect",
                self._auto_detect,
                "Switch to another slot automatically if the chosen one is empty.",
            )
        )

        self._output_limit = LabeledSlider(
            "Master output limit", 0.1, 1.0, settings.master_output_limit, 0.05,
            description="Absolute hardware ceiling, applied after everything else.",
        )
        self._output_limit.valueChanged.connect(self._on_changed)
        controller.body.addWidget(self._output_limit)
        layout.addWidget(controller)

        layout.addStretch(1)
        return container

    def _build_right(self, settings: AppSettings) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        telemetry = Card("Telemetry")
        self._game_combo = QComboBox()
        for adapter in self.app.adapters.values():
            label = adapter.display_name if adapter.supported else f"{adapter.display_name} (n/a)"
            self._game_combo.addItem(label, adapter.game_id)
        index = self._game_combo.findData(settings.game_id)
        if index >= 0:
            self._game_combo.setCurrentIndex(index)
        self._game_combo.currentIndexChanged.connect(self._on_game_changed)
        telemetry.body.addWidget(FieldRow("Game", self._game_combo))

        self._udp_port = QSpinBox()
        self._udp_port.setRange(1024, 65535)
        self._udp_port.setValue(settings.udp_port)
        self._udp_port.valueChanged.connect(self._on_changed)
        telemetry.body.addWidget(
            FieldRow("UDP port", self._udp_port, "Must match the game's telemetry settings.")
        )

        self._telemetry_timeout = QDoubleSpinBox()
        self._telemetry_timeout.setRange(0.1, 10.0)
        self._telemetry_timeout.setSingleStep(0.1)
        self._telemetry_timeout.setSuffix(" s")
        self._telemetry_timeout.setValue(settings.telemetry_timeout)
        self._telemetry_timeout.valueChanged.connect(self._on_changed)
        telemetry.body.addWidget(
            FieldRow(
                "Stale data cutoff",
                self._telemetry_timeout,
                "All game-driven vibration stops once data is older than this.",
            )
        )

        self._connection_timeout = QDoubleSpinBox()
        self._connection_timeout.setRange(0.5, 30.0)
        self._connection_timeout.setSingleStep(0.5)
        self._connection_timeout.setSuffix(" s")
        self._connection_timeout.setValue(settings.connection_timeout)
        self._connection_timeout.valueChanged.connect(self._on_changed)
        telemetry.body.addWidget(
            FieldRow(
                "Disconnect threshold",
                self._connection_timeout,
                "How long without packets before the UI reports no data.",
            )
        )

        self._packet_diagnostics = self._toggle(settings.packet_diagnostics)
        telemetry.body.addWidget(
            FieldRow(
                "Packet diagnostics",
                self._packet_diagnostics,
                "Log detail about rejected packets. Verbose.",
            )
        )
        layout.addWidget(telemetry)

        # --- advanced ---
        advanced = Card(
            "Advanced",
            hint="Physical motor model and loop timing. The defaults are tuned for "
                 "ordinary ERM motors like the Blitz's - change them only if you know "
                 "what you are chasing.",
        )
        self._advanced_body = QWidget()
        advanced_layout = QVBoxLayout(self._advanced_body)
        advanced_layout.setContentsMargins(0, 4, 0, 0)
        advanced_layout.setSpacing(12)

        self._update_rate = QSpinBox()
        self._update_rate.setRange(30, 250)
        self._update_rate.setSuffix(" Hz")
        self._update_rate.setValue(int(settings.update_rate_hz))
        self._update_rate.valueChanged.connect(self._on_changed)
        advanced_layout.addWidget(
            FieldRow(
                "Haptic update rate",
                self._update_rate,
                "120 Hz is plenty: the fastest useful modulation is around 35 Hz.",
            )
        )

        profile = self.app.profiles.active
        self._min_effective = LabeledSlider(
            "Minimum effective drive", 0.0, 0.6, profile.motor.min_effective, 0.01,
            description="Below this level an ERM rotor does not turn at all. Raise it if "
                        "subtle effects are silent; lower it if idle feels too strong.",
        )
        self._slew_rise = LabeledSlider(
            "Motor rise rate", 5.0, 200.0, profile.motor.slew_rise, 5.0, decimals=0,
            description="Units per second. High keeps impacts sharp - the motor's own "
                        "inertia already provides the smoothing.",
        )
        self._slew_fall = LabeledSlider(
            "Motor fall rate", 5.0, 200.0, profile.motor.slew_fall, 5.0, decimals=0,
        )
        for slider in (self._min_effective, self._slew_rise, self._slew_fall):
            slider.valueChanged.connect(self._on_motor_changed)
            advanced_layout.addWidget(slider)

        self._advanced_body.setVisible(False)
        advanced.body.addWidget(self._advanced_body)

        toggle_row = QHBoxLayout()
        toggle_row.addStretch(1)
        self._advanced_button = QPushButton("Show Advanced")
        self._advanced_button.setObjectName("Ghost")
        self._advanced_button.setCheckable(True)
        self._advanced_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._advanced_button.toggled.connect(self._on_advanced_toggled)
        toggle_row.addWidget(self._advanced_button)
        advanced.body.addLayout(toggle_row)
        layout.addWidget(advanced)

        about = Card("Data Location")
        path_label = QLabel(str(data_dir()))
        path_label.setObjectName("Mono")
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        about.body.addWidget(path_label)
        layout.addWidget(about)

        layout.addStretch(1)
        return container

    # ------------------------------------------------------------------
    def _toggle(self, checked: bool) -> ToggleSwitch:
        toggle = ToggleSwitch()
        toggle.setChecked(checked)
        toggle.toggled.connect(self._on_changed)
        return toggle

    def _on_advanced_toggled(self, shown: bool) -> None:
        self._advanced_body.setVisible(shown)
        self._advanced_button.setText("Hide Advanced" if shown else "Show Advanced")

    def _on_game_changed(self) -> None:
        if self._loading:
            return
        game_id = self._game_combo.currentData()
        if game_id:
            self.app.set_game(game_id)

    def _on_motor_changed(self, _value: float) -> None:
        if self._loading:
            return
        profile = self.app.profiles.active
        profile.motor.min_effective = self._min_effective.value()
        profile.motor.slew_rise = self._slew_rise.value()
        profile.motor.slew_fall = self._slew_fall.value()
        self.app.apply_profile(profile)

    def _on_changed(self, *_args) -> None:
        if self._loading:
            return
        settings = self.app.settings

        settings.start_minimized = self._start_minimized.isChecked()
        settings.minimize_to_tray = self._minimize_to_tray.isChecked()
        settings.start_engine_on_launch = self._start_engine.isChecked()
        settings.auto_start_telemetry = self._auto_telemetry.isChecked()
        settings.auto_detect_controller = self._auto_detect.isChecked()
        settings.packet_diagnostics = self._packet_diagnostics.isChecked()
        settings.telemetry_timeout = self._telemetry_timeout.value()
        settings.connection_timeout = self._connection_timeout.value()

        if settings.controller_index != self._controller_index.value():
            self.app.set_controller_index(self._controller_index.value())
        if abs(settings.master_output_limit - self._output_limit.value()) > 1e-6:
            self.app.set_output_limit(self._output_limit.value())
        if settings.udp_port != self._udp_port.value():
            self.app.set_udp_port(self._udp_port.value())
        if int(settings.update_rate_hz) != self._update_rate.value():
            self.app.set_update_rate(float(self._update_rate.value()))

        self.app.engine.set_telemetry_timeout(settings.telemetry_timeout)
        self.app.device_manager.auto_detect = settings.auto_detect_controller
        if self.app.active_adapter is not None:
            self.app.active_adapter.configure(connection_timeout=settings.connection_timeout)
        settings.save()

    def on_shown(self) -> None:
        self._loading = True
        profile = self.app.profiles.active
        self._min_effective.set_value(profile.motor.min_effective)
        self._slew_rise.set_value(profile.motor.slew_rise)
        self._slew_fall.set_value(profile.motor.slew_fall)
        self._controller_index.setValue(self.app.settings.controller_index)
        self._udp_port.setValue(self.app.settings.udp_port)
        self._loading = False

    def refresh(self, report: DiagnosticsReport) -> None:
        return
