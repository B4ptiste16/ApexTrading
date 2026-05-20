"""
APEX UI Styles
All Qt stylesheets and colour constants in one place.
"""

COLORS = {
    "green":  "#3fb89a",
    "red":    "#c75c6b",
    "orange": "#d99a52",
    "yellow": "#d6c95e",
    "purple": "#8a93c9",
    "bg":     "#0a0d12",
    "panel":  "#10141b",
    "panel2": "#161b24",
    "border": "#222a36",
    "text":   "#d6dce6",
    "muted":  "#5a6478",
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
    background-color: {C['bg']};
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
    background: {C['bg']};
}}

QTabBar::tab {{
    background: transparent;
    color: {C['muted']};
    font-family: 'JetBrains Mono';
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 1px;
    padding: 12px 26px;
    border: none;
    border-bottom: 1px solid transparent;
    margin-right: 4px;
}}

QTabBar::tab:selected {{
    color: {C['text']};
    border-bottom: 1px solid {C['green']};
}}

QTabBar::tab:hover:!selected {{
    color: {C['text']};
}}

/* ── SCROLL AREA ── */
QScrollArea {{
    border: none;
    background: transparent;
}}

QScrollBar:vertical {{
    background: {C['bg']};
    width: 5px;
    border-radius: 2px;
}}

QScrollBar::handle:vertical {{
    background: {C['border']};
    border-radius: 2px;
    min-height: 20px;
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

QScrollBar:horizontal {{
    background: {C['bg']};
    height: 5px;
    border-radius: 2px;
}}

QScrollBar::handle:horizontal {{
    background: {C['border']};
    border-radius: 2px;
    min-width: 20px;
}}

/* ── FRAMES / PANELS ── */
QFrame#card {{
    background: {C['panel']};
    border: 1px solid {C['border']};
    border-radius: 6px;
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
    text-transform: uppercase;
    color: {C['muted']};
    padding: 0;
}}

QLabel#cardLabel {{
    font-size: 9px;
    color: {C['muted']};
    letter-spacing: 2px;
}}

QLabel#cardValue {{
    font-family: 'Syne', sans-serif;
    font-size: 20px;
    font-weight: 600;
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
    border-radius: 4px;
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
    background: rgba(63,184,154,0.10);
    color: {C['green']};
    border: 1px solid rgba(63,184,154,0.55);
}}

QPushButton#runBtn:hover {{
    background: rgba(63,184,154,0.18);
    border-color: {C['green']};
}}

QPushButton#stopBtn {{
    background: rgba(199,92,107,0.10);
    color: {C['red']};
    border: 1px solid rgba(199,92,107,0.55);
}}

QPushButton#stopBtn:hover {{
    background: rgba(199,92,107,0.18);
    border-color: {C['red']};
}}

QPushButton#dangerBtn {{
    background: rgba(199,92,107,0.08);
    color: {C['red']};
    border: 1px solid rgba(199,92,107,0.45);
}}

QPushButton#dangerBtn:hover {{
    background: rgba(199,92,107,0.16);
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
    padding: 8px 12px;
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
    text-transform: uppercase;
    padding: 8px 12px;
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
    min-width: 180px;
}}

QComboBox::drop-down {{
    border: none;
    width: 20px;
}}

QComboBox QAbstractItemView {{
    background: {C['panel']};
    color: {C['text']};
    border: 1px solid {C['border']};
    selection-background-color: #111a24;
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

/* ── TEXT EDIT (log view) ── */
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
    padding: 5px 10px;
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
    padding: 4px 8px;
    border-radius: 4px;
}}
"""
