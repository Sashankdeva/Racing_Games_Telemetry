"""Dark theme: palette constants and the application stylesheet.

Colour is used as information, not decoration. Teal means live/healthy,
amber means degraded, red means a problem, grey means inactive. Nothing is
coloured just to look busy - on a page you glance at mid-corner, every
splash of colour has to mean something.
"""

from __future__ import annotations

# --- palette --------------------------------------------------------------
BG = "#0B0E13"
SURFACE = "#141920"
SURFACE_ALT = "#1B222C"
SURFACE_HOVER = "#212B36"
BORDER = "#252E3A"
BORDER_LIGHT = "#324053"

TEXT = "#E8ECF1"
TEXT_DIM = "#8A95A5"
TEXT_FAINT = "#5C6675"

ACCENT = "#FF3D2E"
ACCENT_HOVER = "#FF5648"
ACCENT_DIM = "#7A2118"

LIVE = "#00E5C0"
LIVE_DIM = "#0C6355"
WARN = "#FFB020"
DANGER = "#FF3B4E"
IDLE = "#4A5566"

#: Motor meter gradient, low to high intensity.
METER_LOW = "#00E5C0"
METER_MID = "#FFB020"
METER_HIGH = "#FF3D2E"

FONT_FAMILY = "Segoe UI, Inter, system-ui, sans-serif"


STYLESHEET = f"""
* {{
    font-family: {FONT_FAMILY};
    outline: none;
}}

QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-size: 13px;
}}

QMainWindow, QDialog {{ background-color: {BG}; }}

/* Labels must not paint the page background over the lighter card surface,
   which otherwise leaves a dark bar behind every caption. */
QLabel {{ background: transparent; }}

/* ---------------- sidebar ---------------- */
#Sidebar {{
    background-color: {SURFACE};
    border-right: 1px solid {BORDER};
}}
#BrandTitle {{
    font-size: 15px;
    font-weight: 700;
    color: {TEXT};
    letter-spacing: 0.5px;
}}
#BrandSubtitle {{
    font-size: 10px;
    font-weight: 600;
    color: {ACCENT};
    letter-spacing: 1.6px;
}}
#NavButton {{
    background-color: transparent;
    border: none;
    border-left: 3px solid transparent;
    padding: 11px 16px;
    text-align: left;
    font-size: 13px;
    font-weight: 500;
    color: {TEXT_DIM};
    border-radius: 0px;
}}
#NavButton:hover {{
    background-color: {SURFACE_HOVER};
    color: {TEXT};
}}
#NavButton:checked {{
    background-color: {SURFACE_ALT};
    border-left: 3px solid {ACCENT};
    color: {TEXT};
    font-weight: 600;
}}

/* ---------------- page shell ---------------- */
#PageTitle {{
    font-size: 24px;
    font-weight: 700;
    color: {TEXT};
}}
#PageSubtitle {{
    font-size: 13px;
    color: {TEXT_DIM};
}}
QScrollArea {{ background-color: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background-color: transparent; }}

/* ---------------- cards ---------------- */
#Card {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
#CardTitle {{
    font-size: 12px;
    font-weight: 700;
    color: {TEXT};
    letter-spacing: 0.8px;
}}
#CardHint {{
    font-size: 11px;
    color: {TEXT_FAINT};
}}
#SectionLabel {{
    font-size: 11px;
    font-weight: 700;
    color: {TEXT_FAINT};
    letter-spacing: 1.2px;
}}
#Hint {{ font-size: 11px; color: {TEXT_FAINT}; }}
#ValueLabel {{
    font-size: 13px;
    font-weight: 600;
    color: {ACCENT};
}}
#BigValue {{
    font-size: 30px;
    font-weight: 700;
    color: {TEXT};
}}
#BigValueUnit {{ font-size: 12px; color: {TEXT_DIM}; }}
#StatLabel {{
    font-size: 10px;
    font-weight: 700;
    color: {TEXT_FAINT};
    letter-spacing: 1.1px;
}}
#Mono {{
    font-family: "Cascadia Mono", Consolas, monospace;
    font-size: 12px;
    color: {TEXT_DIM};
}}

/* ---------------- buttons ---------------- */
QPushButton {{
    background-color: {SURFACE_ALT};
    border: 1px solid {BORDER_LIGHT};
    border-radius: 6px;
    padding: 8px 16px;
    font-size: 12px;
    font-weight: 600;
    color: {TEXT};
}}
QPushButton:hover {{ background-color: {SURFACE_HOVER}; border-color: {ACCENT_DIM}; }}
QPushButton:pressed {{ background-color: {BG}; }}
QPushButton:disabled {{ color: {TEXT_FAINT}; border-color: {BORDER}; background-color: {SURFACE}; }}

QPushButton#Primary {{
    background-color: {ACCENT};
    border: 1px solid {ACCENT};
    color: #FFFFFF;
}}
QPushButton#Primary:hover {{ background-color: {ACCENT_HOVER}; }}
QPushButton#Primary:disabled {{ background-color: {ACCENT_DIM}; border-color: {ACCENT_DIM}; color: {TEXT_DIM}; }}

QPushButton#Danger {{
    background-color: transparent;
    border: 1px solid {DANGER};
    color: {DANGER};
}}
QPushButton#Danger:hover {{ background-color: {DANGER}; color: #FFFFFF; }}

QPushButton#EmergencyStop {{
    background-color: {DANGER};
    border: 2px solid #FF6B7A;
    border-radius: 8px;
    color: #FFFFFF;
    font-size: 15px;
    font-weight: 800;
    letter-spacing: 2px;
    padding: 16px 20px;
}}
QPushButton#EmergencyStop:hover {{ background-color: #FF5566; }}
QPushButton#EmergencyStop:pressed {{ background-color: #D42030; }}
QPushButton#EmergencyStop:checked {{
    background-color: {WARN};
    border-color: #FFD070;
    color: #241800;
}}

QPushButton#Ghost {{
    background-color: transparent;
    border: 1px solid {BORDER_LIGHT};
    color: {TEXT_DIM};
    padding: 6px 12px;
}}
QPushButton#Ghost:hover {{ color: {TEXT}; border-color: {ACCENT}; }}

QPushButton#Preset {{
    background-color: {SURFACE_ALT};
    border: 1px solid {BORDER_LIGHT};
    padding: 10px 8px;
    font-size: 12px;
}}
QPushButton#Preset:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}

/* ---------------- inputs ---------------- */
QSlider::groove:horizontal {{
    height: 4px;
    background: {BORDER};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {TEXT};
    width: 14px;
    height: 14px;
    margin: -6px 0;
    border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{ background: {ACCENT}; }}
QSlider::groove:horizontal:disabled {{ background: {SURFACE_ALT}; }}
QSlider::sub-page:horizontal:disabled {{ background: {TEXT_FAINT}; }}
QSlider::handle:horizontal:disabled {{ background: {TEXT_FAINT}; }}

QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {{
    background-color: {SURFACE_ALT};
    border: 1px solid {BORDER_LIGHT};
    border-radius: 6px;
    padding: 7px 10px;
    font-size: 12px;
    color: {TEXT};
    selection-background-color: {ACCENT};
}}
QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover, QLineEdit:hover {{ border-color: {ACCENT_DIM}; }}
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
/* Triangle drawn purely from borders, so no image asset is needed.
   width/height must be 0 or Qt reserves a box and the borders never meet. */
QComboBox::down-arrow {{
    image: none;
    width: 0px;
    height: 0px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {TEXT_DIM};
    margin-right: 8px;
}}
QComboBox::down-arrow:hover {{ border-top-color: {ACCENT}; }}
QComboBox QAbstractItemView {{
    background-color: {SURFACE_ALT};
    border: 1px solid {BORDER_LIGHT};
    border-radius: 6px;
    selection-background-color: {ACCENT};
    color: {TEXT};
    padding: 4px;
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 16px; background: transparent; border: none; }}

QCheckBox {{ spacing: 8px; font-size: 12px; color: {TEXT}; }}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border-radius: 4px;
    border: 1px solid {BORDER_LIGHT};
    background: {SURFACE_ALT};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}

/* ---------------- misc ---------------- */
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER_LIGHT}; border-radius: 5px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {TEXT_FAINT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; border: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {BORDER_LIGHT}; border-radius: 5px; min-width: 30px; }}

QToolTip {{
    background-color: {SURFACE_ALT};
    color: {TEXT};
    border: 1px solid {BORDER_LIGHT};
    border-radius: 4px;
    padding: 5px 8px;
}}

QPlainTextEdit, QTextEdit {{
    background-color: {BG};
    border: 1px solid {BORDER};
    border-radius: 6px;
    font-family: "Cascadia Mono", Consolas, monospace;
    font-size: 11px;
    color: {TEXT_DIM};
    selection-background-color: {ACCENT};
}}

QFrame#Divider {{ background-color: {BORDER}; max-height: 1px; border: none; }}
"""
