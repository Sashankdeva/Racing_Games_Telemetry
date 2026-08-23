"""Domain databases: cars, tracks and (later) driver learning.

Everything here is *editable prior knowledge*, deliberately separated from
`config` (application settings) and from telemetry (measured fact). Ratings
seed the strategy engine before it has evidence; measured data supersedes
them as it accumulates.
"""

from app.domain.car_profiles import CarProfile, builtin_cars, create_car_store
from app.domain.store import RecordStore
from app.domain.track_profiles import TrackProfile, builtin_tracks, create_track_store

__all__ = [
    "CarProfile",
    "TrackProfile",
    "RecordStore",
    "builtin_cars",
    "builtin_tracks",
    "create_car_store",
    "create_track_store",
]
