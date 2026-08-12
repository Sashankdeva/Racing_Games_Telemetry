"""Haptics page - the global feel controls.

The global smoothing control is deliberately last, defaulted off, and
labelled with why. Every effect already shapes its own signal to suit what
it represents; a filter across the sum can only take that away.
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from app.diagnostics.metrics import DiagnosticsReport
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, LabeledSlider, StatBlock
from app.ui.widgets.meters import HapticScope, MotorMeter


class HapticsPage(Page):
    title = "Haptics"
    subtitle = "Global strength and response. Per-effect tuning lives on the Effects page."

    def build(self) -> None:
        self._loading = False

        columns = QHBoxLayout()
        columns.setSpacing(16)
        columns.addWidget(self._build_controls(), 3)
        columns.addWidget(self._build_monitor(), 2)
        self.body.addLayout(columns, 1)
        self.body.addStretch(1)

    def _build_controls(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        core = Card("Global Feel")

        self._intensity = LabeledSlider(
            "Master Strength", 0.0, 1.5, 1.0, 0.05,
            description="Scales every effect. Above 1.00 pushes past each effect's natural level.",
        )
        self._feel = LabeledSlider(
            "Overall Feel", 0.0, 1.0, 0.55, 0.05,
            description="Soft and rounded, through to firm and immediate. Changes the motor's "
                        "response curve, not its speed.",
        )
        self._response = LabeledSlider(
            "Response", 0.0, 1.0, 0.85, 0.05,
            description="How fast output is allowed to change. High is recommended - the motor's "
                        "own inertia already smooths the signal.",
        )
        self._dynamic_range = LabeledSlider(
            "Dynamic Range", 0.0, 1.0, 1.0, 0.05,
            description="Full range keeps subtle effects subtle. Lower it to bring quiet effects "
                        "closer to loud ones if your motors feel weak.",
        )

        for slider in (self._intensity, self._feel, self._response, self._dynamic_range):
            slider.valueChanged.connect(self._on_changed)
            core.body.addWidget(slider)
        layout.addWidget(core)

        limits = Card("Output Limit")
        self._output_limit = LabeledSlider(
            "Maximum Output", 0.1, 1.0, 1.0, 0.05,
            description="Hard ceiling applied last, after everything else. A safety limit rather "
                        "than a tuning control.",
        )
        self._output_limit.valueChanged.connect(self._on_changed)
        limits.body.addWidget(self._output_limit)
        layout.addWidget(limits)

        smoothing = Card(
            "Global Smoothing",
            hint="Off by default, and best left there. Each effect already applies the shaping "
                 "that suits it - a gear shift needs a sub-10 ms attack while body movement wants "
                 "a few Hz of filtering. One filter across the sum flattens both into the same "
                 "mush. Use this only if the output feels harsh on your hardware.",
        )
        self._smoothing = LabeledSlider(
            "Smoothing Amount", 0.0, 1.0, 0.0, 0.05,
            description="0.00 = off (recommended).",
        )
        self._smoothing.valueChanged.connect(self._on_changed)
        smoothing.body.addWidget(self._smoothing)

        reset = QPushButton("Reset Global Settings")
        reset.setObjectName("Ghost")
        reset.clicked.connect(self._on_reset)
        smoothing.body.addWidget(reset)
        layout.addWidget(smoothing)

        layout.addStretch(1)
        return container

    def _build_monitor(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        monitor = Card("Live Output")
        self._left_meter = MotorMeter("LEFT")
        self._right_meter = MotorMeter("RIGHT")
        monitor.body.addWidget(self._left_meter)
        monitor.body.addWidget(self._right_meter)

        self._scope = HapticScope()
        monitor.body.addWidget(self._scope, 1)

        stats = QHBoxLayout()
        stats.setSpacing(20)
        self._rate_stat = StatBlock("Update Rate", "0", "Hz")
        self._limit_stat = StatBlock("Limiter", "-")
        stats.addWidget(self._rate_stat)
        stats.addWidget(self._limit_stat)
        monitor.body.addLayout(stats)
        layout.addWidget(monitor, 1)
        return container

    # ------------------------------------------------------------------
    def _on_changed(self, _value: float) -> None:
        if self._loading:
            return
        profile = self.app.profiles.active
        profile.master.intensity = self._intensity.value()
        profile.master.feel = self._feel.value()
        profile.master.response = self._response.value()
        profile.master.dynamic_range = self._dynamic_range.value()
        profile.master.output_limit = self._output_limit.value()
        profile.master.global_smoothing = self._smoothing.value()
        self.app.apply_profile(profile)

    def _on_reset(self) -> None:
        profile = self.app.profiles.active
        from app.profiles.schema import MasterConfig

        profile.master = MasterConfig()
        self.app.apply_profile(profile)
        self.on_shown()

    def on_shown(self) -> None:
        master = self.app.profiles.active.master
        self._loading = True
        self._intensity.set_value(master.intensity)
        self._feel.set_value(master.feel)
        self._response.set_value(master.response)
        self._dynamic_range.set_value(master.dynamic_range)
        self._output_limit.set_value(master.output_limit)
        self._smoothing.set_value(master.global_smoothing)
        self._loading = False

    def refresh(self, report: DiagnosticsReport) -> None:
        engine = report.engine
        self._left_meter.set_value(engine.left)
        self._right_meter.set_value(engine.right)
        self._scope.push(engine.left, engine.right)
        self._rate_stat.set_value(f"{engine.tick_rate:.0f}")
        self._limit_stat.set_value("Active" if engine.limited else "-")
