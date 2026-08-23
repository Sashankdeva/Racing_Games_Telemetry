"""Settings - how the application runs.

Split by scope, which matters here: telemetry transport belongs to the
active GAME MODE and is stored per mode, while window behaviour and logging
are global. Switching F1 25 <-> F1 26 therefore preserves each mode's own
port, timeouts and preferences.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QLabel,
    QSpinBox,
)

from app.config.settings import AppSettings
from app.core.paths import data_dir
from app.diagnostics.metrics import DiagnosticsReport
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, FieldRow, StatBlock, ToggleSwitch


class SettingsPage(Page):
    title = "Settings"
    subtitle = "Telemetry transport and application behaviour"

    def _build_profile_card(self) -> None:
        """Car & Track intelligence: what is shipped, and what was learned.

        Reads the derived context; no database detail is exposed here.
        """
        card = Card(
            "Car & track intelligence",
            hint="Shipped profile data and what has been learned from your "
                 "own sessions, kept separate.",
        )

        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(12)
        self._profile_game = StatBlock("Game", "-")
        self._profile_car = StatBlock("Car", "-")
        self._profile_track = StatBlock("Track", "-")
        self._profile_status = StatBlock("Profile Status", "-")
        for index, widget in enumerate(
            (self._profile_game, self._profile_car,
             self._profile_track, self._profile_status)
        ):
            grid.addWidget(widget, index // 2, index % 2)
        card.body.addLayout(grid)

        learned = QLabel("OBSERVED")
        learned.setObjectName("StatLabel")
        card.body.addWidget(learned)

        self._profile_observed = QLabel("Nothing learned yet.")
        self._profile_observed.setObjectName("Mono")
        self._profile_observed.setWordWrap(True)
        card.body.addWidget(self._profile_observed)

        self._profile_quality = QLabel("")
        self._profile_quality.setObjectName("Hint")
        self._profile_quality.setWordWrap(True)
        card.body.addWidget(self._profile_quality)

        self.body.addWidget(card)

    def _refresh_profile_card(self) -> None:
        context = self.app.profile_context()

        self._profile_game.set_value(self.app.game.display_name)
        self._profile_car.set_value(
            context.car.name if context.car else "UNKNOWN"
        )
        self._profile_track.set_value(
            context.track.name if context.track else "UNKNOWN"
        )
        # Say plainly whether the shipped numbers are measured or assumed.
        if context.car is None:
            status = "UNKNOWN"
        elif context.car.is_prior:
            status = "DEFAULT (prior)"
        else:
            status = "EDITED"
        self._profile_status.set_value(status)

        observed = context.observed_car
        rows = []
        if observed is not None:
            for name, value in sorted(observed.values.items()):
                rows.append(
                    f"{name:<24} {value.value:<10} "
                    f"{value.sample_count} laps   {value.confidence.value}"
                )
        self._profile_observed.setText(
            "\n".join(rows) if rows else "Nothing learned yet."
        )
        self._profile_quality.setText(
            f"Last session: {context.quality.describe()}."
            if context.quality.total
            else "Complete clean laps to begin learning."
        )

    def build(self) -> None:
        self._loading = False
        self._build_profile_card()
        settings = self.app.settings
        mode_settings = self.app.mode_settings

        telemetry = Card("Telemetry")

        self._game_combo = QComboBox()
        for adapter in self.app.adapters.values():
            label = (
                adapter.display_name
                if adapter.supported
                else f"{adapter.display_name} (n/a)"
            )
            self._game_combo.addItem(label, adapter.game_id)
        index = self._game_combo.findData(settings.game_id)
        if index >= 0:
            self._game_combo.setCurrentIndex(index)
        self._game_combo.currentIndexChanged.connect(self._on_game_changed)
        telemetry.body.addWidget(FieldRow("Game", self._game_combo))

        self._udp_port = QSpinBox()
        self._udp_port.setRange(1024, 65535)
        self._udp_port.setValue(mode_settings.udp_port)
        self._udp_port.valueChanged.connect(self._on_changed)
        telemetry.body.addWidget(
            FieldRow(
                "UDP port",
                self._udp_port,
                "Must match the game's telemetry settings.",
            )
        )

        self._telemetry_timeout = QDoubleSpinBox()
        self._telemetry_timeout.setRange(0.1, 10.0)
        self._telemetry_timeout.setSingleStep(0.1)
        self._telemetry_timeout.setSuffix(" s")
        self._telemetry_timeout.setValue(mode_settings.telemetry_timeout)
        self._telemetry_timeout.valueChanged.connect(self._on_changed)
        telemetry.body.addWidget(
            FieldRow(
                "Stale data cutoff",
                self._telemetry_timeout,
                "Telemetry older than this reports as no-data rather than "
                "being left frozen on its last value.",
            )
        )

        self._connection_timeout = QDoubleSpinBox()
        self._connection_timeout.setRange(0.5, 30.0)
        self._connection_timeout.setSingleStep(0.5)
        self._connection_timeout.setSuffix(" s")
        self._connection_timeout.setValue(mode_settings.connection_timeout)
        self._connection_timeout.valueChanged.connect(self._on_changed)
        telemetry.body.addWidget(
            FieldRow(
                "Disconnect threshold",
                self._connection_timeout,
                "How long without packets before the UI reports no data.",
            )
        )

        self._auto_telemetry = self._toggle(mode_settings.auto_start_telemetry)
        telemetry.body.addWidget(
            FieldRow(
                "Auto-start telemetry",
                self._auto_telemetry,
                "Start listening as soon as the app opens.",
            )
        )
        self.body.addWidget(telemetry)

        general = Card("General")
        self._start_minimized = self._toggle(settings.start_minimized)
        general.body.addWidget(
            FieldRow(
                "Start minimized",
                self._start_minimized,
                "Launch directly to the tray.",
            )
        )
        self._minimize_to_tray = self._toggle(settings.minimize_to_tray)
        general.body.addWidget(
            FieldRow(
                "Close to tray",
                self._minimize_to_tray,
                "Keep running in the background when the window is closed.",
            )
        )
        self._verbose = self._toggle(settings.verbose_logging)
        general.body.addWidget(
            FieldRow(
                "Verbose logging",
                self._verbose,
                "Adds debug detail. Normal mode stays quiet on purpose.",
            )
        )
        self.body.addWidget(general)

        location = Card("Data Location")
        path_label = QLabel(str(data_dir()))
        path_label.setObjectName("Mono")
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        location.body.addWidget(path_label)
        self.body.addWidget(location)

        self.body.addStretch(1)

    # ------------------------------------------------------------------
    def _toggle(self, checked: bool) -> ToggleSwitch:
        toggle = ToggleSwitch()
        toggle.setChecked(checked)
        toggle.toggled.connect(self._on_changed)
        return toggle

    def _on_game_changed(self) -> None:
        if self._loading:
            return
        game_id = self._game_combo.currentData()
        if game_id:
            self.app.set_game(game_id)

    def _on_changed(self, *_args) -> None:
        if self._loading:
            return
        settings: AppSettings = self.app.settings
        mode_settings = self.app.mode_settings

        settings.start_minimized = self._start_minimized.isChecked()
        settings.minimize_to_tray = self._minimize_to_tray.isChecked()
        mode_settings.auto_start_telemetry = self._auto_telemetry.isChecked()
        mode_settings.connection_timeout = self._connection_timeout.value()

        if settings.verbose_logging != self._verbose.isChecked():
            from app.core.logging import set_verbose

            settings.verbose_logging = self._verbose.isChecked()
            set_verbose(settings.verbose_logging)

        if mode_settings.udp_port != self._udp_port.value():
            self.app.set_udp_port(self._udp_port.value())
        if abs(mode_settings.telemetry_timeout - self._telemetry_timeout.value()) > 1e-6:
            self.app.set_telemetry_timeout(self._telemetry_timeout.value())

        if self.app.active_adapter is not None:
            self.app.active_adapter.configure(
                connection_timeout=mode_settings.connection_timeout
            )
        settings.save()
        self.app.save_mode_settings()

    def on_shown(self) -> None:
        self._loading = True
        self._udp_port.setValue(self.app.mode_settings.udp_port)
        self._telemetry_timeout.setValue(self.app.mode_settings.telemetry_timeout)
        self._loading = False
        # Profiles change on lap completion, not continuously, so this
        # refreshes on entry rather than on the UI timer.
        self._refresh_profile_card()

    def refresh(self, report: DiagnosticsReport) -> None:
        return
