"""Setup - ERS, straight-line aid, and strategy parameters for the active game.

Every control on this page is built from `GameProfile`. There is no
`if mode is F1_26` anywhere: the page asks the profile what the game has -
DRS or active aero, MGU-H or not, which deploy modes exist - and renders
that. Adding F1 27 means adding a profile, not editing this file.

Where a regulation difference is real but its telemetry representation has
not been verified here, the profile says so and this page labels it
UNCONFIRMED rather than presenting it as fact.
"""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QLabel, QVBoxLayout, QWidget

from app.diagnostics.metrics import DiagnosticsReport
from app.games.modes import Capability
from app.ui import theme
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, FieldRow, LabeledSlider, ToggleSwitch


def _badge(text: str, colour: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(
        f"font-size: 10px; font-weight: 700; color: {colour}; "
        f"letter-spacing: 0.8px;"
    )
    return label


class SetupPage(Page):
    title = "Setup"
    subtitle = "Energy and aero configuration for the active game mode"

    def build(self) -> None:
        self._loading = False
        # Rebuilt wholesale on a mode switch, because the *controls*
        # themselves differ between titles - not just their values.
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(16)
        self.body.addWidget(self._container)
        self.body.addStretch(1)
        self._build_for_mode()

    # ------------------------------------------------------------------
    def _clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _build_for_mode(self) -> None:
        self._clear()
        game = self.app.game
        settings = self.app.mode_settings

        self._layout.addWidget(self._build_header(game))
        self._layout.addWidget(self._build_ers(game, settings))
        self._layout.addWidget(self._build_aero(game, settings))
        self._layout.addWidget(self._build_strategy(game, settings))

    def _build_header(self, game) -> QWidget:
        card = Card(
            f"{game.display_name} configuration",
            hint=game.notes,
        )
        return card

    # --- ERS ----------------------------------------------------------
    def _build_ers(self, game, settings) -> QWidget:
        ers = game.ers
        card = Card(ers.label, hint=ers.notes)

        if ers.telemetry_unconfirmed:
            card.body.addWidget(
                _badge("UNCONFIRMED - telemetry mapping not verified", theme.WARN)
            )

        spec = QLabel(
            f"Store {ers.store_joules / 1e6:.0f} MJ    "
            f"Deployment {ers.max_deploy_kw:.0f} kW    "
            f"MGU-H {'yes' if ers.has_mguh else 'no'}    "
            f"Electrical share {ers.electrical_share * 100:.0f}%"
        )
        spec.setObjectName("Mono")
        card.body.addWidget(spec)

        self._ers_mode = QComboBox()
        for mode_name in ers.modes:
            self._ers_mode.addItem(mode_name, mode_name)
        index = self._ers_mode.findData(settings.ers_default_mode)
        if index >= 0:
            self._ers_mode.setCurrentIndex(index)
        self._ers_mode.currentIndexChanged.connect(self._on_changed)
        card.body.addWidget(FieldRow(ers.deploy_label, self._ers_mode))

        self._ers_reserve = LabeledSlider(
            "Energy reserve", 0.0, 0.9, settings.ers_reserve, 0.05,
            description="Fraction of the store kept in hand rather than deployed.",
        )
        self._ers_reserve.valueChanged.connect(self._on_changed)
        card.body.addWidget(self._ers_reserve)

        self._ers_auto = ToggleSwitch()
        self._ers_auto.setChecked(settings.ers_auto_manage)
        self._ers_auto.toggled.connect(self._on_changed)
        card.body.addWidget(
            FieldRow(
                "Automatic management",
                self._ers_auto,
                "Let the assistant suggest deployment rather than fixing a mode.",
            )
        )
        return card

    # --- DRS / active aero --------------------------------------------
    def _build_aero(self, game, settings) -> QWidget:
        drs = game.drs
        card = Card(drs.label, hint=drs.description)

        if drs.telemetry_unconfirmed:
            card.body.addWidget(
                _badge("UNCONFIRMED - telemetry mapping not verified", theme.WARN)
            )
        if drs.notes:
            note = QLabel(drs.notes)
            note.setObjectName("Hint")
            note.setWordWrap(True)
            card.body.addWidget(note)

        # Only offer aero-mode selection where the game actually has it.
        self._aero_mode = None
        if drs.has_active_aero and drs.aero_modes:
            self._aero_mode = QComboBox()
            for aero in drs.aero_modes:
                self._aero_mode.addItem(aero, aero)
            index = self._aero_mode.findData(settings.aero_default_mode)
            if index >= 0:
                self._aero_mode.setCurrentIndex(index)
            self._aero_mode.currentIndexChanged.connect(self._on_changed)
            card.body.addWidget(FieldRow("Default wing state", self._aero_mode))

        self._drs_alert = ToggleSwitch()
        self._drs_alert.setChecked(settings.drs_alert_enabled)
        self._drs_alert.toggled.connect(self._on_changed)
        card.body.addWidget(
            FieldRow(
                f"{game.term('drs')} alerts",
                self._drs_alert,
                f"Notify when the gap ahead approaches the "
                f"{drs.activation_gap_s:.1f}s activation threshold.",
            )
        )

        self._drs_gap = QDoubleSpinBox()
        self._drs_gap.setRange(0.1, 5.0)
        self._drs_gap.setSingleStep(0.1)
        self._drs_gap.setSuffix(" s")
        self._drs_gap.setValue(settings.drs_alert_gap_s)
        self._drs_gap.valueChanged.connect(self._on_changed)
        card.body.addWidget(FieldRow("Alert at gap", self._drs_gap))

        status = self.app.game.status(
            Capability.ACTIVE_AERO if drs.has_active_aero else Capability.DRS
        )
        card.body.addWidget(
            _badge(
                f"Telemetry capability: {status.upper()}",
                {"available": theme.LIVE, "unconfirmed": theme.WARN}.get(
                    status, theme.TEXT_FAINT
                ),
            )
        )
        return card

    # --- strategy ------------------------------------------------------
    def _build_strategy(self, game, settings) -> QWidget:
        params = game.strategy
        card = Card(
            "Strategy parameters",
            hint=params.notes or "Per-title assumptions the strategy engine will use.",
        )

        spec = QLabel(
            f"Compounds: {', '.join(params.dry_compounds)}    "
            f"Mandatory changes: {params.mandatory_compounds}    "
            f"Default pit loss: {params.default_pit_loss_s:.1f}s    "
            f"Confidence: {params.confidence * 100:.0f}%"
        )
        spec.setObjectName("Mono")
        spec.setWordWrap(True)
        card.body.addWidget(spec)

        self._aggression = LabeledSlider(
            "Strategy aggression", 0.0, 1.0, settings.strategy_aggression, 0.05,
            description="Higher favours undercuts and offset strategies over track position.",
        )
        self._aggression.valueChanged.connect(self._on_changed)
        card.body.addWidget(self._aggression)

        self._coaching = LabeledSlider(
            "Coaching sensitivity", 0.0, 1.0, settings.coaching_sensitivity, 0.05,
            description="How readily the coach speaks up. Lower means only major issues.",
        )
        self._coaching.valueChanged.connect(self._on_changed)
        card.body.addWidget(self._coaching)

        card.body.addWidget(
            _badge(
                "Strategy engine not built yet - these are stored per mode, ready for it",
                theme.TEXT_FAINT,
            )
        )
        return card

    # ------------------------------------------------------------------
    def _on_changed(self, *_args) -> None:
        if self._loading:
            return
        settings = self.app.mode_settings
        settings.ers_default_mode = self._ers_mode.currentData() or ""
        settings.ers_reserve = self._ers_reserve.value()
        settings.ers_auto_manage = self._ers_auto.isChecked()
        settings.drs_alert_enabled = self._drs_alert.isChecked()
        settings.drs_alert_gap_s = self._drs_gap.value()
        if self._aero_mode is not None:
            settings.aero_default_mode = self._aero_mode.currentData() or ""
        settings.strategy_aggression = self._aggression.value()
        settings.coaching_sensitivity = self._coaching.value()
        self.app.save_mode_settings()

    def on_shown(self) -> None:
        # The controls themselves are mode-dependent, so rebuild rather
        # than merely reloading values.
        self._loading = True
        self._build_for_mode()
        self._loading = False

    def refresh(self, report: DiagnosticsReport) -> None:
        return
