"""Profiles and settings: defaults, persistence, CRUD, tolerant loading."""

from __future__ import annotations

import json

import pytest

from app.config.settings import AppSettings
from app.core.events import EventBus
from app.haptics.effects import EFFECT_CLASSES
from app.profiles.manager import ProfileManager
from app.profiles.schema import Profile, builtin_profiles, slugify


@pytest.fixture
def manager(tmp_path):
    return ProfileManager(EventBus(), directory=tmp_path / "profiles")


class TestBuiltins:
    def test_expected_profiles_ship(self, manager):
        names = manager.names
        for expected in ("Default", "F1 Realistic", "F1 Strong", "F1 Subtle", "Custom"):
            assert expected in names

    def test_every_builtin_covers_every_effect(self):
        """A profile missing an effect would leave it unconfigured."""
        for profile in builtin_profiles():
            for effect_class in EFFECT_CLASSES:
                assert effect_class.id in profile.effects

    def test_default_profile_is_usable_out_of_the_box(self, manager):
        """The stock profile must already feel right - the brief is that
        users should not have to tune anything to get a good experience."""
        default = manager.by_name("Default")
        assert default.master.intensity == pytest.approx(1.0)
        assert default.master.global_smoothing == 0.0  # per-effect is preferred
        enabled = [s for s in default.effects.values() if s.enabled]
        assert len(enabled) == len(EFFECT_CLASSES)

    def test_strong_is_stronger_than_subtle(self, manager):
        strong = manager.by_name("F1 Strong")
        subtle = manager.by_name("F1 Subtle")
        assert strong.master.intensity > subtle.master.intensity


class TestPersistence:
    def test_save_then_reload_round_trips(self, tmp_path):
        directory = tmp_path / "profiles"
        manager = ProfileManager(EventBus(), directory=directory)

        profile = manager.active
        profile.master.intensity = 0.42
        profile.effect("kerb").sharpness = 0.11
        assert manager.save(profile)

        reloaded = ProfileManager(EventBus(), directory=directory)
        restored = reloaded.get(profile.slug)
        assert restored.master.intensity == pytest.approx(0.42)
        assert restored.effect("kerb").sharpness == pytest.approx(0.11)

    def test_writes_are_atomic_leaving_no_temp_file(self, manager, tmp_path):
        manager.save(manager.active)
        assert list((tmp_path / "profiles").glob("*.tmp")) == []

    def test_corrupt_file_is_skipped_not_fatal(self, tmp_path):
        directory = tmp_path / "profiles"
        directory.mkdir(parents=True)
        (directory / "broken.json").write_text("{ not json at all", encoding="utf-8")

        manager = ProfileManager(EventBus(), directory=directory)
        assert len(manager.profiles) >= 5  # builtins still loaded

    def test_unknown_keys_are_ignored(self):
        profile = Profile.from_dict({
            "name": "Future",
            "unknown_field": 123,
            "master": {"intensity": 0.5, "invented": 9},
            "effects": {"kerb": {"intensity": 0.7, "mystery": True}},
        })
        assert profile.name == "Future"
        assert profile.master.intensity == pytest.approx(0.5)
        assert profile.effect("kerb").intensity == pytest.approx(0.7)

    def test_missing_effects_are_filled_with_defaults(self):
        """A profile written before an effect existed must still work."""
        profile = Profile.from_dict({"name": "Old", "effects": {}})
        for effect_class in EFFECT_CLASSES:
            assert effect_class.id in profile.effects

    def test_out_of_range_values_are_clamped(self):
        profile = Profile.from_dict({
            "name": "Wild",
            "master": {"intensity": 99.0, "output_limit": -5.0},
            "effects": {"kerb": {"intensity": 50.0, "balance": -9.0}},
        })
        assert profile.master.intensity <= 1.5
        assert profile.master.output_limit >= 0.1
        assert profile.effect("kerb").intensity <= 2.0
        assert profile.effect("kerb").balance >= -1.0

    def test_garbage_types_do_not_raise(self):
        profile = Profile.from_dict({
            "name": "Odd",
            "master": {"intensity": "loud"},
            "effects": {"kerb": {"intensity": None, "enabled": "yes"}},
        })
        assert isinstance(profile.master.intensity, float)


class TestCrud:
    def test_create_and_delete(self, manager):
        created = manager.create("My Setup")
        assert created is not None
        assert manager.exists("My Setup")

        assert manager.delete(created.slug)
        assert not manager.exists("My Setup")

    def test_duplicate_names_are_rejected(self, manager):
        manager.create("Twice")
        assert manager.create("Twice") is None

    def test_duplicate_generates_a_unique_name(self, manager):
        copy = manager.duplicate(manager.active_slug)
        assert copy is not None
        assert copy.slug != manager.active_slug

    def test_rename(self, manager):
        created = manager.create("Before")
        renamed = manager.rename(created.slug, "After")
        assert renamed is not None
        assert manager.exists("After")

    def test_builtin_delete_resets_instead_of_removing(self, manager):
        default = manager.by_name("Default")
        default.master.intensity = 0.1
        manager.save(default)

        manager.delete(default.slug)

        restored = manager.by_name("Default")
        assert restored is not None  # still present
        assert restored.master.intensity == pytest.approx(1.0)  # back to shipped value

    def test_set_active(self, manager):
        target = manager.by_name("F1 Strong")
        assert manager.set_active(target.slug).name == "F1 Strong"
        assert manager.active.name == "F1 Strong"


class TestImportExport:
    def test_round_trip(self, manager, tmp_path):
        source = manager.by_name("F1 Strong")
        destination = tmp_path / "exported.json"
        assert manager.export(source.slug, destination)

        imported = manager.import_file(destination)
        assert imported is not None
        assert imported.master.intensity == pytest.approx(source.master.intensity)

    def test_importing_a_duplicate_name_renames_it(self, manager, tmp_path):
        destination = tmp_path / "default.json"
        manager.export(manager.by_name("Default").slug, destination)

        imported = manager.import_file(destination)
        assert imported is not None
        assert imported.name != "Default"

    def test_importing_rubbish_returns_none(self, manager, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        assert manager.import_file(bad) is None

    def test_importing_a_missing_file_returns_none(self, manager, tmp_path):
        assert manager.import_file(tmp_path / "nope.json") is None


class TestSlugify:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Default", "default"),
            ("F1 Realistic", "f1-realistic"),
            ("  Spaces  ", "spaces"),
            ("!!!", "profile"),
            ("Mixed_Case 123", "mixed-case-123"),
        ],
    )
    def test_slugs(self, name, expected):
        assert slugify(name) == expected


class TestAppSettings:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "settings.json"
        settings = AppSettings(udp_port=20800, update_rate_hz=90.0, verbose_logging=True)
        assert settings.save(path)

        loaded = AppSettings.load(path)
        assert loaded.udp_port == 20800
        assert loaded.update_rate_hz == pytest.approx(90.0)
        assert loaded.verbose_logging is True

    def test_missing_file_gives_defaults(self, tmp_path):
        settings = AppSettings.load(tmp_path / "absent.json")
        assert settings.udp_port == 20777

    def test_corrupt_file_gives_defaults(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text("{{{", encoding="utf-8")
        assert AppSettings.load(path).udp_port == 20777

    def test_values_are_clamped_on_load(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"controller_index": 99, "update_rate_hz": 5000, "udp_port": 10}),
            encoding="utf-8",
        )
        settings = AppSettings.load(path)
        assert 0 <= settings.controller_index <= 3
        assert 30 <= settings.update_rate_hz <= 250
        assert settings.udp_port >= 1024

    def test_unknown_keys_are_ignored(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"udp_port": 20801, "from_the_future": True}), encoding="utf-8")
        assert AppSettings.load(path).udp_port == 20801
