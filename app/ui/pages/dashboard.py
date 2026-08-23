"""Dashboard - the live telemetry view.

Phase 1 scope: prove every field arrives and is correct. The Driver Coach,
Strategy and Race Intelligence panels come in later phases; the layout
already leaves room for them rather than being rearranged later.

Values come from the telemetry state, not from any cached copy. A stale
frame keeps its last valid values and is flagged STALE with its age: a
dropped packet does not move the car off lap 18. Only NO_DATA - nothing
valid ever received - blanks the readouts.
"""

from __future__ import annotations

import time

from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.core.models import TelemetryFrame
from app.core.telemetry_state import TelemetryStatus
from app.diagnostics.metrics import DiagnosticsReport
from app.domain.lap_analysis import format_delta, format_lap_time
from app.games.base import TelemetryStage
from app.ui import theme
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, StatBlock, StatusPill
from app.domain.smart_suggestions import Severity
from app.ui.widgets.meters import BatteryMeter, InputBar, RpmBar, TyreGrid

#: The dashboard shows only the single most relevant suggestion - the
#: full list lives on the Suggestions page. Flooding the driving view with
#: five messages is exactly what the brief rules out.
MAX_SUGGESTIONS = 1
#: Suggestions are re-evaluated at most this often. The UI ticks at 20 Hz
#: and telemetry at 60; neither is a sensible rate for advice.
SUGGESTION_INTERVAL_S = 1.0

SEVERITY_COLOURS = {
    Severity.INFO: theme.LIVE,
    Severity.ADVISORY: theme.WARN,
    Severity.WARNING: theme.DANGER,
    Severity.CRITICAL: theme.DANGER,
}


class DashboardPage(Page):
    title = "Dashboard"
    subtitle = "Live telemetry"

    def build(self) -> None:
        self._last_suggestion_eval = 0.0
        self.body.addWidget(self._build_status())

        columns = QHBoxLayout()
        columns.setSpacing(16)
        columns.addWidget(self._build_car_column(), 3)
        columns.addWidget(self._build_session_column(), 2)
        self.body.addLayout(columns, 1)

    # ------------------------------------------------------------------
    def _build_status(self) -> QWidget:
        card = Card("Connection")
        row = QHBoxLayout()
        row.setSpacing(28)

        self._stage_pill = StatusPill("Waiting", theme.IDLE)
        # Update rate leads, because it is the number that says whether this
        # page is actually refreshing. Packets/s is ~7x higher (F1 sends
        # several packet types per tick) and reading it as an update rate
        # makes a perfectly healthy feed look broken.
        self._rate_stat = StatBlock("Update Rate", "0", "/s")
        self._packets_stat = StatBlock("Packets", "0", "/s")
        self._frames_stat = StatBlock("Frames", "0")

        for caption, widget in (("PIPELINE", self._stage_pill),):
            column = QVBoxLayout()
            column.setSpacing(3)
            label = QLabel(caption)
            label.setObjectName("StatLabel")
            column.addWidget(label)
            column.addWidget(widget)
            row.addLayout(column, 2)

        row.addWidget(self._rate_stat, 1)
        row.addWidget(self._packets_stat, 1)
        row.addWidget(self._frames_stat, 1)
        card.body.addLayout(row)

        # Shown only while stale, so old values can never be mistaken for
        # current ones. Hidden when live and when there is no data at all.
        self._stale_pill = StatusPill("", theme.WARN)
        self._stale_pill.hide()
        card.body.addWidget(self._stale_pill)

        # Says why there is no data, instead of leaving a wall of dashes to
        # be interpreted. Hidden entirely once telemetry is live.
        self._reason = QLabel("")
        self._reason.setObjectName("Hint")
        self._reason.setWordWrap(True)
        self._reason.hide()
        card.body.addWidget(self._reason)
        return card

    def _build_car_column(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        # --- engine ---
        engine = Card("Car")
        self._rpm_bar = RpmBar()
        engine.body.addWidget(self._rpm_bar)

        stats = QHBoxLayout()
        stats.setSpacing(20)
        self._speed = StatBlock("Speed", "-", "kph")
        self._rpm = StatBlock("RPM", "-")
        self._gear = StatBlock("Gear", "-")
        # Label comes from the game profile: "DRS" in F1 25,
        # "Manual Override" in F1 26.
        self._drs = StatBlock(self.app.game.term("drs"), "-")
        for widget in (self._speed, self._rpm, self._gear, self._drs):
            stats.addWidget(widget)
        engine.body.addLayout(stats)
        layout.addWidget(engine)

        # --- driver inputs ---
        inputs = Card("Inputs")
        self._throttle = InputBar("THROTTLE", theme.LIVE)
        self._brake = InputBar("BRAKE", theme.DANGER)
        self._steering = InputBar("STEERING", theme.ACCENT, bipolar=True)
        for widget in (self._throttle, self._brake, self._steering):
            inputs.body.addWidget(widget)
        layout.addWidget(inputs)

        # --- tyres ---
        tyres = Card("Tyres", hint="Surface temperature, pressure and wear per corner.")
        self._tyres = TyreGrid()
        tyres.body.addWidget(self._tyres)
        self._compound_label = QLabel("Compound: -")
        self._compound_label.setObjectName("Hint")
        tyres.body.addWidget(self._compound_label)
        # Stint degradation, measured. Says INSUFFICIENT DATA until it has
        # earned the right to show a figure.
        self._stint_label = QLabel("")
        self._stint_label.setObjectName("Hint")
        tyres.body.addWidget(self._stint_label)
        layout.addWidget(tyres)

        layout.addStretch(1)
        return container

    def _build_session_column(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        session = Card("Session")
        grid = QGridLayout()
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(12)
        self._position = StatBlock("Position", "-")
        self._lap = StatBlock("Lap", "-")
        self._gap_ahead = StatBlock("Gap Ahead", "-", "s")
        self._gap_leader = StatBlock("To Leader", "-", "s")
        self._last_lap = StatBlock("Last Lap", "-")
        self._best_lap = StatBlock("Best Lap", "-")
        for index, widget in enumerate(
            (
                self._position, self._lap,
                self._gap_ahead, self._gap_leader,
                self._last_lap, self._best_lap,
            )
        ):
            grid.addWidget(widget, index // 2, index % 2)
        session.body.addLayout(grid)
        layout.addWidget(session)

        resources = Card("Fuel & ERS")
        grid2 = QGridLayout()
        grid2.setHorizontalSpacing(20)
        grid2.setVerticalSpacing(12)
        self._fuel = StatBlock("Fuel", "-", "kg")
        self._fuel_laps = StatBlock("Fuel Delta", "-", "laps")
        self._ers = StatBlock("ERS Store", "-", "%")
        self._ers_mode = StatBlock("ERS Mode", "-")
        for index, widget in enumerate(
            (self._fuel, self._fuel_laps, self._ers, self._ers_mode)
        ):
            grid2.addWidget(widget, index // 2, index % 2)
        resources.body.addLayout(grid2)
        # Battery below the numbers: the meter answers "how much left" at a
        # glance, and turns red the moment Overtake is deployed.
        self._battery = BatteryMeter()
        resources.body.addWidget(self._battery)
        self._battery_note = QLabel("")
        self._battery_note.setObjectName("Hint")
        resources.body.addWidget(self._battery_note)
        layout.addWidget(resources)

        # Weather, damage, suspension and the rest of the frame live on the
        # Telemetry page. The brief is explicit that the driver must read
        # this screen in seconds, and every extra card costs that.
        pace = Card("Pace", hint="Measured from completed laps.")
        grid3 = QGridLayout()
        grid3.setHorizontalSpacing(20)
        grid3.setVerticalSpacing(12)
        self._theoretical = StatBlock("Theoretical", "-")
        self._last_delta = StatBlock("Last vs Best", "-")
        for index, widget in enumerate((self._theoretical, self._last_delta)):
            grid3.addWidget(widget, 0, index)
        pace.body.addLayout(grid3)
        self._pace_note = QLabel("No completed laps yet")
        self._pace_note.setObjectName("Hint")
        self._pace_note.setWordWrap(True)
        pace.body.addWidget(self._pace_note)
        layout.addWidget(pace)

        # Driver suggestions. Empty until something is actually worth
        # saying - an empty panel is the correct state most of the time.
        suggestions = Card(
            "Smart race engineer",
            hint="Only shown when something meaningful changes.",
        )
        self._suggestion_labels: list[QLabel] = []
        for _ in range(MAX_SUGGESTIONS):
            label = QLabel("")
            label.setWordWrap(True)
            label.hide()
            suggestions.body.addWidget(label)
            self._suggestion_labels.append(label)
        self._suggestion_empty = QLabel("Nothing to report.")
        self._suggestion_empty.setObjectName("Hint")
        suggestions.body.addWidget(self._suggestion_empty)
        layout.addWidget(suggestions)

        layout.addStretch(1)
        return container

    # ------------------------------------------------------------------
    def on_shown(self) -> None:
        # Terminology is mode-specific and the mode can change live.
        self._drs.set_label(self.app.game.term("drs"))

    def refresh(self, report: DiagnosticsReport) -> None:
        adapter = report.adapter
        if adapter is None:
            self._stage_pill.set_state("No adapter", theme.IDLE)
        else:
            stage = adapter.stage
            colour = {
                TelemetryStage.ERROR: theme.DANGER,
                TelemetryStage.WAITING: theme.IDLE,
                TelemetryStage.SOCKET_BOUND: theme.WARN,
                TelemetryStage.PACKETS_RECEIVED: theme.WARN,
                TelemetryStage.PACKETS_PARSED: theme.WARN,
                TelemetryStage.TELEMETRY_VALID: theme.WARN,
                TelemetryStage.TELEMETRY_LIVE: theme.LIVE,
            }[stage]
            self._stage_pill.set_state(f"{int(stage)}/6  {stage.label}", colour)
            self._rate_stat.set_value(f"{adapter.frame_rate:.0f}")
            self._packets_stat.set_value(f"{adapter.packet_rate:.0f}")
            self._frames_stat.set_value(f"{adapter.frames_emitted}")

        # Pace survives a dropout: the laps already completed are still
        # the truth about this session.
        self._refresh_pace()
        self._refresh_stint()
        self._refresh_suggestions(report)

        frame = report.frame
        if report.status is TelemetryStatus.NO_DATA:
            # Nothing has ever arrived: there is genuinely nothing to show.
            self._show_no_data()
            self._explain(adapter)
            return

        # LIVE or STALE both render the real values. Stale data is flagged,
        # not erased - the car is still on the same lap and tyres.
        self._show_frame(frame)
        if report.stale:
            self._show_stale(report.age)
        else:
            self._reason.hide()
            self._stale_pill.hide()

    def _show_stale(self, age_s: float) -> None:
        """Flag the values as old without taking them away."""
        self._stale_pill.set_state(f"STALE   last update {age_s:.1f}s ago", theme.WARN)
        self._stale_pill.show()
        self._reason.setText(
            "Telemetry has stopped. The values below are the last received, "
            "not current. Session history, best lap and stint data are kept."
        )
        self._reason.show()

    def _explain(self, adapter) -> None:
        """Say why the dashboard is empty, in terms of what to change.

        Each pipeline stage fails for a different reason, and the counters
        already distinguish them; without this the user only sees dashes and
        has to guess between a wrong port, a wrong packet format and a game
        that simply is not driving.
        """
        if adapter is None:
            text = "No game adapter is selected."
        elif adapter.error:
            text = adapter.error
        elif adapter.raw_packets == 0:
            text = (
                f"Nothing is arriving on UDP port {self.app.mode_settings.udp_port}. "
                "In-game: Settings > Telemetry Settings > UDP Telemetry ON, "
                f"IP 127.0.0.1, port {self.app.mode_settings.udp_port}."
            )
            if adapter.detected_port:
                text = (
                    f"Telemetry is arriving on port {adapter.detected_port}, but this "
                    f"app is listening on {self.app.mode_settings.udp_port}. "
                    "Change the port in Settings, or point the game here."
                )
        elif adapter.packets_parsed == 0:
            text = (
                f"{adapter.raw_packets} packets arrived but none could be decoded "
                "- this does not look like F1 UDP telemetry."
            )
        elif adapter.frames_emitted == 0:
            text = (
                f"Packets are arriving and decoding (format "
                f"{adapter.packet_format or 'unknown'}), but no car-telemetry frame "
                "has been produced yet. This is normal in a menu or replay - drive "
                "the car. If it persists on track, the car data does not match a "
                "layout this app knows."
            )
        else:
            text = (
                "Telemetry has stopped updating. The session is paused, in a menu, "
                "or the game stopped sending."
            )
        if adapter is not None and adapter.format_mismatch:
            text += (
                f"  Note: the game is sending packet format {adapter.packet_format}, "
                f"which is not what {self.app.game.display_name} mode expects - check "
                "the mode selector."
            )
        self._reason.setText(text)
        self._reason.show()

    def _show_no_data(self) -> None:
        self._stale_pill.hide()
        self._battery.clear()
        self._battery_note.setText("")
        for widget in (
            self._speed, self._rpm, self._gear, self._drs, self._position,
            self._lap, self._gap_ahead, self._gap_leader,
            self._last_lap, self._best_lap, self._fuel,
            self._fuel_laps, self._ers, self._ers_mode,
        ):
            widget.set_value("-")
        self._rpm_bar.set_ratio(0.0)
        for bar in (self._throttle, self._brake, self._steering):
            bar.set_value(0.0)
        self._tyres.clear()
        self._compound_label.setText("Compound: -")

    def _show_frame(self, f: TelemetryFrame) -> None:
        self._rpm_bar.set_ratio(f.rpm_ratio)
        self._speed.set_value(f"{f.speed_kph:.0f}")
        self._rpm.set_value(f"{f.rpm:.0f}")
        self._gear.set_value(_gear_text(f.gear))
        self._drs.set_value("OPEN" if f.drs_active else "closed")

        self._throttle.set_value(f.throttle)
        self._brake.set_value(f.brake)
        self._steering.set_value(f.steering)

        self._tyres.set_values(f.tyre_surface_temp, f.tyre_pressure, f.tyre_wear)
        self._compound_label.setText(
            f"Compound: {f.tyre_compound}"
            + (f"   age {f.tyre_age_laps} laps" if f.tyre_age_laps >= 0 else "")
        )

        self._position.set_value(str(f.position) if f.position else "-")
        self._lap.set_value(
            f"{f.current_lap}/{f.total_laps}" if f.total_laps else str(f.current_lap or "-")
        )
        self._gap_ahead.set_value(
            f"{f.delta_to_car_ahead_s:.3f}" if f.delta_to_car_ahead_s > 0 else "-"
        )
        self._gap_leader.set_value(
            f"{f.delta_to_leader_s:.3f}" if f.delta_to_leader_s > 0 else "-"
        )
        self._last_lap.set_value(_lap_time(f.last_lap_time_s))
        self._best_lap.set_value(_lap_time(f.best_lap_time_s))

        self._fuel.set_value(f"{f.fuel_in_tank:.1f}" if f.fuel_in_tank else "-")
        self._fuel_laps.set_value(
            f"{f.fuel_remaining_laps:+.1f}" if f.fuel_remaining_laps else "-"
        )
        self._ers.set_value(f"{f.ers_store_percent:.0f}")
        self._ers_mode.set_value(f.ers_mode or "-")
        self._battery.set_state(f.ers_store_percent, f.ers_mode, available=True)
        self._battery_note.setText(
            "OVERTAKE - deploying hard" if self._battery.overtaking else ""
        )


    def _refresh_suggestions(self, report: DiagnosticsReport) -> None:
        """Re-evaluate at a slow cadence, never per telemetry frame."""
        now = time.monotonic()
        if now - self._last_suggestion_eval < SUGGESTION_INTERVAL_S:
            return
        self._last_suggestion_eval = now

        active = self.app.suggestions.evaluate(
            self.app.suggestion_context(report)
        )
        self._suggestion_empty.setVisible(not active)
        for index, label in enumerate(self._suggestion_labels):
            if index >= len(active):
                label.hide()
                continue
            s = active[index]
            colour = SEVERITY_COLOURS[s.severity]
            label.setText(
                f"<span style='color:{colour};font-weight:700;'>"
                f"{s.severity.label}  {s.category.value}</span><br>"
                f"<span style='color:{theme.TEXT};'>{s.message}</span><br>"
                f"<span style='color:{theme.TEXT_FAINT};'>Why: {s.reason}</span><br>"
                f"<span style='color:{theme.TEXT_FAINT};font-size:10px;'>"
                f"Confidence: {s.confidence.value}</span>"
            )
            label.show()

    def _refresh_stint(self) -> None:
        """Current stint and its measured degradation, from cached state."""
        state = self.app.tyres
        if not self.app.stints:
            self._stint_label.setText("")
            return
        stint = self.app.stints[-1]
        self._stint_label.setText(
            f"Stint {stint.number} - {stint.compound or 'Unknown'}, "
            f"{stint.length} lap(s)   Degradation: {state.describe_degradation()}"
        )

    def _refresh_pace(self) -> None:
        """Pace comes from completed laps, so it updates on lap change only.

        Deliberately reads the Application's cached analysis rather than
        recomputing: this runs at UI rate, the analysis does not.
        """
        analysis = self.app.lap_analysis

        if analysis.theoretical_available:
            self._theoretical.set_value(format_lap_time(analysis.theoretical_best_s))
        else:
            self._theoretical.set_value("-")

        if analysis.has_pace and analysis.delta_to_best_s:
            self._last_delta.set_value(format_delta(analysis.delta_to_best_s))
        elif analysis.has_pace:
            self._last_delta.set_value("best")
        else:
            self._last_delta.set_value("-")

        if not analysis.has_pace:
            self._pace_note.setText("No completed laps yet")
            return

        parts = [f"{analysis.valid_laps} valid lap(s) - {analysis.confidence.value}"]
        worst = analysis.worst_sector()
        if worst is not None:
            parts.append(worst.describe())
        elif analysis.valid_laps > 1:
            parts.append("Last lap matched your session bests")
        if analysis.time_available_s > 0:
            parts.append(f"{analysis.time_available_s:.3f}s available on a clean lap")
        self._pace_note.setText("   ".join(parts))


def _gear_text(gear: int) -> str:
    if gear < 0:
        return "R"
    if gear == 0:
        return "N"
    return str(gear)


def _lap_time(seconds: float) -> str:
    if seconds <= 0:
        return "-"
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    return f"{minutes}:{remainder:06.3f}" if minutes else f"{remainder:.3f}"
