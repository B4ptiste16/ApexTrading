"""
APEX UI Styles — V7
"""

COLORS = {
    # V7.1.6 — softer accent palette, lighter dark backgrounds.
    # The previous teal-green and coral-red looked a touch neon at
    # standard monitor brightness; these are desaturated by ~25 %.
    # Backgrounds also moved one step lighter so the gradient feels
    # less cave-like.
    "green":  "#7ab5a2",   # was #5fa68f → softer sage-teal
    "red":    "#c28e97",   # was #b66f7a → lighter rose
    "orange": "#c8a070",   # was #c89060 → lighter amber
    "yellow": "#cdc578",   # was #d6c95e
    "purple": "#8a93c9",   # kept — already soft
    "bg":     "#161a26",   # gradient start  (was #0c0f16)
    "bg2":    "#1d2336",   # gradient end    (was #131a2a)
    "panel":  "#1a1f2d",   # card / row bg   (was #111622)
    "panel2": "#222837",   # nested bg       (was #181f2e)
    "border": "#2a3447",   # 1px frames      (was #232d40)
    "text":   "#d8dde8",   # body text       (kept)
    "muted":  "#6a7894",   # secondary text  (was #5c6b82)
}

C = COLORS

BOT_COLOR = {
    "LONG":  C["green"],
    "SHORT": C["red"],
    "DAY":   C["orange"],
}

DARK_STYLESHEET = f"""
/* ── GLOBAL ── */
QWidget {{
    background-color: transparent;
    color: {C['text']};
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
}}

QMainWindow {{
    background-color: {C['bg']};
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
    color: {C['muted']};
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
    color: {C['text']};
    border-bottom: 2px solid {C['green']};
}}

QTabBar::tab:hover:!selected {{
    color: {C['text']};
    background: rgba(255,255,255,0.025);
}}

/* ── CORNER BUTTONS (Universe / Tools) ── */
QPushButton#cornerBtn {{
    background: transparent;
    color: {C['muted']};
    border: none;
    border-bottom: 2px solid transparent;
    font-family: 'JetBrains Mono';
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 1px;
    padding: 7px 14px;
    border-radius: 0;
    min-width: 0;
}}

QPushButton#cornerBtn:hover {{
    color: {C['text']};
}}

QPushButton#cornerBtn[active="true"] {{
    color: {C['text']};
    border-bottom: 2px solid {C['purple']};
}}

/* ── TAB QUICK CONTROLS ── */
QPushButton#tabPlayBtn {{
    background: transparent;
    color: {C['muted']};
    border: none;
    padding: 0;
    font-size: 8px;
    border-radius: 2px;
    min-width: 0;
    max-width: 16px;
    max-height: 16px;
}}

QPushButton#tabPlayBtn:hover {{
    color: {C['green']};
    background: rgba(122,181,162,0.15);
}}

QPushButton#tabStopBtn {{
    background: transparent;
    color: {C['muted']};
    border: none;
    padding: 0;
    font-size: 8px;
    border-radius: 2px;
    min-width: 0;
    max-width: 16px;
    max-height: 16px;
}}

QPushButton#tabStopBtn:hover {{
    color: {C['red']};
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
    color: {C['muted']};
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
    background: {C['border']};
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
    background: {C['border']};
    border-radius: 2px;
    min-width: 30px;
}}

QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ── FRAMES / PANELS ── */
QFrame#card {{
    background: {C['panel']};
    border: 1px solid {C['border']};
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
    color: {C['muted']};
    padding: 0;
}}

QLabel#cardLabel {{
    font-size: 8px;
    color: {C['muted']};
    letter-spacing: 3px;
}}

QLabel#cardValue {{
    font-family: 'Syne', sans-serif;
    font-size: 18px;
    font-weight: 700;
    color: {C['text']};
}}

QLabel#cardSub {{
    font-size: 9px;
    color: {C['muted']};
}}

/* ── BUTTONS ── */
QPushButton {{
    background: {C['panel2']};
    color: {C['text']};
    border: 1px solid {C['border']};
    border-radius: 5px;
    font-family: 'JetBrains Mono';
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 1px;
    padding: 8px 18px;
}}

QPushButton:hover {{
    color: {C['text']};
    border-color: {C['muted']};
    background: {C['panel']};
}}

QPushButton#runBtn {{
    background: rgba(122,181,162,0.10);
    color: {C['green']};
    border: 1px solid rgba(122,181,162,0.55);
}}

QPushButton#runBtn:hover {{
    background: rgba(122,181,162,0.18);
    border-color: {C['green']};
}}

QPushButton#stopBtn {{
    background: rgba(194,142,151,0.10);
    color: {C['red']};
    border: 1px solid rgba(194,142,151,0.55);
}}

QPushButton#stopBtn:hover {{
    background: rgba(194,142,151,0.18);
    border-color: {C['red']};
}}

QPushButton#dangerBtn {{
    background: rgba(194,142,151,0.08);
    color: {C['red']};
    border: 1px solid rgba(194,142,151,0.45);
}}

QPushButton#dangerBtn:hover {{
    background: rgba(194,142,151,0.16);
    border-color: {C['red']};
}}

QPushButton#toolBtn {{
    background: rgba(138,147,201,0.08);
    color: {C['purple']};
    border: 1px solid rgba(138,147,201,0.45);
}}

QPushButton#toolBtn:hover {{
    background: rgba(138,147,201,0.16);
    border-color: {C['purple']};
}}

QPushButton#updateBtn {{
    background: rgba(138,147,201,0.10);
    color: {C['purple']};
    border: 1px solid rgba(138,147,201,0.45);
    font-size: 10px;
    padding: 6px 16px;
}}

/* V7.1.7: explicit Quit button in the header */
QPushButton#quitBtn {{
    background: transparent;
    color: {C['muted']};
    border: 1px solid {C['border']};
    border-radius: 5px;
    font-family: 'JetBrains Mono';
    font-size: 10px;
    letter-spacing: 2px;
    padding: 6px 14px;
    margin-left: 8px;
}}

QPushButton#quitBtn:hover {{
    color: {C['red']};
    border-color: rgba(194,142,151,0.55);
    background: rgba(194,142,151,0.08);
}}

QPushButton#addBotBtn {{
    background: rgba(122,181,162,0.08);
    color: {C['green']};
    border: 1px solid rgba(122,181,162,0.40);
    font-size: 10px;
    padding: 6px 14px;
}}

QPushButton#addBotBtn:hover {{
    background: rgba(122,181,162,0.16);
    border-color: {C['green']};
}}

QPushButton#silenceBtn {{
    background: rgba(92,107,130,0.07);
    color: {C['muted']};
    border: 1px solid rgba(92,107,130,0.30);
    font-size: 10px;
    padding: 5px 12px;
}}

QPushButton#silenceBtn:hover {{
    background: rgba(92,107,130,0.14);
}}

/* ── TABLE ── */
QTableWidget {{
    background: {C['panel']};
    color: {C['text']};
    border: 1px solid {C['border']};
    border-radius: 6px;
    gridline-color: {C['border']};
    font-size: 11px;
    selection-background-color: {C['panel2']};
}}

QTableWidget::item {{
    padding: 9px 12px;
    border: none;
}}

QTableWidget::item:selected {{
    background: {C['panel2']};
    color: {C['text']};
}}

QHeaderView::section {{
    background: {C['panel2']};
    color: {C['muted']};
    font-size: 9px;
    font-weight: 600;
    letter-spacing: 2px;
    padding: 9px 12px;
    border: none;
    border-bottom: 1px solid {C['border']};
}}

/* ── COMBO BOX ── */
QComboBox {{
    background: {C['panel']};
    color: {C['text']};
    border: 1px solid {C['border']};
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
    background: {C['panel']};
    color: {C['text']};
    border: 1px solid {C['border']};
    selection-background-color: {C['panel2']};
}}

/* ── PROGRESS BAR ── */
QProgressBar {{
    background: {C['panel']};
    border: 1px solid {C['border']};
    border-radius: 4px;
    text-align: center;
    color: {C['text']};
    font-size: 10px;
}}

QProgressBar::chunk {{
    background: {C['green']};
    border-radius: 3px;
}}

/* ── TEXT EDIT ── */
QTextEdit, QPlainTextEdit {{
    background: {C['panel2']};
    color: {C['text']};
    border: 1px solid {C['border']};
    border-radius: 5px;
    font-size: 11px;
    padding: 6px;
}}

/* ── LINE EDIT ── */
QLineEdit {{
    background: {C['panel']};
    color: {C['text']};
    border: 1px solid {C['border']};
    border-radius: 5px;
    padding: 6px 12px;
    font-size: 11px;
}}

QLineEdit:focus {{
    border-color: {C['muted']};
}}

/* ── MESSAGE BOX ── */
QMessageBox {{
    background: {C['panel']};
    color: {C['text']};
}}

QMessageBox QLabel {{
    color: {C['text']};
}}

/* ── STATUS BAR ── */
QStatusBar {{
    background: {C['panel']};
    color: {C['muted']};
    font-size: 10px;
    border-top: 1px solid {C['border']};
}}

/* ── SPLITTER ── */
QSplitter::handle {{
    background: {C['border']};
}}

/* ── TOOLTIP ── */
QToolTip {{
    background: {C['panel']};
    color: {C['text']};
    border: 1px solid {C['border']};
    font-size: 11px;
    padding: 5px 10px;
    border-radius: 4px;
}}

/* ── CHECKBOX ── */
QCheckBox {{
    color: {C['text']};
    spacing: 6px;
}}

QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {C['border']};
    border-radius: 3px;
    background: {C['panel']};
}}

QCheckBox::indicator:checked {{
    background: {C['green']};
    border-color: {C['green']};
}}

/* ── SPIN BOX ── */
QDoubleSpinBox, QSpinBox {{
    background: {C['panel']};
    color: {C['text']};
    border: 1px solid {C['border']};
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 11px;
}}

QDoubleSpinBox:focus, QSpinBox:focus {{
    border-color: {C['muted']};
}}

QDoubleSpinBox::up-button, QDoubleSpinBox::down-button,
QSpinBox::up-button, QSpinBox::down-button {{
    border: none;
    background: transparent;
    width: 14px;
}}
"""
