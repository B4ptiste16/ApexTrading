"""
V4.6.68 — Bot-card enrichment + Details/telemetry dialog.

Adds, to the existing bot cards (More Bots), the meta the user expects:
  • AI app used to BUILD the bot  (logo chip)
  • AI apps it can RUN on          (logo chips)
  • compatible brokers             (chips)
  • estimated cost / day
and a Details button → a dialog with the full description + a telemetry chart
(returns by AI confidence).

AI apps are shown as LOGOS: if a PNG exists at assets/ai_logos/<provider>.png it
is used; otherwise a clean branded chip (brand colour + name) is drawn as a
fallback so the feature works out-of-the-box and upgrades automatically when
real logo files are dropped in.
"""

from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QLabel, QHBoxLayout, QVBoxLayout, QPushButton, QDialog, QFrame,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap

from ui.styles import COLORS
import core.data as D

C = COLORS

# provider key -> (display name, brand colour)
AI_BRANDS = {
    "anthropic": ("Claude",  "#d97757"),
    "claude":    ("Claude",  "#d97757"),
    "openai":    ("OpenAI",  "#10a37f"),
    "gpt":       ("OpenAI",  "#10a37f"),
    "google":    ("Gemini",  "#4285f4"),
    "gemini":    ("Gemini",  "#4285f4"),
    "xai":       ("Grok",    "#c9d1d9"),
    "grok":      ("Grok",    "#c9d1d9"),
    "groq":      ("Groq",    "#f55036"),
}
_ALL_PROVIDERS = ["anthropic", "openai", "google", "xai", "groq"]

_LOGO_DIR = Path(__file__).resolve().parent.parent / "assets" / "ai_logos"


def _norm_provider(p: str) -> str:
    p = (p or "").strip().lower()
    for key in AI_BRANDS:
        if key in p:
            return key if key in ("anthropic", "openai", "google", "xai",
                                  "groq") else AI_BRANDS_KEY_CANON.get(key, key)
    return p


AI_BRANDS_KEY_CANON = {"claude": "anthropic", "gpt": "openai",
                       "gemini": "google", "grok": "xai"}


def ai_logo_chip(provider: str, active: bool = False) -> QWidget:
    """A small logo chip for an AI provider. Uses assets/ai_logos/<p>.png if
    present, else a branded text chip."""
    key = _norm_provider(provider)
    name, brand = AI_BRANDS.get(key, AI_BRANDS.get(AI_BRANDS_KEY_CANON.get(key, ""),
                                                   (provider or "AI", C["muted"])))
    png = _LOGO_DIR / f"{key}.png"
    lbl = QLabel()
    if png.exists():
        try:
            pm = QPixmap(str(png)).scaledToHeight(
                18, Qt.TransformationMode.SmoothTransformation)
            lbl.setPixmap(pm)
            lbl.setToolTip(name)
            return lbl
        except Exception:
            pass
    # Branded text chip fallback
    lbl.setText(name)
    border = brand if active else "transparent"
    lbl.setStyleSheet(
        f"background:{brand}26;color:{brand};border:1px solid {border};"
        f"border-radius:9px;padding:2px 9px;font-size:10px;font-weight:700;")
    lbl.setToolTip(f"{name}" + ("  · used to build this bot" if active else ""))
    return lbl


def _broker_chip(broker: str) -> QLabel:
    name = {"alpaca": "Alpaca", "ibkr": "IBKR"}.get(broker.lower(), broker)
    col = C["green"] if broker.lower() == "alpaca" else C["purple"]
    lbl = QLabel(name)
    lbl.setStyleSheet(
        f"background:{col}22;color:{col};border-radius:9px;"
        f"padding:2px 9px;font-size:10px;font-weight:700;")
    return lbl


def _chip_row(label: str, widgets: list) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(5)
    cap = QLabel(label)
    cap.setStyleSheet(f"color:{C['muted']};font-size:9px;letter-spacing:1px;")
    cap.setFixedWidth(58)
    h.addWidget(cap)
    for x in widgets:
        h.addWidget(x)
    h.addStretch()
    return w


def _custom_source(side: str) -> str:
    try:
        reg = D.load_bot_registry()
        for c in reg.get("custom", []):
            if str(c.get("id", "")).upper() == side.upper():
                p = Path(c.get("script", ""))
                if p.exists():
                    return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return ""


def card_meta(side: str, info: dict, is_custom: bool) -> dict:
    """Resolve the display meta for a bot (built-in or custom)."""
    meta = {
        "description": info.get("description", ""),
        "method":      "",
        "ai_used":     "anthropic",
        "compatible":  list(_ALL_PROVIDERS),
        "brokers":     info.get("brokers", ["alpaca", "ibkr"]),
    }
    if is_custom:
        try:
            from core.bot_meta import parse_meta
            m = parse_meta(_custom_source(side))
            if m.get("description"): meta["description"] = m["description"]
            meta["method"] = m.get("method", "")
            if m.get("ai_used"):
                meta["ai_used"] = (m["ai_used"][0] if isinstance(m["ai_used"], list)
                                   else m["ai_used"])
            if m.get("compatible_models"):
                meta["compatible"] = m["compatible_models"]
            if m.get("brokers"):
                meta["brokers"] = m["brokers"]
        except Exception:
            pass
    # Current AI provider / model wired to this bot
    try:
        env = D.read_env_keys()
        meta["provider"] = (env.get(f"AI_PROVIDER_{side.upper()}")
                            or env.get("AI_PROVIDER") or meta["ai_used"])
        meta["model"] = (env.get(f"AI_MODEL_{side.upper()}")
                         or env.get("AI_MODEL") or "default")
    except Exception:
        meta["provider"] = meta["ai_used"]; meta["model"] = "default"
    # Estimated cost
    try:
        c = D.estimate_api_costs(side)
        meta["cost_day"] = c.get("per_day", 0.0)
    except Exception:
        meta["cost_day"] = 0.0
    return meta


def add_card_meta(layout: QVBoxLayout, side: str, info: dict,
                  is_custom: bool, parent=None) -> None:
    """Append the AI/broker chips + a Details button to an existing bot card."""
    m = card_meta(side, info, is_custom)
    layout.addWidget(_chip_row("BUILT WITH", [ai_logo_chip(m["ai_used"], True)]))
    comp = [ai_logo_chip(p) for p in (m["compatible"] or [])[:5]]
    if comp:
        layout.addWidget(_chip_row("RUNS ON", comp))
    layout.addWidget(_chip_row(
        "BROKERS", [_broker_chip(b) for b in (m["brokers"] or [])]))
    det = QPushButton("Details")
    det.setObjectName("toolBtn")
    det.setStyleSheet(
        f"QPushButton{{background:rgba(138,147,201,0.15);color:{C['purple']};"
        f"border:none;border-radius:4px;padding:4px 8px;font-size:10px;"
        f"font-weight:600;}}QPushButton:hover{{background:rgba(138,147,201,0.30);}}")
    det.clicked.connect(lambda: BotDetailsDialog(side, info, is_custom, parent).exec())
    layout.addWidget(det)


class BotDetailsDialog(QDialog):
    def __init__(self, side: str, info: dict, is_custom: bool, parent=None):
        super().__init__(parent)
        m = card_meta(side, info, is_custom)
        self.setWindowTitle(f"{info.get('label', side)} — details")
        self.setMinimumSize(560, 560)
        self.setStyleSheet(f"background:{C['bg']};")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(10)

        title = QLabel(info.get("label", side))
        title.setStyleSheet(f"color:{C['text']};font-size:18px;font-weight:800;")
        lay.addWidget(title)

        desc = QLabel(m["description"] or "—")
        desc.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        desc.setWordWrap(True)
        lay.addWidget(desc)
        if m.get("method"):
            meth = QLabel("Method: " + m["method"])
            meth.setStyleSheet(f"color:{C['muted']};font-size:10px;")
            meth.setWordWrap(True)
            lay.addWidget(meth)

        lay.addWidget(_chip_row("BUILT WITH", [ai_logo_chip(m["ai_used"], True)]))
        lay.addWidget(_chip_row("RUNS ON",
                                [ai_logo_chip(p) for p in (m["compatible"] or [])[:5]]))
        lay.addWidget(_chip_row("BROKERS",
                                [_broker_chip(b) for b in (m["brokers"] or [])]))

        cd = m.get("cost_day", 0.0) or 0.0
        cost = QLabel(f"Estimated cost: ${cd:.2f}/day  ·  ${cd*30:.2f}/month   "
                      f"(currently running on {m.get('provider','—')} · "
                      f"{m.get('model','—')})")
        cost.setStyleSheet(f"color:{C['yellow']};font-size:10px;")
        cost.setWordWrap(True)
        lay.addWidget(cost)

        tele_hdr = QLabel("TELEMETRY")
        tele_hdr.setStyleSheet(
            f"color:{C['purple']};font-size:10px;letter-spacing:2px;"
            f"font-weight:700;margin-top:6px;")
        lay.addWidget(tele_hdr)
        try:
            from ui.widgets import ChartView
            import core.charts as CH
            chart = ChartView()
            chart.setMinimumHeight(280)
            chart.load_chart(CH.returns_by_confidence(D.load_bot_log(side), side))
            lay.addWidget(chart)
        except Exception as e:
            err = QLabel(f"telemetry unavailable: {e}")
            err.setStyleSheet(f"color:{C['muted']};font-size:10px;")
            lay.addWidget(err)

        note = QLabel("Returns-by-AI-model telemetry appears once this bot has "
                      "run under more than one model.")
        note.setStyleSheet(f"color:{C['muted']};font-size:9px;")
        note.setWordWrap(True)
        lay.addWidget(note)

        close = QPushButton("Close")
        close.setObjectName("addBotBtn")
        close.clicked.connect(self.accept)
        lay.addWidget(close)
