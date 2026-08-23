"""Track database page - editable circuit characteristics."""

from __future__ import annotations

from PySide6.QtWidgets import QDoubleSpinBox, QSpinBox, QWidget

from app.domain.track_profiles import RATING_FIELDS
from app.ui.pages.profile_page import ProfileEditorPage
from app.ui.widgets.common import Card, FieldRow


class TrackPage(ProfileEditorPage):
    title = "Track"
    subtitle = "Circuit characteristics used by the strategy engine"
    selection_attr = "selected_track"
    store_attr = "tracks"
    rating_fields = RATING_FIELDS
    record_noun = "track"
    prior_hint = (
        "Initial assumptions per circuit. Observed degradation and pace "
        "from the live session should override these as evidence builds."
    )

    def build_extra(self) -> QWidget | None:
        card = Card(
            "Measured values",
            hint="Unlike the ratings above, these are real quantities rather "
                 "than subjective scores.",
        )
        self._pit_loss = QDoubleSpinBox()
        self._pit_loss.setRange(10.0, 45.0)
        self._pit_loss.setSingleStep(0.5)
        self._pit_loss.setSuffix(" s")
        card.body.addWidget(
            FieldRow(
                "Pit lane time loss",
                self._pit_loss,
                "Pit entry to pit exit including the stop itself.",
            )
        )

        self._race_laps = QSpinBox()
        self._race_laps.setRange(1, 120)
        card.body.addWidget(FieldRow("Race distance", self._race_laps, "Laps."))
        return card

    def load_extra(self, record) -> None:
        self._pit_loss.setValue(record.pit_loss_s)
        self._race_laps.setValue(record.race_laps)

    def apply_extra(self, record):
        record.pit_loss_s = self._pit_loss.value()
        record.race_laps = self._race_laps.value()
        return record
