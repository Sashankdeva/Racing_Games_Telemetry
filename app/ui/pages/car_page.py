"""Car database page - editable performance priors per car."""

from __future__ import annotations

from app.domain.car_profiles import RATING_FIELDS
from app.ui.pages.profile_page import ProfileEditorPage


class CarPage(ProfileEditorPage):
    title = "Car"
    subtitle = "Performance priors used by the strategy engine"
    selection_attr = "selected_car"
    store_attr = "cars"
    rating_fields = RATING_FIELDS
    record_noun = "car"
    prior_hint = (
        "These are starting assumptions, not measurements. The strategy "
        "engine uses them until it has seen enough real telemetry, then "
        "measured pace takes precedence. Edit freely."
    )
