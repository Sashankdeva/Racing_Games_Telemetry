"""Smart Suggestions - the full list, with reasons and source data.

The Dashboard shows only the single most relevant suggestion so it stays
readable at speed. This page is where the rest lives: everything currently
active, everything recently resolved, and the numbers each decision came
from.

No logic here. The page renders whatever the engine produced; every
threshold, cooldown and confidence decision belongs to
`app.domain.smart_suggestions`.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.diagnostics.metrics import DiagnosticsReport
from app.domain.smart_suggestions import Category, Severity, Suggestion
from app.ui import theme
from app.ui.pages.base import Page
from app.ui.widgets.common import Card

SEVERITY_COLOURS = {
    Severity.INFO: theme.LIVE,
    Severity.ADVISORY: theme.WARN,
    Severity.WARNING: theme.DANGER,
    Severity.CRITICAL: theme.DANGER,
}

#: "All" plus one filter per category the engine can produce.
FILTERS: tuple[str, ...] = ("ALL", *(c.value for c in Category))


class _SuggestionCard(QWidget):
    """One suggestion: what, why, how sure, and the numbers behind it."""

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(4)

        self._heading = QLabel()
        self._heading.setStyleSheet("font-size: 11px; font-weight: 700;")
        layout.addWidget(self._heading)

        self._message = QLabel()
        self._message.setWordWrap(True)
        self._message.setStyleSheet(
            f"font-size: 14px; font-weight: 600; color: {theme.TEXT};"
        )
        layout.addWidget(self._message)

        self._reason = QLabel()
        self._reason.setObjectName("Hint")
        self._reason.setWordWrap(True)
        layout.addWidget(self._reason)

        self._source = QLabel()
        self._source.setObjectName("Mono")
        self._source.setWordWrap(True)
        self._source.setStyleSheet(f"font-size: 10px; color: {theme.TEXT_FAINT};")
        layout.addWidget(self._source)

    def show_suggestion(self, s: Suggestion, *, resolved: bool = False) -> None:
        colour = theme.TEXT_FAINT if resolved else SEVERITY_COLOURS[s.severity]
        state = s.state.value if resolved else s.severity.label
        self._heading.setText(f"{state}   {s.category.value}   {s.priority.label}")
        self._heading.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {colour}; letter-spacing:0.6px;"
        )
        self._message.setText(s.message)
        self._message.setStyleSheet(
            f"font-size: 14px; font-weight: 600; "
            f"color: {theme.TEXT_FAINT if resolved else theme.TEXT};"
        )
        self._reason.setText(f"Why: {s.reason}")
        # The source data is what makes a recommendation auditable.
        self._source.setText(
            f"Confidence: {s.confidence.value}    "
            + "   ".join(f"{k}={v}" for k, v in s.source_data.items())
        )
        self.setStyleSheet(
            f"background: {theme.SURFACE_ALT}; border-radius: 8px;"
        )


class SuggestionsPage(Page):
    title = "Suggestions"
    subtitle = "Race-engineer suggestions, with the reasoning behind each one"

    #: Cards are created once and reused; rebuilding widgets on a 20 Hz
    #: timer is how a UI starts stuttering mid-race.
    MAX_ACTIVE = 8
    MAX_RECENT = 8

    def build(self) -> None:
        self._filter = "ALL"

        self.body.addWidget(self._build_filters())

        columns = QHBoxLayout()
        columns.setSpacing(16)
        columns.addWidget(self._build_active(), 1)
        columns.addWidget(self._build_recent(), 1)
        self.body.addLayout(columns, 1)

    # ------------------------------------------------------------------
    def _build_filters(self) -> QWidget:
        card = Card("Filter")
        row = QHBoxLayout()
        row.setSpacing(6)

        self._buttons = QButtonGroup(self)
        self._buttons.setExclusive(True)
        for name in FILTERS:
            button = QPushButton(name.title())
            button.setCheckable(True)
            button.setChecked(name == "ALL")
            button.setMinimumHeight(30)
            button.clicked.connect(
                lambda _checked=False, value=name: self._set_filter(value)
            )
            self._buttons.addButton(button)
            row.addWidget(button)
        row.addStretch(1)
        card.body.addLayout(row)
        return card

    def _set_filter(self, value: str) -> None:
        self._filter = value
        self.refresh(self.app.report())

    def _scrolling_column(self, title: str, hint: str, count: int):
        card = Card(title, hint=hint)
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        cards = []
        for _ in range(count):
            entry = _SuggestionCard()
            entry.hide()
            layout.addWidget(entry)
            cards.append(entry)

        empty = QLabel("Nothing to report.")
        empty.setObjectName("Hint")
        layout.addWidget(empty)
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        card.body.addWidget(scroll)
        return card, cards, empty

    def _build_active(self) -> QWidget:
        card, self._active_cards, self._active_empty = self._scrolling_column(
            "Active",
            "Conditions happening right now. They disappear on their own "
            "when the condition does.",
            self.MAX_ACTIVE,
        )
        return card

    def _build_recent(self) -> QWidget:
        card, self._recent_cards, self._recent_empty = self._scrolling_column(
            "Recent",
            "Resolved or expired. Kept so a race can be reviewed afterwards.",
            self.MAX_RECENT,
        )
        return card

    # ------------------------------------------------------------------
    def _matches(self, suggestion: Suggestion) -> bool:
        return self._filter == "ALL" or suggestion.category.value == self._filter

    def _fill(self, cards, empty, items, *, resolved: bool) -> None:
        shown = [s for s in items if self._matches(s)][: len(cards)]
        for index, card in enumerate(cards):
            if index >= len(shown):
                card.hide()
                continue
            card.show_suggestion(shown[index], resolved=resolved)
            card.show()
        empty.setVisible(not shown)

    def refresh(self, report: DiagnosticsReport) -> None:
        # Evaluation happens here rather than in a rule, so the page shows
        # the same engine state the dashboard does.
        engine = self.app.suggestions
        engine.evaluate(self.app.suggestion_context(report))

        self._fill(self._active_cards, self._active_empty, engine.active, resolved=False)
        self._fill(
            self._recent_cards, self._recent_empty, engine.history, resolved=True
        )
