"""Three reported faults: tyre data blank, no battery meter, no suggestions.

The tyre tests matter most. F1 26's car-telemetry entry is 59 bytes against
the 60-byte layout verified up to F1 25, so one byte moved. If it moved
anywhere before the thermal block, every tyre temperature and pressure
decodes as garbage and is rejected at once - which is what "tyre data is not
functioning" looked like.
"""

from __future__ import annotations

import struct

import pytest

from app.core.models import TelemetryFrame, Wheels
from app.domain.driver_session import LapRecord
from app.domain.lap_analysis import analyse_laps
from app.domain.stints import build_stints, current_tyre_state
from app.games.f1 import packets as p
from app.games.f1 import parser

CARS_26, PLAYER = 24, 9
TAIL = struct.pack(
    "<4H4B4BH4f4B",
    *[380] * 4, *[88, 89, 90, 91], *[94, 95, 96, 97], 105,
    *[21.5, 21.6, 22.8, 22.9], *[0] * 4,
)


@pytest.fixture(autouse=True)
def clean_strides():
    parser.reset_stride_cache()
    yield
    parser.reset_stride_cache()


def telemetry_packet(core_bytes: int = 22, stride: int = 59) -> bytes:
    """An F1 26 car-telemetry packet whose core is `core_bytes` long.

    core_bytes=22 is the layout verified through F1 25. 21 models a single
    byte disappearing from the core, which shifts the whole thermal block.
    """
    header = struct.pack(
        "<HBBBBBQfIIBB", 2026, 26, 1, 0, 1, 6, 7, 1.0, 1, 1, PLAYER, 255
    )
    body = b""
    for _ in range(CARS_26):
        if core_bytes == 22:
            core = struct.pack("<HfffBbHBBH", 210, 0.75, 0.1, 0.0, 0, 6, 11500, 0, 50, 0)
        else:  # revLightsBitValue narrowed to a single byte
            core = struct.pack("<HfffBbHBBB", 210, 0.75, 0.1, 0.0, 0, 6, 11500, 0, 50, 0)
        entry = core + TAIL
        body += entry[:stride] + b"\x00" * max(0, stride - len(entry))
    return header + body + b"\x00" * 3


class TestTyreDataDecodes:
    def _parse(self, data):
        return parser.parse_car_telemetry(data, p.parse_header(data))

    def test_unshifted_layout_still_works(self):
        result = self._parse(telemetry_packet(core_bytes=22))
        # Packed RL,RR,FL,FR = 21.5, 21.6, 22.8, 22.9 - so FL is 22.8.
        assert result.tyre_pressure.fl == pytest.approx(22.8, abs=0.01)
        assert result.tyre_pressure.rl == pytest.approx(21.5, abs=0.01)
        assert result.tyre_surface_temp.fl == 90
        assert result.brake_temp.fl == 380

    def test_shifted_thermal_block_is_recovered(self):
        """The reported fault: one byte moves and every tyre reading dies."""
        result = self._parse(telemetry_packet(core_bytes=21))

        assert result is not None
        assert result.speed_kph == 210  # core was never the problem
        assert result.tyre_pressure.fl == pytest.approx(22.8, abs=0.01)
        assert result.tyre_pressure.rl == pytest.approx(21.5, abs=0.01)
        assert result.tyre_surface_temp.fl == 90
        assert result.brake_temp.fl == 380

    def test_corner_mapping_survives_the_shift(self):
        """F1 sends RL,RR,FL,FR - a recovered block must still map right."""
        result = self._parse(telemetry_packet(core_bytes=21))
        # Packed as RL,RR,FL,FR = 88,89,90,91
        assert result.tyre_surface_temp.rl == 88
        assert result.tyre_surface_temp.rr == 89
        assert result.tyre_surface_temp.fl == 90
        assert result.tyre_surface_temp.fr == 91

    def test_unrecoverable_block_stays_empty_not_invented(self):
        """If the block's internals changed, blank is the honest answer."""
        header = struct.pack(
            "<HBBBBBQfIIBB", 2026, 26, 1, 0, 1, 6, 7, 1.0, 1, 1, PLAYER, 255
        )
        body = b""
        for _ in range(CARS_26):
            core = struct.pack("<HfffBbHBBH", 210, 0.75, 0.1, 0.0, 0, 6, 11500, 0, 50, 0)
            reshaped = TAIL[:6] + TAIL[7:]  # a byte removed inside the block
            entry = core + reshaped
            body += entry[:59] + b"\x00" * max(0, 59 - len(entry))
        result = self._parse(header + body + b"\x00" * 3)

        assert result is not None
        assert result.speed_kph == 210
        assert result.tyre_pressure.fl == 0.0  # reported UNAVAILABLE, not guessed

    def test_implausible_values_are_never_accepted(self):
        """Nonsense must not slip through the offset search."""
        assert not parser.plausible_tyre_tail(
            Wheels(380, 380, 380, 380), Wheels(90, 90, 90, 90), Wheels(999, 1, 2, 3)
        )
        assert parser.plausible_tyre_tail(
            Wheels(380, 380, 380, 380), Wheels(90, 90, 90, 90), Wheels(21.5, 21.6, 22.8, 22.9)
        )


class TestBatteryMeter:
    @pytest.fixture
    def meter(self):
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication

        from app.ui.widgets.meters import BatteryMeter

        QApplication.instance() or QApplication([])
        return BatteryMeter()

    def test_normal_mode_uses_the_live_colour(self, meter):
        from app.ui import theme

        meter.set_state(70.0, "Medium")
        assert not meter.overtaking
        assert meter.charge_colour().name().lower() == theme.LIVE.lower()

    def test_overtake_changes_the_colour(self, meter):
        """The requested behaviour."""
        from app.ui import theme

        meter.set_state(70.0, "Overtake")
        assert meter.overtaking
        assert meter.charge_colour().name().lower() == theme.ACCENT.lower()

    def test_overtake_match_is_case_insensitive(self, meter):
        """The label comes from the game, not from us."""
        for mode in ("Overtake", "overtake", " OVERTAKE "):
            meter.set_state(50.0, mode)
            assert meter.overtaking, mode

    def test_low_charge_warns_in_normal_mode(self, meter):
        from app.ui import theme

        meter.set_state(5.0, "Medium")
        assert meter.charge_colour().name().lower() == theme.WARN.lower()

    def test_overtake_colour_wins_over_low_charge(self, meter):
        """Deploying hard is the more important fact."""
        from app.ui import theme

        meter.set_state(5.0, "Overtake")
        assert meter.charge_colour().name().lower() == theme.ACCENT.lower()

    def test_missing_mode_is_never_read_as_overtake(self, meter):
        meter.set_state(70.0, "")
        assert not meter.overtaking

    def test_clear_marks_it_unavailable(self, meter):
        from app.ui import theme

        meter.set_state(70.0, "Overtake")
        meter.clear()
        assert meter.charge_colour().name().lower() == theme.SURFACE_ALT.lower()

    def test_percentage_is_clamped(self, meter):
        meter.set_state(150.0, "Medium")
        meter.set_state(-20.0, "Medium")  # must not raise


def lap(number, time_s, s1=30.0, s2=31.0, s3=None, **kw):
    s3 = round(time_s - s1 - s2, 3) if s3 is None else s3
    return LapRecord(
        lap_number=number, lap_time_s=time_s, sector1_s=s1, sector2_s=s2,
        sector3_s=s3, compound=kw.pop("compound", "Medium"),
        tyre_age_laps=kw.pop("age", number), **kw
    )


class TestDashboardIntegration:
    @pytest.fixture
    def window(self, tmp_path, monkeypatch):
        pytest.importorskip("PySide6")
        monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path))
        from PySide6.QtWidgets import QApplication

        from app.config.settings import AppSettings
        from app.core.application import Application
        from app.ui.main_window import MainWindow

        qt = QApplication.instance() or QApplication([])
        app = Application(AppSettings(game_mode="f1_26"))
        app.mode_settings.auto_start_telemetry = False
        app.persist_on_exit = False
        app.startup()
        window = MainWindow(app)
        yield window, qt, app
        window._timer.stop()
        app.shutdown()

    def _dashboard(self, window):
        return [
            window.stack.widget(i)
            for i in range(window.stack.count())
            if type(window.stack.widget(i)).__name__ == "DashboardPage"
        ][0]

    def test_battery_reflects_overtake(self, window):
        win, qt, app = window
        dashboard = self._dashboard(win)

        app._on_telemetry_frame(
            TelemetryFrame(valid=True, game="f1", ers_store_percent=64.0,
                           ers_mode="Overtake")
        )
        dashboard.refresh(app.report())
        qt.processEvents()

        assert dashboard._battery.overtaking
        assert "OVERTAKE" in dashboard._battery_note.text()

    def test_battery_normal_mode_has_no_note(self, window):
        win, qt, app = window
        dashboard = self._dashboard(win)

        app._on_telemetry_frame(
            TelemetryFrame(valid=True, game="f1", ers_store_percent=64.0,
                           ers_mode="Medium")
        )
        dashboard.refresh(app.report())
        qt.processEvents()

        assert not dashboard._battery.overtaking
        assert dashboard._battery_note.text() == ""

    def test_suggestions_panel_populates(self, window):
        win, qt, app = window
        dashboard = self._dashboard(win)

        app._on_telemetry_frame(
            TelemetryFrame(
                valid=True, game="f1", ers_mode="Medium", ers_store_percent=6.0,
                current_lap=5, total_laps=30, position=4,
            )
        )
        dashboard._last_suggestion_eval = 0.0
        dashboard.refresh(app.report())
        qt.processEvents()

        shown = [lbl for lbl in dashboard._suggestion_labels if not lbl.isHidden()]
        assert shown, "no suggestion rendered"
        # The dashboard shows one suggestion, with its reason.
        assert "Why:" in shown[0].text()

    def test_panel_is_empty_before_anything_happens(self, window):
        win, qt, app = window
        dashboard = self._dashboard(win)
        dashboard.refresh(app.report())
        qt.processEvents()

        assert all(lbl.isHidden() for lbl in dashboard._suggestion_labels)
        assert not dashboard._suggestion_empty.isHidden()

    def test_stint_tyre_state_reaches_the_engine(self, window):
        """The engine must read the stint model, not recompute it."""
        win, qt, app = window
        laps = [lap(i + 1, 92.0 + 0.09 * i, age=i + 1) for i in range(12)]
        stints = build_stints(laps)
        state = current_tyre_state(stints)
        assert state.degradation_confidence.is_usable

        app.stints = stints
        app.tyres = state
        app.lap_analysis = analyse_laps(laps)
        # Telemetry must be live: the engine deliberately raises nothing new
        # about a car that is not running.
        app._on_telemetry_frame(
            TelemetryFrame(valid=True, game="f1", current_lap=13, total_laps=30)
        )
        out = app.suggestions.evaluate(app.suggestion_context(app.report()))

        assert any(s.category.value == "TYRE" for s in out)
