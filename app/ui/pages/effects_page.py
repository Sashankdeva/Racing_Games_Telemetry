"""Effects page - one card per effect.

Each card shows only what a normal user needs: on/off, strength, when it
kicks in, its character, and left/right bias. Threshold and balance are
hidden for effects where they are meaningless, and the genuinely technical
parameters stay behind an "Advanced" toggle that is off by default.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.diagnostics.metrics import DiagnosticsReport
from app.haptics.effects import EFFECT_CLASSES, Effect
from app.haptics.effects.base import EffectSettings
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, LabeledSlider, ToggleSwitch
from app.ui.widgets.meters import ActivityDot


class EffectCard(Card):
    """Controls for one effect, plus a live activity indicator."""

    def __init__(self, effect_class: type[Effect], page: "EffectsPage") -> None:
        super().__init__(effect_class.name, effect_class.description)
        self.effect_class = effect_class
        self.page = page
        self._loading = False

        self._activity = ActivityDot()
        self.add_header_widget(self._activity)

        self._toggle = ToggleSwitch()
        self._toggle.toggled.connect(self._on_changed)
        self.add_header_widget(self._toggle)

        self._intensity = LabeledSlider("Intensity", 0.0, 2.0, 1.0, 0.05)
        self.body.addWidget(self._intensity)

        self._threshold = LabeledSlider(
            "Threshold", 0.0, 0.95, 0.0, 0.01,
            description="How much has to be happening before this effect appears.",
        )
        self.body.addWidget(self._threshold)

        self._sharpness = LabeledSlider(effect_class.sharpness_label, 0.0, 1.0, 0.5, 0.05)
        self.body.addWidget(self._sharpness)

        self._balance = LabeledSlider(
            "Left / Right Balance", -1.0, 1.0, 0.0, 0.05,
            description="Centre uses both motors. Only ever attenuates one side.",
        )
        if effect_class.supports_balance:
            self.body.addWidget(self._balance)

        # --- advanced ---
        self._advanced = QWidget()
        advanced_layout = QVBoxLayout(self._advanced)
        advanced_layout.setContentsMargins(0, 4, 0, 0)
        advanced_layout.setSpacing(12)
        self._response = LabeledSlider(
            "Response Curve", 0.3, 3.0, 1.0, 0.05,
            description="Below 1 reacts sooner to small inputs; above 1 weights the top end.",
        )
        advanced_layout.addWidget(self._response)
        self._advanced.setVisible(False)
        self.body.addWidget(self._advanced)

        self._advanced_button = QPushButton("Advanced")
        self._advanced_button.setObjectName("Ghost")
        self._advanced_button.setCheckable(True)
        self._advanced_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._advanced_button.toggled.connect(self._advanced.setVisible)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self._advanced_button)
        self.body.addLayout(row)

        for slider in (
            self._intensity,
            self._threshold,
            self._sharpness,
            self._balance,
            self._response,
        ):
            slider.valueChanged.connect(self._on_changed)

    def load(self, settings: EffectSettings) -> None:
        self._loading = True
        self._toggle.setChecked(settings.enabled)
        self._intensity.set_value(settings.intensity)
        self._threshold.set_value(settings.threshold)
        self._sharpness.set_value(settings.sharpness)
        self._balance.set_value(settings.balance)
        self._response.set_value(settings.response)
        self._loading = False
        self._update_enabled()

    def _update_enabled(self) -> None:
        enabled = self._toggle.isChecked()
        for slider in (
            self._intensity,
            self._threshold,
            self._sharpness,
            self._balance,
            self._response,
        ):
            slider.set_enabled(enabled)

    def _on_changed(self, *_args) -> None:
        if self._loading:
            return
        self._update_enabled()
        self.page.apply_card(self)

    def to_settings(self, existing: EffectSettings) -> EffectSettings:
        """Write the controls back, preserving advanced keys we do not show."""
        settings = existing.copy()
        settings.enabled = self._toggle.isChecked()
        settings.intensity = self._intensity.value()
        settings.threshold = self._threshold.value()
        settings.sharpness = self._sharpness.value()
        settings.balance = self._balance.value()
        settings.response = self._response.value()
        return settings

    def set_activity(self, level: float) -> None:
        self._activity.set_level(level)


class EffectsPage(Page):
    title = "Effects"
    subtitle = "Each effect has its own signal character. Tune them independently."

    def build(self) -> None:
        header = QHBoxLayout()
        header.addStretch(1)
        reset = QPushButton("Reset All Effects")
        reset.setObjectName("Ghost")
        reset.clicked.connect(self._on_reset_all)
        header.addWidget(reset)
        self.body.addLayout(header)

        grid = QGridLayout()
        grid.setSpacing(16)
        self._cards: dict[str, EffectCard] = {}

        for index, effect_class in enumerate(EFFECT_CLASSES):
            card = EffectCard(effect_class, self)
            self._cards[effect_class.id] = card
            grid.addWidget(card, index // 2, index % 2)

        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self.body.addLayout(grid)
        self.body.addStretch(1)

    def apply_card(self, card: EffectCard) -> None:
        profile = self.app.profiles.active
        effect_id = card.effect_class.id
        profile.effects[effect_id] = card.to_settings(profile.effect(effect_id))
        self.app.apply_profile(profile)

    def _on_reset_all(self) -> None:
        from app.profiles.schema import default_effect_settings

        profile = self.app.profiles.active
        for effect_class in EFFECT_CLASSES:
            profile.effects[effect_class.id] = default_effect_settings(effect_class.id)
        self.app.apply_profile(profile)
        self.on_shown()

    def on_shown(self) -> None:
        profile = self.app.profiles.active
        for effect_id, card in self._cards.items():
            card.load(profile.effect(effect_id))

    def refresh(self, report: DiagnosticsReport) -> None:
        for effect_id, card in self._cards.items():
            effect = self.app.engine.effect_by_id(effect_id)
            card.set_activity(effect.last_output.peak if effect else 0.0)
