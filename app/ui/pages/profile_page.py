"""Shared editor for the car and track databases.

Both are the same shape - pick a record, adjust sliders, save or reset - so
they share one page implementation rather than two near-identical ones.

The "prior" framing is surfaced in the UI on purpose: these numbers are
starting assumptions the strategy engine uses before it has evidence, and
the page says so rather than presenting them as measured fact.
"""

from __future__ import annotations


from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from app.diagnostics.metrics import DiagnosticsReport
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, LabeledSlider


class ProfileEditorPage(Page):
    """Subclasses supply the store, the field list and the labels."""

    #: Attribute on Application holding the RecordStore.
    store_attr = ""
    #: ((field_name, label), ...) for the 0-100 sliders.
    rating_fields: tuple[tuple[str, str], ...] = ()
    #: Human name for one record, used in hints.
    record_noun = "profile"
    prior_hint = ""
    #: ModeSettings attribute holding this page's selection, so the choice
    #: is remembered per game mode rather than globally.
    selection_attr = ""

    def build(self) -> None:
        self._loading = False
        self._sliders: dict[str, LabeledSlider] = {}

        header = Card("Select", hint=self.prior_hint)
        row = QHBoxLayout()
        row.setSpacing(12)

        self._combo = QComboBox()
        self._combo.currentIndexChanged.connect(self._on_select)
        row.addWidget(self._combo, 1)

        self._status = QLabel("")
        self._status.setObjectName("Hint")
        row.addWidget(self._status)
        header.body.addLayout(row)
        self.body.addWidget(header)

        ratings = Card(
            "Ratings",
            hint="0-100, where 50 is midfield. Coarse on purpose - what "
                 "matters is relative ordering, not false precision.",
        )
        for name, label in self.rating_fields:
            slider = LabeledSlider(label, 0.0, 100.0, 50.0, 1.0, decimals=0)
            slider.valueChanged.connect(self._on_changed)
            self._sliders[name] = slider
            ratings.body.addWidget(slider)
        self.body.addWidget(ratings)

        self._extra_card = self.build_extra()
        if self._extra_card is not None:
            self.body.addWidget(self._extra_card)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        save = QPushButton("Save")
        save.setObjectName("Primary")
        save.setCursor(Qt.CursorShape.PointingHandCursor)
        save.clicked.connect(self._on_save)
        actions.addWidget(save)

        reset = QPushButton("Reset to shipped values")
        reset.setObjectName("Ghost")
        reset.setCursor(Qt.CursorShape.PointingHandCursor)
        reset.clicked.connect(self._on_reset)
        actions.addWidget(reset)
        actions.addStretch(1)
        self.body.addLayout(actions)

        self.body.addStretch(1)
        self._reload_list()

    # --- hooks for subclasses ----------------------------------------
    def build_extra(self) -> QWidget | None:
        """Extra non-rating controls (e.g. pit loss). Optional."""
        return None

    def load_extra(self, record) -> None:
        return

    def apply_extra(self, record):
        return record

    # ------------------------------------------------------------------
    @property
    def store(self):
        return getattr(self.app, self.store_attr)

    @property
    def selected_key(self) -> str:
        """The selection remembered for the *active* mode."""
        if not self.selection_attr:
            return ""
        return getattr(self.app.mode_settings, self.selection_attr, "") or ""

    def _remember_selection(self, key: str) -> None:
        if not self.selection_attr or not key:
            return
        if getattr(self.app.mode_settings, self.selection_attr, None) == key:
            return
        setattr(self.app.mode_settings, self.selection_attr, key)
        self.app.save_mode_settings()

    def _reload_list(self) -> None:
        """Rebuild from the active mode's database.

        Called on every mode switch, so the list, the selection and the
        values all come from the mode that is now active.
        """
        self._loading = True
        self._combo.clear()
        for record in self.store.all:
            self._combo.addItem(_display_name(record), _key(record))

        # Restore this mode's remembered selection, not the previous one's.
        index = self._combo.findData(self.selected_key)
        if index < 0:
            index = 0
        if self._combo.count():
            self._combo.setCurrentIndex(index)
        self._loading = False
        self._load_selected()

    def _load_selected(self) -> None:
        key = self._combo.currentData()
        record = self.store.get(key) if key else None
        if record is None:
            return

        self._loading = True
        for name, slider in self._sliders.items():
            slider.set_value(float(getattr(record, name, 50.0)))
        self.load_extra(record)
        self._loading = False

        customised = self.store.is_customised(key)
        confidence = getattr(record, "confidence", 0.0)
        self._status.setText(
            ("edited" if customised else "shipped prior")
            + f"   confidence {confidence * 100:.0f}%"
        )

    def _on_select(self) -> None:
        if self._loading:
            return
        self._remember_selection(self._combo.currentData() or "")
        self._load_selected()

    def _on_changed(self, *_args) -> None:
        return  # values are applied on Save, so a slip is not persisted

    def _on_save(self) -> None:
        key = self._combo.currentData()
        record = self.store.get(key) if key else None
        if record is None:
            return
        for name, slider in self._sliders.items():
            setattr(record, name, slider.value())
        record = self.apply_extra(record)
        record.clamped()
        if self.store.save(record):
            self._load_selected()

    def _on_reset(self) -> None:
        key = self._combo.currentData()
        if key and self.store.reset(key):
            self._load_selected()

    def on_shown(self) -> None:
        self._reload_list()

    def refresh(self, report: DiagnosticsReport) -> None:
        return


def _key(record) -> str:
    return getattr(record, "car_id", None) or getattr(record, "track_id", "")


def _display_name(record) -> str:
    name = getattr(record, "name", "")
    team = getattr(record, "team", "") or getattr(record, "country", "")
    return f"{name}  -  {team}" if team else name
