"""Session History & Performance Progression.

The rule this module exists to enforce is that a completed lap is a fact,
and a missing UDP packet cannot unmake it. So the sharpest tests here are
the ones that stop telemetry, crash the process, and switch modes, then
check that nothing recorded has changed.

Everything else is comparison arithmetic and refusing to compare things
that are not comparable.
"""

from __future__ import annotations

import json

import pytest

from app.core.models import TelemetryFrame
from app.domain.driver_session import LapRecord
from app.domain.lap_analysis import Confidence
from app.domain.session_history import (
    MIN_SESSIONS_FOR_TREND,
    STALE_CLOSE_S,
    HistoryAnalysis,
    HistoryError,
    SessionCollector,
    SessionRecord,
    SessionState,
    SessionStore,
    SessionType,
    StoredLap,
    StoredStint,
    Trend,
    sessions_dir,
)
from app.games.modes import GameMode


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path))
    yield tmp_path


def lap(number, time_s, s1=30.0, s2=31.0, s3=None, **kw) -> LapRecord:
    s3 = round(time_s - s1 - s2, 3) if s3 is None else s3
    return LapRecord(
        lap_number=number, lap_time_s=time_s, sector1_s=s1, sector2_s=s2,
        sector3_s=s3, compound=kw.pop("compound", "Medium"),
        tyre_age_laps=kw.pop("age", number), **kw
    )


def frame(**kw) -> TelemetryFrame:
    defaults = dict(
        valid=True, game="f1", current_lap=1, session_type="Race",
        weather="Clear", position=5, track_temperature=39.0,
    )
    defaults.update(kw)
    return TelemetryFrame(**defaults)


def session(
    mode=GameMode.F1_25, car="ferrari", track="monza", times=None,
    started_at=1000.0, session_id="s1",
) -> SessionRecord:
    times = times or [92.4, 92.0, 92.2]
    record = SessionRecord(
        session_id=session_id, game_mode=mode.value, started_at=started_at,
        car_id=car, track_id=track, session_type=SessionType.RACE.value,
    )
    for index, time_s in enumerate(times):
        record.laps.append(
            StoredLap.from_record(lap(index + 1, time_s))
        )
    return record


def collect(times, *, mode=GameMode.F1_25, car="ferrari", track="monza"):
    collector = SessionCollector(mode)
    for index, time_s in enumerate(times):
        collector.observe_frame(frame(current_lap=index + 1), car_id=car, track_id=track)
        collector.observe_lap(lap(index + 1, time_s))
    return collector


# ---------------------------------------------------------------------------
class TestSessionCreation:
    def test_1_a_session_starts_on_the_first_valid_frame(self):
        collector = SessionCollector(GameMode.F1_26)
        assert collector.record is None

        collector.observe_frame(frame(), car_id="ferrari", track_id="monza")
        record = collector.record

        assert record is not None
        assert record.game_mode == "f1_26"
        assert record.car_id == "ferrari"
        assert record.state == SessionState.LIVE.value

    def test_an_invalid_frame_starts_nothing(self):
        collector = SessionCollector(GameMode.F1_25)
        collector.observe_frame(TelemetryFrame(valid=False))
        assert collector.record is None

    @pytest.mark.parametrize(
        "reported,expected",
        [
            ("Race", SessionType.RACE),
            ("Qualifying 2", SessionType.QUALIFYING),
            ("Practice 1", SessionType.PRACTICE),
            ("Sprint Shootout 1", SessionType.SPRINT),
            ("Time Trial", SessionType.TIME_TRIAL),
            ("", SessionType.UNKNOWN),
            ("Something else", SessionType.UNKNOWN),
        ],
    )
    def test_session_type_is_never_guessed(self, reported, expected):
        assert SessionType.parse(reported) is expected

    def test_a_blank_field_never_overwrites_a_known_one(self):
        """The core rule, at field level."""
        collector = SessionCollector(GameMode.F1_25)
        collector.observe_frame(frame(weather="Light rain"))
        collector.observe_frame(frame(weather=""))

        assert collector.record.weather == "Light rain"


class TestLapPersistence:
    def test_2_and_3_laps_and_sectors_are_stored(self):
        collector = collect([92.4, 92.0])
        record = collector.record

        assert record.laps_completed == 2
        assert record.laps[0].sector1_s == 30.0
        assert record.laps[1].lap_time_s == 92.0

    def test_history_is_append_only(self):
        collector = collect([92.4])
        collector.observe_lap(lap(1, 99.9))  # same lap number again
        assert collector.record.laps_completed == 1
        assert collector.record.laps[0].lap_time_s == 92.4

    def test_a_lap_is_written_to_disk_immediately(self):
        """Crash safety: the session must not live only in memory.

        The collector is deliberately discarded without calling finish() -
        the laps must already be on disk.
        """
        collect([92.4, 92.0])
        stored = SessionStore(GameMode.F1_25).load_all()

        assert stored
        assert stored[0].laps_completed == 2

    def test_invalid_laps_are_kept_but_not_counted_as_pace(self):
        collector = SessionCollector(GameMode.F1_25)
        collector.observe_frame(frame())
        collector.observe_lap(lap(1, 92.0))
        collector.observe_lap(lap(2, 88.0, invalid=True))

        record = collector.record
        assert record.laps_completed == 2
        assert len(record.valid_laps) == 1
        assert record.invalid_laps == 1
        assert record.best_lap_s == 92.0


class TestStaleAndFinished:
    def test_4_going_stale_changes_state_only(self):
        """LIVE -> STALE -> LIVE must not erase anything."""
        collector = collect([92.4, 92.0, 91.8])
        before = [lap.lap_time_s for lap in collector.record.laps]

        collector.tick(live=False, now=100.0)
        assert collector.state is SessionState.STALE
        assert [lap.lap_time_s for lap in collector.record.laps] == before

        collector.tick(live=True, now=101.0)
        assert collector.state is SessionState.LIVE
        assert [lap.lap_time_s for lap in collector.record.laps] == before

    def test_stale_is_never_treated_as_empty(self):
        collector = collect([92.4, 92.0, 91.8])
        collector.tick(live=False, now=50.0)

        record = collector.record
        assert record.best_lap_s == 91.8
        assert record.laps_completed == 3

    def test_6_reconnection_continues_the_same_session(self):
        collector = collect([92.4, 92.0])
        session_id = collector.record.session_id

        collector.tick(live=False, now=10.0)
        collector.observe_frame(frame(current_lap=3))
        collector.observe_lap(lap(3, 91.8))

        assert collector.record.session_id == session_id
        assert collector.record.laps_completed == 3

    def test_a_prolonged_silence_closes_and_saves_the_session(self):
        collector = collect([92.4, 92.0])
        collector.tick(live=True, now=0.0)
        collector.tick(live=False, now=STALE_CLOSE_S + 1.0)

        assert collector.state is SessionState.FINISHED
        stored = SessionStore(GameMode.F1_25).load_all()
        assert stored[0].state == SessionState.FINISHED.value

    def test_5_finish_marks_and_saves(self):
        collector = collect([92.4, 92.0])
        finished = collector.finish()

        assert finished.state == SessionState.FINISHED.value
        assert finished.ended_at > 0
        assert collector.record is None

    def test_finish_is_safe_to_call_twice(self):
        collector = collect([92.4])
        collector.finish()
        assert collector.finish() is None

    def test_an_empty_session_is_not_stored(self):
        collector = SessionCollector(GameMode.F1_25)
        collector.observe_frame(frame())
        collector.finish()
        assert SessionStore(GameMode.F1_25).load_all() == []

    def test_21_an_unexpected_loss_still_leaves_the_laps_on_disk(self):
        """Nothing calls finish(): the laps must already be saved."""
        collect([92.4, 92.0, 91.8])
        stored = SessionStore(GameMode.F1_25).load_all()

        assert stored
        assert stored[0].laps_completed == 3

    def test_20_a_restart_finds_the_previous_session(self):
        collect([92.4, 92.0])
        # A brand-new collector, as after an application restart.
        reopened = SessionCollector(GameMode.F1_25)
        assert reopened.history().sessions


class TestBests:
    def test_7_personal_best_across_sessions(self):
        history = HistoryAnalysis([
            session(times=[92.4, 92.0], session_id="a"),
            session(times=[91.6, 91.9], session_id="b"),
        ])
        assert history.personal_best() == 91.6

    def test_8_theoretical_best_is_distinct_from_an_actual_lap(self):
        record = SessionRecord(
            session_id="x", game_mode="f1_25", started_at=0.0,
            car_id="ferrari", track_id="monza",
        )
        record.laps = [
            StoredLap.from_record(lap(1, 92.0, s1=30.0, s2=31.5, s3=30.5)),
            StoredLap.from_record(lap(2, 92.2, s1=30.4, s2=31.0, s3=30.8)),
        ]
        # Best sectors: 30.0 + 31.0 + 30.5 = 91.5, quicker than any lap.
        assert record.theoretical_best_s == pytest.approx(91.5)
        assert record.best_lap_s == 92.0
        assert record.theoretical_best_s < record.best_lap_s

    def test_theoretical_best_needs_all_three_sectors(self):
        record = SessionRecord(
            session_id="x", game_mode="f1_25", started_at=0.0,
        )
        record.laps = [StoredLap.from_record(lap(1, 92.0, s1=30.0, s2=31.0, s3=0.0))]
        assert record.theoretical_best_s is None

    def test_best_sectors_across_sessions(self):
        first = session(session_id="a")
        first.laps = [StoredLap.from_record(lap(1, 92.0, s1=29.8, s2=31.5, s3=30.7))]
        second = session(session_id="b")
        second.laps = [StoredLap.from_record(lap(1, 92.0, s1=30.5, s2=30.9, s3=30.6))]

        history = HistoryAnalysis([second, first])
        assert history.personal_best_sector(1) == pytest.approx(29.8)
        assert history.personal_best_sector(2) == pytest.approx(30.9)
        assert history.theoretical_best() == pytest.approx(29.8 + 30.9 + 30.6)


class TestComparison:
    def test_9_session_comparison_with_sector_breakdown(self):
        previous = session(session_id="old", started_at=1000.0)
        previous.laps = [StoredLap.from_record(lap(1, 91.882, s1=30.00, s2=31.20, s3=30.68))]
        current = session(session_id="new", started_at=2000.0)
        current.laps = [StoredLap.from_record(lap(1, 91.441, s1=29.92, s2=30.91, s3=30.61))]

        comparison = HistoryAnalysis([current, previous]).compare(current)

        assert comparison.available
        assert comparison.improvement_s == pytest.approx(-0.441, abs=0.001)
        deltas = {s.sector: s.delta_s for s in comparison.sectors}
        assert deltas[1] == pytest.approx(-0.08, abs=0.001)
        assert deltas[2] == pytest.approx(-0.29, abs=0.001)
        assert comparison.largest_gain.sector == 2

    def test_incompatible_sessions_are_refused(self):
        """F1 25 Ferrari data must never be compared with F1 26 Ferrari."""
        f25 = session(mode=GameMode.F1_25, session_id="a")
        f26 = session(mode=GameMode.F1_26, session_id="b")
        assert not f25.compatible_with(f26)

        comparison = HistoryAnalysis([f26, f25]).compare(f26)
        assert not comparison.available
        assert "no earlier session" in comparison.reason

    def test_a_different_track_is_not_comparable(self):
        monza = session(track="monza", session_id="a")
        spa = session(track="spa", session_id="b")
        assert not monza.compatible_with(spa)

    def test_a_different_car_is_not_comparable(self):
        assert not session(car="ferrari").compatible_with(
            session(car="mclaren", session_id="b")
        )

    def test_no_comparison_without_an_earlier_session(self):
        only = session(session_id="only")
        comparison = HistoryAnalysis([only]).compare(only)
        assert not comparison.available


class TestProgression:
    def _sessions(self, averages):
        out = []
        for index, average in enumerate(averages):
            record = session(
                session_id=f"s{index}", started_at=1000.0 + index * 100
            )
            record.laps = [
                StoredLap.from_record(lap(1, average - 0.1, s1=30.0, s2=31.0)),
                StoredLap.from_record(lap(2, average + 0.1, s1=30.0, s2=31.0)),
            ]
            out.append(record)
        return list(reversed(out))  # newest first

    def test_10_pace_improving_across_sessions(self):
        history = HistoryAnalysis(
            self._sessions([93.0, 92.8, 92.5, 92.0, 91.8, 91.6])
        )
        progression = history.progression()

        assert progression.pace is Trend.IMPROVING
        assert progression.pace_delta_s > 0

    def test_pace_declining_is_reported_honestly(self):
        history = HistoryAnalysis(
            self._sessions([91.6, 91.8, 92.0, 92.5, 92.8, 93.0])
        )
        assert history.progression().pace is Trend.DECLINING

    def test_stable_pace_is_not_called_progress(self):
        history = HistoryAnalysis(self._sessions([92.0] * 6))
        assert history.progression().pace is Trend.STABLE

    def test_insufficient_sessions_report_so(self):
        """One good day is not progress."""
        history = HistoryAnalysis(self._sessions([92.0, 91.5]))
        progression = history.progression()

        assert progression.pace is Trend.INSUFFICIENT_DATA
        assert progression.sessions < MIN_SESSIONS_FOR_TREND

    def test_consistency_trend(self):
        records = []
        for index, spread in enumerate([1.0, 0.9, 0.8, 0.2, 0.15, 0.1]):
            record = session(session_id=f"c{index}", started_at=1000.0 + index)
            record.laps = [
                StoredLap.from_record(lap(1, 92.0 - spread)),
                StoredLap.from_record(lap(2, 92.0 + spread)),
            ]
            records.append(record)
        progression = HistoryAnalysis(list(reversed(records))).progression()
        assert progression.consistency is Trend.IMPROVING


class TestTyreAndStrategyHistory:
    def _with_stints(self, degradations, session_id="s"):
        record = session(session_id=session_id)
        record.stints = [
            StoredStint(
                number=index + 1, compound="Medium", laps=12, clean_laps=10,
                degradation_s_per_lap=value,
                confidence=Confidence.HIGH.name,
            )
            for index, value in enumerate(degradations)
        ]
        for index in range(3):
            record.laps.append(
                StoredLap.from_record(lap(index + 1, 92.0 + index * 0.1))
            )
        return record

    def test_11_tyre_history_per_compound(self):
        history = HistoryAnalysis([self._with_stints([0.06, 0.07])])
        compounds = history.tyre_history()

        assert len(compounds) == 1
        medium = compounds[0]
        assert medium.compound == "Medium"
        assert medium.stints == 2
        assert medium.average_degradation == pytest.approx(0.065)

    def test_an_unmeasurable_stint_contributes_no_number(self):
        record = session()
        record.stints = [
            StoredStint(number=1, compound="Hard", laps=4, degradation_s_per_lap=None)
        ]
        compounds = HistoryAnalysis([record]).tyre_history()
        assert compounds[0].average_degradation is None
        assert compounds[0].confidence is Confidence.NO_DATA

    def test_12_strategy_history_records_both_sides(self):
        """What was recommended and what happened - no verdict."""
        from app.domain.session_history import StoredStrategyChange

        record = session()
        record.recommended_strategy = "Pit L22 -> Hard"
        record.strategy_changes = [
            StoredStrategyChange(lap=20, previous="Stay out",
                                 current="Pit L22 -> Hard", reason="degradation"),
        ]
        restored = SessionRecord.from_json(record.to_json())

        assert restored.recommended_strategy == "Pit L22 -> Hard"
        assert restored.strategy_changes[0].lap == 20
        # No field claims the recommendation was right or wrong.
        assert not hasattr(restored.strategy_changes[0], "correct")

    def test_13_driver_coach_observations_are_stored(self):
        from app.domain.session_history import StoredObservation

        record = session()
        record.observations = [
            StoredObservation(
                id="pace.s2", category="PACE", sector=2, first_detected_lap=8,
                occurrences=6, peak_loss_s=0.31, current_loss_s=0.09,
                status="IMPROVING",
            )
        ]
        restored = SessionRecord.from_json(record.to_json())

        stored = restored.observations[0]
        assert stored.first_detected_lap == 8
        assert stored.occurrences == 6
        assert stored.status == "IMPROVING"


class TestSearch:
    def _history(self):
        return HistoryAnalysis([
            session(car="ferrari", track="monza", session_id="a", started_at=3000.0),
            session(car="ferrari", track="spa", session_id="b", started_at=2000.0),
            session(car="mclaren", track="monza", session_id="c", started_at=1000.0),
        ])

    def test_search_by_track(self):
        found = self._history().compatible(track_id="monza")
        assert {s.session_id for s in found} == {"a", "c"}

    def test_search_by_car_and_track(self):
        found = self._history().compatible(car_id="ferrari", track_id="monza")
        assert [s.session_id for s in found] == ["a"]

    def test_search_by_date(self):
        found = self._history().compatible(since=2500.0)
        assert [s.session_id for s in found] == ["a"]

    def test_search_by_session_type(self):
        found = self._history().compatible(session_type=SessionType.RACE.value)
        assert len(found) == 3


class TestImportExport:
    def test_17_export_round_trip(self):
        record = session(mode=GameMode.F1_26, session_id="x")
        restored = SessionRecord.from_json(record.to_json())

        assert restored.session_id == "x"
        assert restored.game_mode == "f1_26"
        assert restored.laps_completed == record.laps_completed

    def test_exported_shape_matches_the_documented_format(self):
        data = json.loads(session(mode=GameMode.F1_26).to_json())
        assert data["schema_version"] == 1
        assert data["game_mode"] == "f1_26"
        assert "session_id" in data and "track_id" in data and "car_id" in data
        assert isinstance(data["laps"], list)

    def test_csv_export_of_laps(self):
        text = session(times=[92.4, 92.0]).laps_to_csv()
        lines = text.strip().splitlines()

        assert lines[0].startswith("lap_number,lap_time_s")
        assert len(lines) == 3  # header plus two laps

    @pytest.mark.parametrize(
        "payload,message",
        [
            ({"schema_version": 99, "game_mode": "f1_25", "session_id": "x"},
             "schema_version"),
            ({"schema_version": 1, "game_mode": "f1_99", "session_id": "x"},
             "game_mode"),
            ({"schema_version": 1, "game_mode": "f1_25", "session_id": ""},
             "session_id"),
        ],
    )
    def test_18_invalid_data_is_refused(self, payload, message):
        with pytest.raises(HistoryError, match=message):
            SessionRecord.from_dict(payload)

    def test_corrupt_laps_are_refused(self):
        payload = {
            "schema_version": 1, "game_mode": "f1_25", "session_id": "x",
            "laps": [{"lap_number": "first"}],
        }
        with pytest.raises(HistoryError, match="corrupt session data"):
            SessionRecord.from_dict(payload)

    def test_malformed_json_is_refused(self):
        with pytest.raises(HistoryError, match="not valid JSON"):
            SessionRecord.from_json("{{{")

    def test_16_importing_another_modes_session_is_refused(self):
        store = SessionStore(GameMode.F1_26)
        foreign = session(mode=GameMode.F1_25, session_id="f")

        with pytest.raises(HistoryError, match="f1_25"):
            store.import_session(foreign.to_json())

    def test_a_corrupt_file_does_not_lose_the_others(self):
        store = SessionStore(GameMode.F1_25)
        store.save(session(session_id="good"))
        store.directory.mkdir(parents=True, exist_ok=True)
        (store.directory / "bad.json").write_text("{{{", encoding="utf-8")

        loaded = store.load_all()
        assert [s.session_id for s in loaded] == ["good"]


class TestModeIsolation:
    def test_14_and_15_sessions_never_cross_modes(self):
        collect([92.4, 92.0], mode=GameMode.F1_25)
        collect([88.0, 88.2], mode=GameMode.F1_26)

        f25 = SessionStore(GameMode.F1_25).load_all()
        f26 = SessionStore(GameMode.F1_26).load_all()

        assert len(f25) == 1 and len(f26) == 1
        assert f25[0].best_lap_s == 92.0
        assert f26[0].best_lap_s == 88.0

    def test_the_directories_are_separate(self):
        assert sessions_dir(GameMode.F1_25) != sessions_dir(GameMode.F1_26)

    def test_a_file_claiming_another_mode_is_ignored(self):
        store = SessionStore(GameMode.F1_26)
        store.directory.mkdir(parents=True, exist_ok=True)
        foreign = session(mode=GameMode.F1_25, session_id="x")
        (store.directory / "x.json").write_text(foreign.to_json(), encoding="utf-8")

        assert store.load_all() == []

    def test_switching_mode_closes_the_session(self):
        collector = collect([92.4, 92.0])
        collector.set_mode(GameMode.F1_26)

        assert collector.record is None
        assert SessionStore(GameMode.F1_25).load_all()[0].state == (
            SessionState.FINISHED.value
        )


class TestReplayDeterminism:
    def _fingerprint(self, tmp_path, monkeypatch, name):
        monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path / name))
        collector = collect([92.4, 92.0, 91.8, 92.1])
        return collector.record.fingerprint()

    def test_19_the_same_laps_produce_the_same_fingerprint(self, tmp_path, monkeypatch):
        """Session id and wall-clock time differ between runs; the driving
        does not, and that is what a replay must reproduce."""
        first = self._fingerprint(tmp_path, monkeypatch, "a")
        second = self._fingerprint(tmp_path, monkeypatch, "b")
        assert first == second

    def test_different_laps_produce_a_different_fingerprint(self):
        """Guards the test above from passing for everything."""
        a = collect([92.4, 92.0]).record.fingerprint()
        b = collect([92.4, 91.0]).record.fingerprint()
        assert a != b


class TestApplicationIntegration:
    @pytest.fixture
    def app(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path))
        from app.config.settings import AppSettings
        from app.core.application import Application

        instance = Application(AppSettings(game_mode="f1_26"))
        instance.mode_settings.auto_start_telemetry = False
        instance.persist_on_exit = False
        yield instance
        instance.shutdown()

    def _drive(self, app, laps=6, base=92.0):
        for index in range(laps):
            common = dict(
                valid=True, game="f1", tyre_compound="Medium",
                tyre_age_laps=index + 1, sector1_time_s=30.4,
                sector2_time_s=31.1, position=5, total_laps=40,
                session_type="Race", weather="Clear",
            )
            app._on_telemetry_frame(TelemetryFrame(current_lap=index + 1, **common))
            app._on_telemetry_frame(
                TelemetryFrame(
                    current_lap=index + 2, last_lap_time_s=base + index * 0.1,
                    **common
                )
            )

    def test_laps_reach_the_history(self, app):
        self._drive(app)
        assert app.history.record is not None
        assert app.history.record.laps_completed == 6

    def test_22_history_survives_going_stale(self, app):
        """The headline requirement, through the real application."""
        self._drive(app)
        before = [lap.lap_time_s for lap in app.history.record.laps]

        app.telemetry.set_timeout(0.01)
        import time

        time.sleep(0.05)
        report = app.report()
        assert report.stale

        assert app.history.state is SessionState.STALE
        assert [lap.lap_time_s for lap in app.history.record.laps] == before

    def test_stale_then_live_keeps_everything(self, app):
        self._drive(app)
        before = app.history.record.laps_completed
        app.telemetry.set_timeout(0.01)
        import time

        time.sleep(0.05)
        app.report()

        app.telemetry.set_timeout(2.0)
        self._drive(app, laps=2, base=91.5)
        assert app.history.record.laps_completed >= before

    def test_shutdown_saves_the_session(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path))
        from app.config.settings import AppSettings
        from app.core.application import Application

        instance = Application(AppSettings(game_mode="f1_26"))
        instance.mode_settings.auto_start_telemetry = False
        instance.persist_on_exit = False
        self._drive(instance)
        instance.shutdown()

        stored = SessionStore(GameMode.F1_26).load_all()
        assert stored and stored[0].state == SessionState.FINISHED.value

    def test_the_session_carries_stints_and_observations(self, app):
        self._drive(app, laps=12)
        record = app.history.record
        assert record.stints
        assert record.stints[0].compound == "Medium"

    def test_history_is_queryable_from_the_application(self, app):
        self._drive(app)
        history = app.session_history()
        assert history.sessions
        assert history.personal_best() is not None
