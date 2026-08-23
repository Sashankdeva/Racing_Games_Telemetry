"""Telemetry - the complete normalized frame.

The Dashboard is deliberately sparse so it can be read at speed. This page
is the opposite: everything the parser produces, grouped by source, for
when something needs checking rather than driving.

Fields the active game does not provide are shown as UNAVAILABLE rather
than as a plausible-looking zero, and anything whose mapping has not been
verified for the running title is marked UNCONFIRMED. Both come from the
game profile, so neither is a judgement made here.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QLabel, QVBoxLayout, QWidget

from app.core.models import TelemetryFrame
from app.core.telemetry_state import TelemetryStatus
from app.diagnostics.metrics import DiagnosticsReport
from app.games.modes import Capability
from app.ui import theme
from app.ui.pages.base import Page
from app.ui.widgets.common import Card

#: Placeholder for a field the game genuinely does not send.
UNAVAILABLE = "UNAVAILABLE"


class _Readout(QWidget):
    """One label/value row, monospaced so columns line up while scanning."""

    def __init__(self, label: str) -> None:
        super().__init__()
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(12)

        self._label = QLabel(label)
        self._label.setObjectName("Hint")
        layout.addWidget(self._label, 0, 0)

        self._value = QLabel("-")
        self._value.setObjectName("Mono")
        layout.addWidget(self._value, 0, 1)
        layout.setColumnStretch(1, 1)

    def set(self, value: str, colour: str = "") -> None:
        if self._value.text() != value:
            self._value.setText(value)
        self._value.setStyleSheet(f"color: {colour};" if colour else "")

    def dim(self) -> None:
        """Mark the value as stale without changing it."""
        self._value.setStyleSheet(f"color: {theme.TEXT_FAINT};")


class TelemetryPage(Page):
    title = "Telemetry"
    subtitle = "Every field the parser produces, grouped by source packet"

    #: (key, label, source packet) - grouping by source means a whole
    #: missing packet type reads as one block rather than as a scatter of
    #: unrelated faults.
    GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
        ("Car telemetry", (
            ("speed", "Speed"),
            ("rpm", "Engine RPM"),
            ("max_rpm", "Rev limit"),
            ("gear", "Gear"),
            ("throttle", "Throttle"),
            ("brake", "Brake"),
            ("clutch", "Clutch"),
            ("steering", "Steering"),
            ("drs", "DRS / override"),
        )),
        ("Tyres", (
            ("compound", "Compound"),
            ("tyre_age", "Tyre age"),
            ("tyre_wear", "Wear"),
            ("tyre_surface", "Surface temp"),
            ("tyre_inner", "Inner temp"),
            ("tyre_pressure", "Pressure"),
            ("brake_temp", "Brake temp"),
            ("surfaces", "Surface type"),
        )),
        ("Lap & position", (
            ("position", "Position"),
            ("lap", "Lap"),
            ("sector", "Sector"),
            ("last_lap", "Last lap"),
            ("best_lap", "Best lap"),
            ("s1", "Sector 1"),
            ("s2", "Sector 2"),
            ("lap_distance", "Lap distance"),
            ("gap_ahead", "Gap ahead"),
            ("gap_leader", "Gap to leader"),
            ("lap_invalid", "Lap valid"),
            ("penalties", "Penalties"),
        )),
        ("Fuel & energy", (
            ("fuel", "Fuel in tank"),
            ("fuel_capacity", "Fuel capacity"),
            ("fuel_laps", "Fuel delta"),
            ("ers_store", "ERS store"),
            ("ers_mode", "ERS mode"),
            ("ers_deployed", "ERS deployed"),
            ("ers_harvested", "ERS harvested"),
        )),
        ("Motion", (
            ("g_lat", "G lateral"),
            ("g_lon", "G longitudinal"),
            ("g_vert", "G vertical"),
            ("wheel_slip", "Wheel slip"),
            ("wheel_speed", "Wheel speed"),
            ("susp_pos", "Suspension pos"),
        )),
        ("Session & conditions", (
            ("session_type", "Session"),
            ("weather", "Weather"),
            ("air_temp", "Air temp"),
            ("track_temp", "Track temp"),
            ("time_left", "Time left"),
            ("pit_status", "Pit status"),
            ("safety_car", "Safety car"),
        )),
        ("Damage", (
            ("damage", "Reported damage"),
        )),
    )

    def build(self) -> None:
        self._rows: dict[str, _Readout] = {}

        columns = QGridLayout()
        columns.setHorizontalSpacing(16)
        columns.setVerticalSpacing(16)

        for index, (title, fields) in enumerate(self.GROUPS):
            card = Card(title)
            for key, label in fields:
                row = _Readout(label)
                self._rows[key] = row
                card.body.addWidget(row)
            # Top-aligned so a short card does not float in the middle of a
            # row sized by a taller neighbour.
            columns.addWidget(card, index // 2, index % 2, Qt.AlignmentFlag.AlignTop)

        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.addLayout(columns)
        self.body.addWidget(holder)
        self.body.addStretch(1)

    # ------------------------------------------------------------------
    def refresh(self, report: DiagnosticsReport) -> None:
        if report.status is TelemetryStatus.NO_DATA:
            for row in self._rows.values():
                row.set("-", theme.TEXT_FAINT)
            return
        # Stale values are still real values; they are dimmed rather than
        # discarded so the full frame stays inspectable after a dropout.
        self._show(report.frame)
        if report.stale:
            for row in self._rows.values():
                row.dim()

    def _show(self, f: TelemetryFrame) -> None:
        game = self.app.game
        set_row = self._set

        # --- car telemetry ---
        set_row("speed", f"{f.speed_kph:.0f} kph")
        set_row("rpm", f"{f.rpm:.0f}")
        set_row("max_rpm", f"{f.max_rpm:.0f}" if f.max_rpm else UNAVAILABLE)
        set_row("gear", _gear(f.gear))
        set_row("throttle", f"{f.throttle * 100:.0f}%")
        set_row("brake", f"{f.brake * 100:.0f}%")
        set_row("clutch", f"{f.clutch * 100:.0f}%")
        set_row("steering", f"{f.steering:+.2f}")

        # DRS is DRS in F1 25 and active aero / manual override in F1 26.
        drs_status = game.status(
            Capability.ACTIVE_AERO if game.drs.has_active_aero else Capability.DRS
        )
        set_row(
            "drs",
            f"{game.term('drs')}: {'OPEN' if f.drs_active else 'closed'}"
            + ("  (UNCONFIRMED)" if drs_status == "unconfirmed" else ""),
            theme.WARN if drs_status == "unconfirmed" else "",
        )

        # --- tyres ---
        set_row("compound", f.tyre_compound or UNAVAILABLE)
        set_row("tyre_age", f"{f.tyre_age_laps} laps" if f.tyre_age_laps >= 0 else UNAVAILABLE)
        set_row("tyre_wear", _wheels(f.tyre_wear, "%"))
        set_row("tyre_surface", _wheels(f.tyre_surface_temp, "C"))
        set_row("tyre_inner", _wheels(f.tyre_inner_temp, "C"))
        set_row("tyre_pressure", _wheels(f.tyre_pressure, "psi"))
        set_row("brake_temp", _wheels(f.brake_temp, "C"))
        set_row(
            "surfaces",
            " ".join(s.name[:4] for s in f.surfaces.as_tuple())
            if f.surfaces.as_tuple() else UNAVAILABLE,
        )

        # --- lap & position ---
        set_row("position", str(f.position) if f.position else UNAVAILABLE)
        set_row("lap", f"{f.current_lap}/{f.total_laps}" if f.total_laps else str(f.current_lap))
        set_row("sector", str(f.sector + 1))
        set_row("last_lap", _time(f.last_lap_time_s))
        set_row("best_lap", _time(f.best_lap_time_s))
        set_row("s1", _time(f.sector1_time_s))
        set_row("s2", _time(f.sector2_time_s))
        set_row("lap_distance", f"{f.lap_distance_m:.0f} m")
        set_row("gap_ahead", f"{f.delta_to_car_ahead_s:.3f} s" if f.delta_to_car_ahead_s else "-")
        set_row("gap_leader", f"{f.delta_to_leader_s:.3f} s" if f.delta_to_leader_s else "-")
        set_row(
            "lap_invalid",
            "INVALID" if f.lap_invalid else "valid",
            theme.DANGER if f.lap_invalid else theme.LIVE,
        )
        set_row("penalties", f"{f.penalties_s}s" if f.penalties_s else "none")

        # --- fuel & energy ---
        set_row("fuel", f"{f.fuel_in_tank:.2f} kg" if f.fuel_in_tank else UNAVAILABLE)
        set_row("fuel_capacity", f"{f.fuel_capacity:.1f} kg" if f.fuel_capacity else UNAVAILABLE)
        set_row("fuel_laps", f"{f.fuel_remaining_laps:+.2f} laps")
        set_row("ers_store", f"{f.ers_store_percent:.1f}%")
        set_row("ers_mode", f.ers_mode or UNAVAILABLE)
        set_row("ers_deployed", f"{f.ers_deployed_lap / 1000:.0f} kJ")
        set_row("ers_harvested", f"{f.ers_harvested_lap / 1000:.0f} kJ")

        # --- motion ---
        set_row("g_lat", f"{f.g_lateral:+.2f}")
        set_row("g_lon", f"{f.g_longitudinal:+.2f}")
        set_row("g_vert", f"{f.g_vertical:+.2f}")
        set_row("wheel_slip", _wheels(f.wheel_slip_ratio, "", 3))
        set_row("wheel_speed", _wheels(f.wheel_speed, ""))
        set_row("susp_pos", _wheels(f.suspension_position, "", 3))

        # --- session ---
        set_row("session_type", f.session_type or UNAVAILABLE)
        set_row("weather", f.weather or UNAVAILABLE)
        set_row("air_temp", f"{f.air_temperature:.0f} C" if f.air_temperature else UNAVAILABLE)
        set_row("track_temp", f"{f.track_temperature:.0f} C" if f.track_temperature else UNAVAILABLE)
        set_row("time_left", _time(f.session_time_left_s))
        set_row("pit_status", "IN PITS" if f.in_pits else "on track")
        set_row("safety_car", f.safety_car or "none")

        damage = f.damage_summary()
        set_row("damage", damage or "none reported", theme.WARN if damage else "")

    def _set(self, key: str, value: str, colour: str = "") -> None:
        row = self._rows.get(key)
        if row is None:
            return
        if not colour and value == UNAVAILABLE:
            colour = theme.TEXT_FAINT
        row.set(value, colour)


def _gear(gear: int) -> str:
    return "R" if gear < 0 else "N" if gear == 0 else str(gear)


def _time(seconds: float) -> str:
    if seconds <= 0:
        return "-"
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    return f"{minutes}:{remainder:06.3f}" if minutes else f"{remainder:.3f}"


def _wheels(wheels, unit: str, decimals: int = 1) -> str:
    values = wheels.as_tuple()
    if not any(values):
        return UNAVAILABLE
    joined = " ".join(f"{v:.{decimals}f}" for v in values)
    return f"{joined} {unit}".strip()
