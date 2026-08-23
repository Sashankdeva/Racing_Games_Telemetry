"""Games page - adapter status and telemetry configuration.

Unsupported adapters are shown honestly: Forza appears with its real state
("architecture ready"), never as a connected game. Anything else would make
the app lie to the user about what it can do.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
)

from app.diagnostics.metrics import DiagnosticsReport
from app.games.base import GameAdapter
from app.ui import theme
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, FieldRow, StatBlock, StatusPill


class GameCard(Card):
    def __init__(self, adapter: GameAdapter, page: "GamesPage") -> None:
        super().__init__(adapter.display_name, adapter.description)
        self.adapter = adapter
        self.page = page

        self._pill = StatusPill("-", theme.IDLE)
        self.body.addWidget(self._pill)

        if adapter.supported:
            self._port_spin = QSpinBox()
            self._port_spin.setRange(1024, 65535)
            self._port_spin.setValue(page.app.mode_settings.udp_port)
            self._port_spin.valueChanged.connect(page.app.set_udp_port)
            self.body.addWidget(
                FieldRow(
                    "UDP Port",
                    self._port_spin,
                    "Must match the port set in the game's telemetry settings.",
                )
            )

            stats = QHBoxLayout()
            stats.setSpacing(20)
            self._rate_stat = StatBlock("Packet Rate", "0", "/s")
            self._received_stat = StatBlock("Received", "0")
            self._rejected_stat = StatBlock("Rejected", "0")
            for stat in (self._rate_stat, self._received_stat, self._rejected_stat):
                stats.addWidget(stat)
            self.body.addLayout(stats)

            buttons = QHBoxLayout()
            buttons.setSpacing(8)
            self._toggle_button = QPushButton("Start Listening")
            self._toggle_button.setObjectName("Primary")
            self._toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._toggle_button.clicked.connect(self._on_toggle)
            buttons.addWidget(self._toggle_button)
            buttons.addStretch(1)
            self.body.addLayout(buttons)

            self._error_label = QLabel("")
            self._error_label.setStyleSheet(f"font-size: 11px; color: {theme.DANGER};")
            self._error_label.setWordWrap(True)
            self._error_label.setVisible(False)
            self.body.addWidget(self._error_label)

            # The single most common misconfiguration: the game sending to a
            # different port than the one being listened on. The scanner
            # watches the usual alternatives so this can be stated outright
            # instead of appearing as unexplained silence.
            self._hint_label = QLabel("")
            self._hint_label.setStyleSheet(f"font-size: 11px; color: {theme.WARN};")
            self._hint_label.setWordWrap(True)
            self._hint_label.setVisible(False)
            self.body.addWidget(self._hint_label)

            self._live_label = QLabel("")
            self._live_label.setObjectName("Mono")
            self._live_label.setVisible(False)
            self.body.addWidget(self._live_label)
        else:
            note = QLabel(
                "This adapter is a placeholder. The normalized telemetry model, "
                "dashboard and analysis layers are game-agnostic, so implementing "
                "the telemetry reader is all that is required."
            )
            note.setObjectName("Hint")
            note.setWordWrap(True)
            self.body.addWidget(note)

    def _on_toggle(self) -> None:
        if self.adapter.status().running:
            self.page.app.stop_telemetry()
        else:
            self.page.app.set_game(self.adapter.game_id)
            self.page.app.start_telemetry()

    def refresh(self) -> None:
        status = self.adapter.status()

        if not self.adapter.supported:
            self._pill.set_state("Architecture ready - not implemented", theme.IDLE)
            return

        if not status.running:
            self._pill.set_state("Stopped", theme.IDLE)
        elif status.connected:
            self._pill.set_state("Connected - receiving telemetry", theme.LIVE)
        else:
            self._pill.set_state("Listening - waiting for game", theme.WARN)

        self._rate_stat.set_value(f"{status.packet_rate:.0f}")
        self._received_stat.set_value(f"{status.packets_received}")
        self._rejected_stat.set_value(f"{status.packets_rejected}")
        self._toggle_button.setText("Stop Listening" if status.running else "Start Listening")

        if status.error:
            self._error_label.setText(status.error)
            self._error_label.setVisible(True)
        else:
            self._error_label.setVisible(False)

        if status.detected_port and status.packets_received == 0:
            self._hint_label.setText(
                f"Telemetry was detected on UDP port {status.detected_port}, but this "
                f"app is listening on {self.page.app.mode_settings.udp_port}. Either change "
                f"the port above to {status.detected_port}, or point the game at "
                f"{self.page.app.mode_settings.udp_port}."
            )
            self._hint_label.setVisible(True)
        elif status.running and status.packets_received == 0:
            self._hint_label.setText(
                "Socket is open but no packets have arrived. In game: Settings > "
                "Telemetry Settings > UDP Telemetry = On, IP 127.0.0.1, and note "
                "that F1 only streams while you are in a session, not in the menus."
            )
            self._hint_label.setVisible(True)
        else:
            self._hint_label.setVisible(False)

        if status.frames_emitted:
            self._live_label.setText(
                f"LIVE   RPM {status.live_rpm:.0f}/{status.live_max_rpm:.0f}   "
                f"{status.live_speed_kph:.0f} kph   gear {status.live_gear}   "
                f"thr {status.live_throttle * 100:.0f}%   brk {status.live_brake * 100:.0f}%"
            )
            self._live_label.setVisible(True)
        else:
            self._live_label.setVisible(False)


class GamesPage(Page):
    title = "Games"
    subtitle = "Telemetry sources. F1 is supported today; the engine itself is game-agnostic."

    def build(self) -> None:
        self._cards: list[GameCard] = []
        for adapter in self.app.adapters.values():
            card = GameCard(adapter, self)
            self._cards.append(card)
            self.body.addWidget(card)

        setup = Card(
            "Setting Up F1",
            hint="In-game: Settings > Telemetry Settings. Set UDP Telemetry to On, "
                 "UDP Broadcast Mode Off, IP Address 127.0.0.1 (or this PC's address if the "
                 "game runs elsewhere), and UDP Port to match the value above. A send rate of "
                 "60 Hz gives the engine the most to work with.",
        )
        self.body.addWidget(setup)
        self.body.addStretch(1)

    def refresh(self, report: DiagnosticsReport) -> None:
        for card in self._cards:
            card.refresh()
