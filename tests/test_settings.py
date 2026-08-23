"""Global application settings.

Transport and per-mode preferences are covered by test_game_modes.py -
this file only covers what is genuinely global.
"""

from __future__ import annotations

import json

from app.config.settings import AppSettings
from app.games.modes import GameMode


class TestAppSettings:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "settings.json"
        settings = AppSettings(
            game_mode="f1_26", verbose_logging=True, minimize_to_tray=False
        )
        assert settings.save(path)

        loaded = AppSettings.load(path)
        assert loaded.game_mode == "f1_26"
        assert loaded.verbose_logging is True
        assert loaded.minimize_to_tray is False

    def test_missing_file_gives_defaults(self, tmp_path):
        settings = AppSettings.load(tmp_path / "absent.json")
        assert settings.mode is GameMode.F1_25

    def test_corrupt_file_gives_defaults(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text("{{{", encoding="utf-8")
        assert AppSettings.load(path).mode is GameMode.F1_25

    def test_unknown_game_mode_falls_back(self, tmp_path):
        """A hand-edited or future mode must not stop the app starting."""
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"game_mode": "f1_99"}), encoding="utf-8")
        assert AppSettings.load(path).mode is GameMode.F1_25

    def test_unknown_keys_are_ignored(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"game_mode": "f1_26", "from_the_future": True}),
            encoding="utf-8",
        )
        assert AppSettings.load(path).game_mode == "f1_26"

    def test_mode_property_parses(self):
        assert AppSettings(game_mode="f1_26").mode is GameMode.F1_26

    def test_transport_settings_are_not_global(self):
        """They belong to the game mode; keeping them here would let one
        mode overwrite the other."""
        assert not hasattr(AppSettings(), "udp_port")
        assert not hasattr(AppSettings(), "telemetry_timeout")
