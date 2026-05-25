"""
APEX UI Styles — V7
"""

# V3.1.6 — theme presets. apply_theme() mutates COLORS + BOT_COLOR in-place
# and returns the new stylesheet string — pass it to QApplication.setStyleSheet()
# for a fully live theme change without restarting the app.
THEMES: dict = {
    # V4.0.0 — BAPTOU brand palette. The traditional green-as-outline
    # is gone; the "green" slot is now a clear teal-cyan that still
    # reads as "positive / active" without screaming "stock-broker
    # generic". P/L numbers still use red for losses, but profits now
    # render in the same teal so the brand is consistent end-to-end.
    "Default (BAPTOU teal)": {
        "green":  "#3eb8a4", "red":    "#c28e97", "orange": "#c8a070",
        "yellow": "#cdc578", "purple": "#8a93c9",
        "bg":     "#161a26", "bg2":    "#1d2336",
        # V4.6.6 — sobered-up palette. Border was #2a3447 (high contrast,
        # showed up as visible outlines on every card); softened to
        # blend into panel so containers separate by subtle background
        # tint instead of hard outlines. Muted text bumped from
        # #6a7894 to #9aa6c4 for readability over the dark blue bg.
        "panel":  "#1a1f2d", "panel2": "#222837", "border": "#222837",
        "text":   "#e3e8f2", "muted":  "#9aa6c4",
    },
    "Midnight Blue": {
        "green":  "#67a8d6", "red":    "#d68f9c", "orange": "#d6a36a",
        "yellow": "#d8d27a", "purple": "#9ba5e0",
        "bg":     "#0f1320", "bg2":    "#161d34",
        "panel":  "#141a2c", "panel2": "#1c2440", "border": "#1c2440",
        "text":   "#e5eaf3", "muted":  "#9ba6c2",
    },
    "Forest": {
        "green":  "#a3c98a", "red":    "#c9928a", "orange": "#d2a973",
        "yellow": "#e2d986", "purple": "#a8b1d3",
        "bg":     "#161c19", "bg2":    "#1e2922",
        "panel":  "#1c2620", "panel2": "#243029", "border": "#2f3d34",
        "text":   "#dde5dc", "muted":  "#7a8a7d",
    },
    "Cyberpunk": {
        "green":  "#3eea88", "red":    "#ff5c93", "orange": "#ffae54",
        "yellow": "#ffe14c", "purple": "#b164ff",
        "bg":     "#0c0820", "bg2":    "#190f33",
        "panel":  "#140e2a", "panel2": "#1e1538", "border": "#382562",
        "text":   "#ede5ff", "muted":  "#7c70a8",
    },
    "Solarized Dark": {
        "green":  "#859900", "red":    "#dc322f", "orange": "#cb4b16",
        "yellow": "#b58900", "purple": "#6c71c4",
        "bg":     "#002b36", "bg2":    "#04323d",
        "panel":  "#073642", "panel2": "#0c4252", "border": "#1f5b6c",
        "text":   "#eee8d5", "muted":  "#586e75",
    },
}


COLORS = dict(THEMES["Default (BAPTOU teal)"])

# v3.1.6 — Read the user's saved theme BEFORE DARK_STYLESHEET is built
# below. The stylesheet is now a function (build_stylesheet) so it re-reads
# COLORS every time it's called, making live theme switching possible.
try:
    import json as _json, os as _os
    from pathlib import Path as _Path
    _stg = _Path(_os.environ.get(
        "APEX_DATA_DIR",
        _os.path.expandvars(r"%LocalAppData%\APEX Trading Platform")
    )) / "apex_settings.json"
    if _stg.exists():
        _saved = _json.loads(_stg.read_text(encoding="utf-8")).get("theme")
        if _saved and _saved in THEMES:
            COLORS.update(THEMES[_saved])
except Exception:
    pass


def build_stylesheet(colors=None) -> str:
    """Build the complete QSS stylesheet from the given (or current) COLORS palette.
    Called once at import and again by apply_theme() for live updates."""
    c = colors if colors is not None else COLORS
    return f"""
/* ── GLOBAL ── */
QWidget {{
    background-color: transparent;
    color: {c['text']};
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
}}

QMainWindow {{
    background-color: {c['bg']};
}}

/* ── TABS ── */
QTabWidget#mainTabs::pane {{
    border: none;
    background: transparent;
}}

QTabWidget#mainTabs > QWidget {{
    background: transparent;
}}

QTabBar::tab {{
    background: transparent;
    color: {c['muted']};
    font-family: 'JetBrains Mono';
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 1px;
    padding: 13px 22px;
    border: none;
    border-bottom: 2px solid transparent;
    margin-right: 2px;
}}

QTabBar::tab:selected {{
    color: {c['text']};
    border-bottom: 2px solid {c['green']};
}}

QTabBar::tab:hover:!selected {{
    color: {c['text']};
    background: rgba(255,255,255,0.025);
}}

/* ── CORNER BUTTONS (Universe / Tools) ── */
QPushButton#cornerBtn {{
    background: transparent;
    color: {c['muted']};
    border: none;
    border-bottom: 2px solid transparent;
    font-family: 'JetBrains Mono';
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 1px;
    padding: 13px 14px;
    border-radius: 0;
    min-width: 0;
    margin: 0;
}}

QPushButton#cornerBtn:hover {{
    color: {c['text']};
}}

QPushButton#cornerBtn[active="true"] {{
    color: {c['text']};
    border-bottom: 2px solid {c['purple']};
}}

/* ── TAB QUICK CONTROLS ── */
QPushButton#tabPlayBtn {{
    background: transparent;
    color: {c['muted']};
    border: none;
    padding: 0;
    font-size: 8px;
    border-radius: 2px;
    min-width: 0;
    max-width: 16px;
    max-height: 16px;
}}

QPushButton#tabPlayBtn:hover {{
    color: {c['green']};
    background: rgba(122,181,162,0.15);
}}

QPushButton#tabStopBtn {{
    background: transparent;
    color: {c['muted']};
    border: none;
    padding: 0;
    font-size: 8px;
    border-radius: 2px;
    min-width: 0;
    max-width: 16px;
    max-height: 16px;
}}

QPushButton#tabStopBtn:hover {{
    color: {c['red']};
    background: rgba(194,142,151,0.15);
}}

QPushButton#tabRemoveBtn {{
    background: transparent;
    color: transparent;
    border: none;
    padding: 0;
    font-size: 8px;
    border-radius: 2px;
    min-width: 0;
    max-width: 16px;
    max-height: 16px;
}}

QPushButton#tabRemoveBtn:hover {{
    color: {c['muted']};
    background: rgba(255,255,255,0.08);
}}

/* ── SCROLL AREA ── */
QScrollArea {{
    border: none;
    background: transparent;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 4px;
    border-radius: 2px;
    margin: 0;
}}

QScrollBar::handle:vertical {{
    background: {c['border']};
    border-radius: 2px;
    min-height: 30px;
}}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {{ height: 0; }}

QScrollBar:horizontal {{
    background: transparent;
    height: 4px;
    border-radius: 2px;
}}

QScrollBar::handle:horizontal {{
    background: {c['border']};
    border-radius: 2px;
    min-width: 30px;
}}

QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ── FRAMES / PANELS ── */
QFrame#card {{
    background: {c['panel']};
    border: none;
    border-radius: 8px;
}}

QFrame#sectionFrame {{
    background: transparent;
    border: none;
}}

/* ── LABELS ── */
QLabel#sectionTitle {{
    font-family: 'Syne', sans-serif;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 2px;
    color: {c['muted']};
    padding: 0;
}}

QLabel#cardLabel {{
    font-size: 8px;
    color: {c['muted']};
    letter-spacing: 3px;
}}

QLabel#cardValue {{
    font-family: 'Syne', sans-serif;
    font-size: 18px;
    font-weight: 700;
    color: {c['text']};
}}

QLabel#cardSub {{
    font-size: 9px;
    color: {c['muted']};
}}

/* ── BUTTONS ── */
QPushButton {{
    background: {c['panel2']};
    color: {c['text']};
    border: none;
    border-radius: 5px;
    font-family: 'JetBrains Mono';
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 1px;
    padding: 8px 18px;
}}

QPushButton:hover {{
    color: {c['text']};
    background: {c['panel']};
}}

QPushButton#runBtn {{
    background: rgba(122,181,162,0.10);
    color: {c['green']};
    border: 1px solid rgba(122,181,162,0.45);
}}

QPushButton#runBtn:hover {{
    background: rgba(122,181,162,0.18);
    border-color: {c['green']};
}}

QPushButton#stopBtn {{
    background: rgba(194,142,151,0.10);
    color: {c['red']};
    border: 1px solid rgba(194,142,151,0.45);
}}

QPushButton#stopBtn:hover {{
    background: rgba(194,142,151,0.18);
    border-color: {c['red']};
}}

QPushButton#dangerBtn {{
    background: rgba(194,142,151,0.08);
    color: {c['red']};
    border: 1px solid rgba(194,142,151,0.35);
}}

QPushButton#dangerBtn:hover {{
    background: rgba(194,142,151,0.16);
    border-color: {c['red']};
}}

QPushButton#toolBtn {{
    background: rgba(138,147,201,0.08);
    color: {c['purple']};
    border: 1px solid rgba(138,147,201,0.35);
}}

QPushButton#toolBtn:hover {{
    background: rgba(138,147,201,0.16);
    border-color: {c['purple']};
}}

QPushButton#updateBtn {{
    background: rgba(138,147,201,0.10);
    color: {c['purple']};
    border: 1px solid rgba(138,147,201,0.45);
    font-size: 10px;
    padding: 6px 16px;
}}

/* V3.2.0 — broker-mode selector chip in the header */
QPushButton#brokerModeBtn {{
    background: rgba(138,147,201,0.08);
    color: {c['purple']};
    border: 1px solid {c['border']};
    border-radius: 5px;
    font-family: 'JetBrains Mono';
    font-size: 10px;
    letter-spacing: 1px;
    font-weight: 600;
    padding: 6px 12px;
    margin-left: 6px;
}}
QPushButton#brokerModeBtn:hover {{
    background: rgba(138,147,201,0.18);
    border-color: {c['purple']};
}}

/* Manual trading mode toggle */
QPushButton#manualModeBtn {{
    background: transparent;
    color: {c['muted']};
    border: 1px solid {c['border']};
    border-radius: 5px;
    font-family: 'JetBrains Mono';
    font-size: 10px;
    letter-spacing: 1px;
    font-weight: 600;
    padding: 6px 12px;
    margin-left: 6px;
}}
QPushButton#manualModeBtn:hover {{
    background: rgba(200,160,112,0.12);
    border-color: {c['orange']};
    color: {c['orange']};
}}
QPushButton#manualModeBtn[active="true"] {{
    background: rgba(200,160,112,0.14);
    color: {c['orange']};
    border-color: {c['orange']};
}}

/* V3.0.1 — clickable user chip in the header (opens Account menu) */
QPushButton#userChipBtn {{
    background: transparent;
    color: {c['muted']};
    border: 1px solid transparent;
    border-radius: 5px;
    font-family: 'JetBrains Mono';
    font-size: 10px;
    letter-spacing: 0.5px;
    padding: 6px 12px;
    margin-left: 12px;
    text-align: center;
}}

QPushButton#userChipBtn:hover {{
    color: {c['text']};
    border-color: {c['border']};
    background: rgba(255,255,255,0.04);
}}

QPushButton#userChipBtn:pressed {{
    background: rgba(255,255,255,0.08);
}}

/* Drop-down menu used by the user chip (Switch / Sign-out) */
QMenu {{
    background: {c['panel']};
    color: {c['text']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    padding: 6px;
    font-family: 'JetBrains Mono';
    font-size: 11px;
}}

QMenu::item {{
    padding: 8px 22px 8px 16px;
    border-radius: 5px;
    margin: 1px 0;
}}

QMenu::item:selected {{
    background: {c['panel2']};
    color: {c['text']};
}}

QMenu::separator {{
    height: 1px;
    background: {c['border']};
    margin: 4px 6px;
}}

/* V7.1.7: explicit Quit button in the header */
QPushButton#quitBtn {{
    background: transparent;
    color: {c['muted']};
    border: 1px solid {c['border']};
    border-radius: 5px;
    font-family: 'JetBrains Mono';
    font-size: 10px;
    letter-spacing: 2px;
    padding: 6px 14px;
    margin-left: 8px;
}}

QPushButton#quitBtn:hover {{
    color: {c['red']};
    border-color: rgba(194,142,151,0.55);
    background: rgba(194,142,151,0.08);
}}

QPushButton#addBotBtn {{
    background: rgba(122,181,162,0.08);
    color: {c['green']};
    border: 1px solid rgba(122,181,162,0.35);
    font-size: 10px;
    padding: 6px 14px;
}}

QPushButton#addBotBtn:hover {{
    background: rgba(122,181,162,0.16);
    border-color: {c['green']};
}}

QPushButton#silenceBtn {{
    background: rgba(92,107,130,0.07);
    color: {c['muted']};
    border: none;
    font-size: 10px;
    padding: 5px 12px;
}}

QPushButton#silenceBtn:hover {{
    background: rgba(92,107,130,0.14);
}}

/* ── TABLE ── */
QTableWidget {{
    background: {c['panel']};
    color: {c['text']};
    border: none;
    border-radius: 6px;
    gridline-color: {c['border']};
    font-size: 11px;
    selection-background-color: {c['panel2']};
}}

QTableWidget::item {{
    padding: 9px 12px;
    border: none;
}}

QTableWidget::item:selected {{
    background: {c['panel2']};
    color: {c['text']};
}}

QHeaderView::section {{
    background: {c['panel2']};
    color: {c['muted']};
    font-size: 9px;
    font-weight: 600;
    letter-spacing: 2px;
    padding: 9px 12px;
    border: none;
    border-bottom: 1px solid {c['border']};
}}

/* ── COMBO BOX ── */
QComboBox {{
    background: {c['panel']};
    color: {c['text']};
    border: 1px solid {c['border']};
    border-radius: 5px;
    padding: 5px 10px;
    font-size: 11px;
    min-width: 80px;
}}

QComboBox::drop-down {{
    border: none;
    width: 20px;
}}

QComboBox QAbstractItemView {{
    background: {c['panel']};
    color: {c['text']};
    border: 1px solid {c['border']};
    selection-background-color: {c['panel2']};
}}

/* ── PROGRESS BAR ── */
QProgressBar {{
    background: {c['panel']};
    border: none;
    border-radius: 4px;
    text-align: center;
    color: {c['text']};
    font-size: 10px;
}}

QProgressBar::chunk {{
    background: {c['green']};
    border-radius: 3px;
}}

/* ── TEXT EDIT ── */
QTextEdit, QPlainTextEdit {{
    background: {c['panel2']};
    color: {c['text']};
    border: none;
    border-radius: 5px;
    font-size: 11px;
    padding: 6px;
}}

/* ── LINE EDIT ── */
QLineEdit {{
    background: {c['panel']};
    color: {c['text']};
    border: none;
    border-radius: 5px;
    padding: 6px 12px;
    font-size: 11px;
}}

QLineEdit:focus {{
    background: {c['panel2']};
}}

/* ── MESSAGE BOX ── */
QMessageBox {{
    background: {c['panel']};
    color: {c['text']};
}}

QMessageBox QLabel {{
    color: {c['text']};
}}

/* ── STATUS BAR ── */
QStatusBar {{
    background: {c['panel']};
    color: {c['muted']};
    font-size: 10px;
    border-top: 1px solid {c['border']};
}}

/* ── SPLITTER ── */
QSplitter::handle {{
    background: {c['border']};
}}

/* ── TOOLTIP ── */
QToolTip {{
    background: {c['panel']};
    color: {c['text']};
    border: 1px solid {c['border']};
    font-size: 11px;
    padding: 5px 10px;
    border-radius: 4px;
}}

/* ── CHECKBOX ── */
QCheckBox {{
    color: {c['text']};
    spacing: 6px;
}}

QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {c['border']};
    border-radius: 3px;
    background: {c['panel']};
}}

QCheckBox::indicator:checked {{
    background: {c['green']};
    border-color: {c['green']};
}}

/* ── SPIN BOX ── */
QDoubleSpinBox, QSpinBox {{
    background: {c['panel']};
    color: {c['text']};
    border: 1px solid {c['border']};
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 11px;
}}

QDoubleSpinBox:focus, QSpinBox:focus {{
    border-color: {c['muted']};
}}

QDoubleSpinBox::up-button, QDoubleSpinBox::down-button,
QSpinBox::up-button, QSpinBox::down-button {{
    border: none;
    background: transparent;
    width: 14px;
}}
"""


def apply_theme(name: str) -> str:
    """Mutate COLORS + BOT_COLOR in-place, rebuild and return the new stylesheet.
    For a fully live update: QApplication.instance().setStyleSheet(apply_theme(name))"""
    if name not in THEMES:
        return build_stylesheet()
    COLORS.update(THEMES[name])
    BOT_COLOR.update({
        "LONG":  COLORS["green"],
        "SHORT": COLORS["red"],
        "DAY":   COLORS["orange"],
    })
    return build_stylesheet()


C = COLORS

BOT_COLOR = {
    "LONG":  C["green"],
    "SHORT": C["red"],
    "DAY":   C["orange"],
}

DARK_STYLESHEET = build_stylesheet()  # backward compat — evaluated once at import
