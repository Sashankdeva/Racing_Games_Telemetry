"""Application pages, in navigation order."""

from app.ui.pages.base import Page
from app.ui.pages.controller_page import ControllerPage
from app.ui.pages.dashboard import DashboardPage
from app.ui.pages.diagnostics_page import DiagnosticsPage
from app.ui.pages.effects_page import EffectsPage
from app.ui.pages.games_page import GamesPage
from app.ui.pages.haptics_page import HapticsPage
from app.ui.pages.profiles_page import ProfilesPage
from app.ui.pages.settings_page import SettingsPage

PAGE_CLASSES: tuple[type[Page], ...] = (
    DashboardPage,
    HapticsPage,
    EffectsPage,
    GamesPage,
    ProfilesPage,
    ControllerPage,
    DiagnosticsPage,
    SettingsPage,
)

__all__ = ["Page", "PAGE_CLASSES"]
