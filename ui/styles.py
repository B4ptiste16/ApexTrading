"""
APEX UI Styles
All Qt stylesheets and colour constants in one place.
"""

COLORS = {
    "green":  "#00f5c3",
    "red":    "#ff4d6d",
    "orange": "#ff9f43",
    "yellow": "#ffe353",
    "purple": "#7b8cff",
    "bg":     "#060a0e",
    "panel":  "#0b0f16",
    "panel2": "#0f1520",
    "border": "#18212e",
    "text":   "#d0d8e4",
    "muted":  "#4a5568",
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
    background: {C['panel']};
    color: {C['muted']};
    font-family: 'JetBrains Mono';
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 2px;
    padding: 10px 22px;
    border: 1px solid {C['border']};
    border-bottom: none;
    margin-right: 2px;
    border-radius: 6px 6px 0 0;
}}

QTabBar::tab:selected {{
    background: {C['panel2']};
    color: {C['text']};
    border-color: {C['border']};
}}

QTabBar::tab:hover:!selected {{
    color: {C['text']};
    background: {C['panel2']};
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
    border-radius: 8px;
}}

QFrame#sectionFrame {{
    background: transparent;
    border: none;
}}

/* ── LABELS ── */
QLabel#sectionTitle {{
    font-family: 'Syne', sans-serif;
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 4px;
    text-transform: uppercase;
    color: {C['green']};
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
    color: {C['muted']};
    border: 1px solid {C['border']};
    border-radius: 6px;
    font-family: 'JetBrains Mono';
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 1px;
    padding: 7px 16px;
}}

QPushButton:hover {{
    color: {C['text']};
    border-color: {C['border']};
    background: {C['panel']};
}}

QPushButton#runBtn {{
    background: rgba(0,245,195,0.1);
    color: {C['green']};
    border: 1px solid {C['green']};
}}

QPushButton#runBtn:hover {{
    background: rgba(0,245,195,0.2);
}}

QPushButton#stopBtn {{
    background: rgba(255,77,109,0.1);
    color: {C['red']};
    border: 1px solid {C['red']};
}}

QPushButton#stopBtn:hover {{
    background: rgba(255,77,109,0.2);
}}

QPushButton#dangerBtn {{
    background: rgba(255,77,109,0.08);
    color: {C['red']};
    border: 1px solid {C['red']};
}}

QPushButton#dangerBtn:hover {{
    background: rgba(255,77,109,0.2);
}}

QPushButton#toolBtn {{
    background: rgba(123,140,255,0.08);
    color: {C['purple']};
    border: 1px solid {C['purple']};
}}

QPushButton#toolBtn:hover {{
    background: rgba(123,140,255,0.2);
}}

QPushButton#updateBtn {{
    background: rgba(123,140,255,0.1);
    color: {C['purple']};
    border: 1px solid {C['purple']};
    font-size: 10px;
    padding: 5px 14px;
}}

/* ── TABLE ── */
QTableWidget {{
    background: {C['panel']};
    color: {C['text']};
    border: 1px solid {C['border']};
    border-radius: 6px;
    gridline-color: {C['border']};
    font-size: 11px;
    selection-background-color: #111a24;
}}

QTableWidget::item {{
    padding: 6px 10px;
    border: none;
}}

QTableWidget::item:selected {{
    background: #111a24;
    color: {C['text']};
}}

QHeaderView::section {{
    background: #0c1420;
    color: {C['green']};
    font-size: 9px;
    font-weight: 600;
    letter-spacing: 2px;
    text-transform: uppercase;
    padding: 6px 10px;
    border: 1px solid {C['border']};
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
    border-color: {C['green']};
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
