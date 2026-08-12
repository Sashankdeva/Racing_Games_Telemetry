"""Profiles page - create, duplicate, rename, delete, import, export.

Built-in profiles cannot be deleted, only reset: that guarantees a known-
good configuration is always one click away no matter what the user has
changed.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.diagnostics.metrics import DiagnosticsReport
from app.ui import theme
from app.ui.pages.base import Page
from app.ui.widgets.common import Card


class ProfilesPage(Page):
    title = "Profiles"
    subtitle = "Saved haptic configurations. Switching applies instantly."

    def build(self) -> None:
        columns = QHBoxLayout()
        columns.setSpacing(16)

        # --- list ---
        list_card = Card("Profiles")
        self._list = QListWidget()
        self._list.setStyleSheet(
            f"""
            QListWidget {{
                background-color: {theme.BG};
                border: 1px solid {theme.BORDER};
                border-radius: 8px;
                padding: 6px;
                font-size: 13px;
            }}
            QListWidget::item {{ padding: 10px 12px; border-radius: 6px; color: {theme.TEXT_DIM}; }}
            QListWidget::item:selected {{ background-color: {theme.SURFACE_HOVER}; color: {theme.TEXT}; }}
            QListWidget::item:hover {{ background-color: {theme.SURFACE_ALT}; }}
            """
        )
        self._list.setMinimumHeight(280)
        self._list.currentItemChanged.connect(self._on_selection_changed)
        self._list.itemDoubleClicked.connect(lambda _: self._on_activate())
        list_card.body.addWidget(self._list)

        activate = QPushButton("Activate Selected")
        activate.setObjectName("Primary")
        activate.setCursor(Qt.CursorShape.PointingHandCursor)
        activate.clicked.connect(self._on_activate)
        list_card.body.addWidget(activate)
        columns.addWidget(list_card, 3)

        # --- details / actions ---
        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(16)

        details = Card("Selected Profile")
        self._name_label = QLabel("-")
        self._name_label.setStyleSheet(
            f"font-size: 16px; font-weight: 700; color: {theme.TEXT};"
        )
        details.body.addWidget(self._name_label)

        self._description_label = QLabel("")
        self._description_label.setObjectName("Hint")
        self._description_label.setWordWrap(True)
        details.body.addWidget(self._description_label)

        self._summary_label = QLabel("")
        self._summary_label.setObjectName("Mono")
        self._summary_label.setWordWrap(True)
        details.body.addWidget(self._summary_label)
        side_layout.addWidget(details)

        actions = Card("Manage")
        for label, handler, object_name in (
            ("New Profile", self._on_new, ""),
            ("Duplicate", self._on_duplicate, ""),
            ("Rename", self._on_rename, ""),
            ("Save Current Changes", self._on_save, "Primary"),
            ("Import...", self._on_import, "Ghost"),
            ("Export...", self._on_export, "Ghost"),
        ):
            button = QPushButton(label)
            if object_name:
                button.setObjectName(object_name)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(handler)
            actions.body.addWidget(button)

        self._delete_button = QPushButton("Delete")
        self._delete_button.setObjectName("Danger")
        self._delete_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._delete_button.clicked.connect(self._on_delete)
        actions.body.addWidget(self._delete_button)
        side_layout.addWidget(actions)

        side_layout.addStretch(1)
        columns.addWidget(side, 2)

        self.body.addLayout(columns, 1)
        self.body.addStretch(1)
        self._reload()

    # ------------------------------------------------------------------
    def _reload(self) -> None:
        active_slug = self.app.profiles.active_slug
        self._list.clear()
        for profile in self.app.profiles.profiles:
            label = profile.name
            if profile.slug == active_slug:
                label += "   - active"
            if profile.builtin:
                label += "   (built-in)"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, profile.slug)
            self._list.addItem(item)
            if profile.slug == active_slug:
                self._list.setCurrentItem(item)

    def _selected_slug(self) -> str | None:
        item = self._list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _on_selection_changed(self, *_args) -> None:
        slug = self._selected_slug()
        profile = self.app.profiles.get(slug) if slug else None
        if profile is None:
            self._name_label.setText("-")
            self._description_label.setText("")
            self._summary_label.setText("")
            return

        self._name_label.setText(profile.name)
        self._description_label.setText(profile.description)

        enabled = sum(1 for s in profile.effects.values() if s.enabled)
        self._summary_label.setText(
            f"Master strength   {profile.master.intensity:.2f}\n"
            f"Feel / Response   {profile.master.feel:.2f} / {profile.master.response:.2f}\n"
            f"Output limit      {profile.master.output_limit:.2f}\n"
            f"Effects enabled   {enabled} of {len(profile.effects)}"
        )
        self._delete_button.setText("Reset to Default" if profile.builtin else "Delete")

    # ------------------------------------------------------------------
    def _on_activate(self) -> None:
        slug = self._selected_slug()
        if slug:
            self.app.set_active_profile(slug)
            self._reload()
            self._notify_profile_change()

    def _on_new(self) -> None:
        name, ok = QInputDialog.getText(self, "New Profile", "Profile name:")
        if not ok or not name.strip():
            return
        if self.app.profiles.create(name.strip()) is None:
            QMessageBox.warning(self, "Could not create", "That name is already in use.")
            return
        self._reload()

    def _on_duplicate(self) -> None:
        slug = self._selected_slug()
        if slug and self.app.profiles.duplicate(slug):
            self._reload()

    def _on_rename(self) -> None:
        slug = self._selected_slug()
        profile = self.app.profiles.get(slug) if slug else None
        if profile is None:
            return
        name, ok = QInputDialog.getText(self, "Rename Profile", "New name:", text=profile.name)
        if not ok or not name.strip():
            return
        if self.app.profiles.rename(slug, name.strip()) is None:
            QMessageBox.warning(self, "Could not rename", "That name is already in use.")
            return
        self._reload()

    def _on_save(self) -> None:
        if self.app.save_active_profile():
            self._reload()
        else:
            QMessageBox.warning(self, "Save failed", "The profile could not be written to disk.")

    def _on_delete(self) -> None:
        slug = self._selected_slug()
        profile = self.app.profiles.get(slug) if slug else None
        if profile is None:
            return

        if profile.builtin:
            question = f"Reset '{profile.name}' to its shipped settings?"
        else:
            question = f"Delete '{profile.name}'? This cannot be undone."

        if QMessageBox.question(self, "Confirm", question) != QMessageBox.StandardButton.Yes:
            return

        self.app.profiles.delete(slug)
        self.app.apply_profile(self.app.profiles.active)
        self._reload()
        self._notify_profile_change()

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Profile", "", "Profile files (*.json);;All files (*)"
        )
        if not path:
            return
        if self.app.profiles.import_file(Path(path)) is None:
            QMessageBox.warning(self, "Import failed", "That file is not a valid profile.")
            return
        self._reload()

    def _on_export(self) -> None:
        slug = self._selected_slug()
        profile = self.app.profiles.get(slug) if slug else None
        if profile is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Profile", f"{profile.slug}.json", "Profile files (*.json)"
        )
        if not path:
            return
        if not self.app.profiles.export(slug, Path(path)):
            QMessageBox.warning(self, "Export failed", "The profile could not be written.")

    def _notify_profile_change(self) -> None:
        window = self.window()
        if hasattr(window, "reload_pages"):
            window.reload_pages()

    def on_shown(self) -> None:
        self._reload()

    def refresh(self, report: DiagnosticsReport) -> None:
        return
