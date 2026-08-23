"""Application pages, in navigation order.

The order follows how a driver uses them: what is happening now
(Dashboard), the full data behind it (Telemetry), then the analysis built
on top (Lap Analysis), then configuration and tooling.

Tyres, Strategy, Race and Driver pages are added in later phases and slot
into this tuple after Lap Analysis.
"""

from app.ui.pages.base import Page
from app.ui.pages.car_page import CarPage
from app.ui.pages.coach_page import CoachPage
from app.ui.pages.dashboard import DashboardPage
from app.ui.pages.diagnostics_page import DiagnosticsPage
from app.ui.pages.games_page import GamesPage
from app.ui.pages.history_page import HistoryPage
from app.ui.pages.inspector_page import InspectorPage
from app.ui.pages.lap_page import LapAnalysisPage
from app.ui.pages.race_page import RacePage
from app.ui.pages.settings_page import SettingsPage
from app.ui.pages.setup_page import SetupPage
from app.ui.pages.strategy_page import StrategyPage
from app.ui.pages.suggestions_page import SuggestionsPage
from app.ui.pages.telemetry_page import TelemetryPage
from app.ui.pages.track_page import TrackPage
from app.ui.pages.tyres_page import TyresPage

PAGE_CLASSES: tuple[type[Page], ...] = (
    DashboardPage,
    TelemetryPage,
    LapAnalysisPage,
    TyresPage,
    StrategyPage,
    RacePage,
    CoachPage,
    SuggestionsPage,
    HistoryPage,
    InspectorPage,
    SetupPage,
    CarPage,
    TrackPage,
    GamesPage,
    DiagnosticsPage,
    SettingsPage,
)

__all__ = ["Page", "PAGE_CLASSES"]
