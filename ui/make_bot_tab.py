"""
APEX  ·  Make Your Own Bot tab  (V7.1.9)
────────────────────────────────────────────────────────────────────────
Lets the user describe a trading bot in plain English and have an AI
generate the Python file that APEX can run. The user supplies their own
AI API key — Anthropic (Claude), OpenAI (GPT), or any OpenAI-compatible
endpoint via OpenRouter.

The generated file follows the APEX bot contract documented in
BOT_SKELETON.md: a top-level main() function, env-var key reads,
print-with-flush logging, no GUI, no extra dependencies. The skeleton
guide is sent to the model as the system prompt so the model always
produces a file APEX can actually launch.

No external SDK is required at runtime — we hit the providers' HTTP
APIs directly with `requests`, which is already in the desktop app's
dependencies.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

import requests
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui  import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from core      import data as D
from core.paths import DATA_DIR
from ui.styles import COLORS
from ui.widgets import ScrollContent, SectionHeader

C = COLORS


# ── Provider / model catalogue ────────────────────────────────────────

# Each provider entry knows:
#   url      — HTTP endpoint
#   models   — list of (label_for_dropdown, raw_model_id)
#   build_req(prompt, system, key, model) -> (headers, json_body)
#   extract_text(response_json) -> str  (the generated code)

def _anthropic_build(prompt: str, system: str, key: str, model: str) -> tuple[dict, dict]:
    return (
        {"x-api-key": key,
         "anthropic-version": "2023-06-01",
         "content-type": "application/json"},
        {"model": model, "max_tokens": 4096, "system": system,
         "messages": [{"role": "user", "content": prompt}]},
    )


def _anthropic_extract(j: dict) -> str:
    return "".join(part.get("text", "")
                   for part in j.get("content", [])
                   if part.get("type") == "text")


def _openai_build(prompt: str, system: str, key: str, model: str) -> tuple[dict, dict]:
    return (
        {"Authorization": f"Bearer {key}",
         "Content-Type":  "application/json"},
        {"model": model, "max_tokens": 4096,
         "messages": [{"role": "system", "content": system},
                      {"role": "user",   "content": prompt}]},
    )


def _openai_extract(j: dict) -> str:
    try:
        return j["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""


# V3.1.5 — Google Gemini's free tier (60 req/min, 1500/day per project)
# uses a slightly different request shape than OpenAI's. system prompt
# goes into `system_instruction`, user message into `contents`.

def _gemini_build(prompt: str, system: str, key: str, model: str) -> tuple[dict, dict]:
    return (
        {"x-goog-api-key": key,
         "Content-Type":   "application/json"},
        {"system_instruction": {"parts": [{"text": system}]},
         "contents": [{"role": "user", "parts": [{"text": prompt}]}],
         "generationConfig": {"maxOutputTokens": 4096, "temperature": 0.6}},
    )


def _gemini_extract(j: dict) -> str:
    try:
        return "".join(
            p.get("text", "")
            for p in j["candidates"][0]["content"]["parts"]
        )
    except (KeyError, IndexError, TypeError):
        return ""


def _apex_free_build(prompt: str, system: str, key: str, model: str) -> tuple[dict, dict]:
    """The 'Free (via APEX)' option doesn't need an API key — it routes
    through the APEX server which uses APEX's own pooled Anthropic key.
    Rate-limited to 5 calls / hour / signed-in user."""
    from ui.login import load_auth, load_server_url
    tok = (load_auth() or {}).get("token") or ""
    return (
        {"Authorization": f"Bearer {tok}",
         "Content-Type":  "application/json",
         "X-Apex-Endpoint": f"{load_server_url()}/api/makebot/generate"},
        {"prompt": prompt, "system": system},
    )


def _apex_free_extract(j: dict) -> str:
    return j.get("text", "") or ""


PROVIDERS = {
    "✨  Via APEX  (uses your APEX credits)": {
        # Sentinel URL — the worker replaces it with the X-Apex-Endpoint
        # header value at request time (so the server URL is dynamic).
        "url":     "__apex_dynamic__",
        "models":  [
            ("Claude Haiku  (APEX-hosted)", "apex-haiku"),
        ],
        "build":   _apex_free_build,
        "extract": _apex_free_extract,
        "free":    True,
        "credits": True,
    },
    "🎁  Google Gemini  (free 1500/day)": {
        # Gemini's URL embeds the model — {model} placeholder resolved
        # in the worker just before the POST. Free tier: ~1500 req/day,
        # 60 RPM. Get a key at https://aistudio.google.com/apikey.
        "url":     "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        "models":  [
            ("Gemini 2.5 Flash  (recommended)", "gemini-2.5-flash"),
            ("Gemini 2.5 Pro",                  "gemini-2.5-pro"),
            ("Gemini 2.0 Flash",                "gemini-2.0-flash"),
        ],
        "build":   _gemini_build,
        "extract": _gemini_extract,
        "free":    True,
    },
    "🎁  Groq  (free, fast Llama)": {
        # Groq has an OpenAI-compatible API. Free tier: ~14k tokens/min,
        # 30 RPM. Key at https://console.groq.com/keys.
        "url":     "https://api.groq.com/openai/v1/chat/completions",
        "models":  [
            ("Llama 3.3 70B  (recommended)",   "llama-3.3-70b-versatile"),
            ("Llama 3.1 8B Instant",           "llama-3.1-8b-instant"),
            ("Mixtral 8×7B",                   "mixtral-8x7b-32768"),
            ("Gemma 2 9B",                     "gemma2-9b-it"),
        ],
        "build":   _openai_build,
        "extract": _openai_extract,
        "free":    True,
    },
    "Anthropic (Claude)": {
        "url":     "https://api.anthropic.com/v1/messages",
        "models":  [
            ("Claude Sonnet 4.5  (best quality)",   "claude-sonnet-4-5-20250929"),
            ("Claude Haiku 4.5   (fast & cheap)",   "claude-haiku-4-5-20250514"),
            ("Claude Opus 4.7    (top tier)",       "claude-opus-4-7-20251101"),
        ],
        "build":   _anthropic_build,
        "extract": _anthropic_extract,
    },
    "OpenAI (ChatGPT)": {
        "url":     "https://api.openai.com/v1/chat/completions",
        "models":  [
            ("GPT-4o          (recommended)", "gpt-4o"),
            ("GPT-4o-mini     (cheap)",       "gpt-4o-mini"),
            ("GPT-4 Turbo",                   "gpt-4-turbo"),
            ("GPT-5",                         "gpt-5"),
        ],
        "build":   _openai_build,
        "extract": _openai_extract,
    },
    "OpenRouter (any model)": {
        # OpenRouter exposes hundreds of models behind an OpenAI-style
        # API. Lets the user use Gemini, Grok, Llama, etc. by entering
        # the slug.
        "url":     "https://openrouter.ai/api/v1/chat/completions",
        "models":  [
            ("Google Gemini 2.5 Pro",        "google/gemini-2.5-pro"),
            ("Meta Llama 4 405B",            "meta-llama/llama-4-405b"),
            ("xAI Grok 4",                   "x-ai/grok-4"),
            ("Mistral Large 2",              "mistralai/mistral-large-2"),
            ("Custom… (type a slug)",        "__custom__"),
        ],
        "build":   _openai_build,    # same wire format as OpenAI
        "extract": _openai_extract,
    },
}


# ── System prompt — defines the bot contract for the model ────────────

def _load_skeleton_guide() -> str:
    """Read BOT_SKELETON.md from the bundled install (or repo) so the
    model is fed the exact contract every APEX bot must follow."""
    meipass = getattr(sys, "_MEIPASS", None)
    candidates = []
    if meipass:
        candidates.append(Path(meipass) / "BOT_SKELETON.md")
    candidates.append(Path(__file__).parent.parent / "BOT_SKELETON.md")
    for p in candidates:
        if p.exists():
            try:
                return p.read_text(encoding="utf-8")
            except Exception:
                pass
    return ""


_SYSTEM_PROMPT = (
    "You are a senior algorithmic-trading engineer writing a single "
    "Python file that runs inside the APEX Trading Platform. The user "
    "describes the trading strategy and you produce ONLY the .py file "
    "contents — no markdown, no explanations, no triple-backtick "
    "fences. The file MUST follow the APEX bot contract described "
    "below. If the user's description is ambiguous, make reasonable "
    "defaults and add a comment near the top explaining your choices.\n\n"
    "═══ APEX BOT CONTRACT ═══\n\n"
    "{guide}\n\n"
    "═══ OUTPUT RULES ═══\n\n"
    "• Output ONLY raw Python source. No prose before or after.\n"
    "• The first non-blank line is a triple-quoted module docstring "
    "  summarising what the bot does.\n"
    "• Use `print(..., flush=True)` for all logs.\n"
    "• Read keys from os.environ — never hardcode.\n"
    "• Define a top-level main() function that contains the bot loop.\n"
    "• Single file, <1MB, no extra pip dependencies beyond the "
    "  packages already listed in the contract."
)


def _make_system_prompt() -> str:
    guide = _load_skeleton_guide() or (
        "(Bot skeleton guide unavailable. Required contract: a single "
        "main() function at module level, reads ALPACA_API_KEY / "
        "ALPACA_SECRET_KEY / ANTHROPIC_API_KEY from os.environ, uses "
        "print(..., flush=True) for logs.)")
    return _SYSTEM_PROMPT.format(guide=guide)


# ── Worker thread for the API call ───────────────────────────────────

class _GenerateWorker(QThread):
    done    = pyqtSignal(bool, str)     # ok, code-or-error-text
    progress = pyqtSignal(str)          # status line for the UI

    def __init__(self, *, provider: str, model: str, key: str,
                 prompt: str):
        super().__init__()
        self.provider = provider
        self.model    = model
        self.key      = key
        self.prompt   = prompt

    def run(self):
        cfg = PROVIDERS.get(self.provider)
        if not cfg:
            self.done.emit(False, f"Unknown provider: {self.provider}")
            return
        self.progress.emit(f"Calling {self.provider}…")
        headers, body = cfg["build"](
            self.prompt, _make_system_prompt(), self.key, self.model)
        url = cfg["url"]
        # Free-via-APEX: the build hook stuffs the real endpoint into a
        # custom header (server URL is user-configurable, not constant).
        if url == "__apex_dynamic__":
            url = headers.pop("X-Apex-Endpoint", "")
            if not url:
                self.done.emit(False,
                    "APEX server URL not configured. Sign in first.")
                return
        # V3.1.5 — Gemini's URL embeds the model in the path.
        if "{model}" in url:
            url = url.format(model=self.model)
        try:
            r = requests.post(url, headers=headers,
                              json=body, timeout=120)
        except requests.RequestException as e:
            self.done.emit(False, f"Network error: {e}")
            return
        if not r.ok:
            # FastAPI/APEX returns {"detail": "..."}; most other APIs
            # return {"error": {"message": "..."}}. Try both.
            msg = r.text
            try:
                body = r.json()
                msg  = (body.get("detail")
                        or body.get("error", {}).get("message")
                        or r.text)
            except Exception:
                pass
            self.done.emit(False, f"API error ({r.status_code}): {msg}")
            return
        try:
            text = cfg["extract"](r.json())
        except Exception as e:
            self.done.emit(False, f"Could not parse response: {e}")
            return
        if not text.strip():
            self.done.emit(False, "Model returned an empty response.")
            return
        # Strip accidental ```python fences just in case
        cleaned = text.strip()
        for fence in ("```python", "```py", "```"):
            if cleaned.startswith(fence):
                cleaned = cleaned[len(fence):].lstrip("\n")
                break
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].rstrip()
        self.done.emit(True, cleaned)


# ── Tab widget ────────────────────────────────────────────────────────

class MakeBotTab(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll = ScrollContent()
        root.addWidget(self.scroll)
        self._worker: Optional[_GenerateWorker] = None
        self._build()

    def refresh(self):  # called by ApexWindow on tab activate
        pass

    # ── Layout ────────────────────────────────────────────────────────

    def _build(self):
        s = self.scroll

        s.add(SectionHeader("MAKE YOUR OWN BOT", C["purple"]))
        intro = QLabel(
            "Describe a trading strategy in plain English and let an "
            "AI write the bot for you. The generated file follows the "
            "APEX bot contract (read it via Tools → Open skeleton "
            "guide) and can be saved to your local library or "
            "published to the public bot store on the APEX server."
        )
        intro.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        intro.setWordWrap(True)
        s.add(intro)

        # ── Mode toggle: Create new vs Improve existing ────────────
        mode_row = QHBoxLayout()
        mode_lbl = QLabel("Mode:")
        mode_lbl.setStyleSheet(f"color:{C['text']};font-size:11px;")
        self._mode_combo = QComboBox()
        self._mode_combo.addItem("✨  Create a new bot", "create")
        self._mode_combo.addItem("🔧  Improve an existing bot", "improve")
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        self._existing_lbl = QLabel("Existing bot:")
        self._existing_lbl.setStyleSheet(f"color:{C['text']};font-size:11px;")
        self._existing_combo = QComboBox()
        self._existing_combo.setMinimumWidth(220)
        self._refresh_existing_bots()

        mode_row.addWidget(mode_lbl)
        mode_row.addWidget(self._mode_combo)
        mode_row.addSpacing(20)
        mode_row.addWidget(self._existing_lbl)
        mode_row.addWidget(self._existing_combo)
        mode_row.addStretch()
        mw = QWidget(); mw.setLayout(mode_row)
        s.add(mw)
        self._existing_lbl.setVisible(False)
        self._existing_combo.setVisible(False)

        # ── Provider + Model + Key form ─────────────────────────────
        form = QFrame()
        form.setStyleSheet(
            f"background:{C['panel']};border:1px solid {C['border']};"
            f"border-radius:8px;")
        fg = QGridLayout(form)
        fg.setContentsMargins(16, 14, 16, 14)
        fg.setHorizontalSpacing(12)
        fg.setVerticalSpacing(10)

        # Provider
        fg.addWidget(self._lbl("AI provider"), 0, 0)
        self._provider_combo = QComboBox()
        for name in PROVIDERS:
            self._provider_combo.addItem(name)
        self._provider_combo.currentTextChanged.connect(self._on_provider_changed)
        fg.addWidget(self._provider_combo, 0, 1)

        # APEX-credits badge — purple → green gradient, only shown when
        # the "Via APEX" provider is picked. Shows live cost + balance
        # (the worker refreshes balance after each successful generation).
        self._free_badge = QLabel("◊ 10 / gen  ·  balance: —")
        self._free_badge.setStyleSheet(
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
            f"  stop:0 {C['purple']}, stop:1 #2eea88);"
            f"color:#0c1018;font-weight:800;font-size:10px;"
            "letter-spacing:1px;padding:4px 10px;border-radius:6px;"
        )
        self._free_badge.setVisible(False)
        fg.addWidget(self._free_badge, 0, 2)

        # Model
        fg.addWidget(self._lbl("Model"), 1, 0)
        self._model_combo = QComboBox()
        fg.addWidget(self._model_combo, 1, 1)

        # Custom model slug (only for OpenRouter "__custom__")
        self._custom_model_edit = QLineEdit()
        self._custom_model_edit.setPlaceholderText(
            "e.g. anthropic/claude-3.5-sonnet")
        self._custom_model_edit.setVisible(False)
        self._custom_model_edit.setStyleSheet(self._input_css())
        fg.addWidget(self._custom_model_edit, 1, 2)

        # API key
        fg.addWidget(self._lbl("API key"), 2, 0)
        self._key_edit = QLineEdit()
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_edit.setStyleSheet(self._input_css())
        # Pre-fill Anthropic key from the user's .env if we have one.
        try:
            cur = D.read_env_keys()
            self._key_edit.setText(cur.get("ANTHROPIC_API_KEY", ""))
        except Exception:
            pass
        fg.addWidget(self._key_edit, 2, 1)
        self._show_key = QCheckBox("show")
        self._show_key.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        self._show_key.toggled.connect(lambda on:
            self._key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on
                else QLineEdit.EchoMode.Password))
        fg.addWidget(self._show_key, 2, 2)

        # Bot name
        fg.addWidget(self._lbl("Bot name"), 3, 0)
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. spy-momentum")
        self._name_edit.setStyleSheet(self._input_css())
        fg.addWidget(self._name_edit, 3, 1, 1, 2)

        s.add(form)

        # ── Description ───────────────────────────────────────────
        s.add(SectionHeader("WHAT SHOULD THE BOT DO?", C["yellow"]))
        desc_help = QLabel(
            "Plain English. Mention: when to buy / sell, position "
            "sizing, stop-loss / take-profit, any indicators or "
            "external signals. The clearer the spec, the better the "
            "generated code.")
        desc_help.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        desc_help.setWordWrap(True)
        s.add(desc_help)

        self._desc = QPlainTextEdit()
        self._desc.setStyleSheet(
            f"background:{C['panel2']};color:{C['text']};"
            f"border:1px solid {C['border']};border-radius:6px;"
            f"padding:10px;font-family:'JetBrains Mono';font-size:11px;")
        self._desc.setMinimumHeight(160)
        self._desc.setPlaceholderText(
            "Buy SPY at market open if the S&P futures gapped up "
            "more than 0.3 %. Sell at 15:45 ET. Use 50 % of "
            "available cash. Skip if VIX > 25.")
        s.add(self._desc)

        # ── Generate button + status ──────────────────────────────
        btn_row = QHBoxLayout()
        self._gen_btn = QPushButton("✨  Generate bot")
        self._gen_btn.setObjectName("addBotBtn")
        self._gen_btn.clicked.connect(self._on_generate)
        btn_row.addWidget(self._gen_btn)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        btn_row.addWidget(self._status)
        btn_row.addStretch()
        bw = QWidget()
        bw.setLayout(btn_row)
        s.add(bw)

        # ── Generated code viewer ─────────────────────────────────
        s.add(SectionHeader("GENERATED CODE", C["green"]))
        self._code = QPlainTextEdit()
        self._code.setStyleSheet(
            f"background:{C['panel2']};color:{C['text']};"
            f"border:1px solid {C['border']};border-radius:6px;"
            f"padding:10px;font-family:'JetBrains Mono';font-size:11px;")
        self._code.setMinimumHeight(240)
        self._code.setPlaceholderText(
            "Click Generate above. The bot's Python source will "
            "appear here. You can hand-edit it before saving.")
        s.add(self._code)

        # ── Save / publish buttons ────────────────────────────────
        save_row = QHBoxLayout()
        self._save_btn = QPushButton("💾  Save to my library")
        self._save_btn.setObjectName("toolBtn")
        self._save_btn.clicked.connect(self._on_save_local)
        self._pub_btn  = QPushButton("☁  Save & publish to APEX store")
        self._pub_btn.setObjectName("toolBtn")
        self._pub_btn.clicked.connect(self._on_save_and_publish)
        self._save_msg = QLabel("")
        self._save_msg.setStyleSheet(f"color:{C['green']};font-size:11px;")
        save_row.addWidget(self._save_btn)
        save_row.addWidget(self._pub_btn)
        save_row.addWidget(self._save_msg)
        save_row.addStretch()
        sw = QWidget()
        sw.setLayout(save_row)
        s.add(sw)
        s.add_stretch()

        # Populate the model dropdown for the initial provider
        self._on_provider_changed(self._provider_combo.currentText())

    # ── Small helpers ────────────────────────────────────────────────

    def _lbl(self, text: str) -> QLabel:
        w = QLabel(text)
        w.setStyleSheet(f"color:{C['text']};font-size:11px;")
        return w

    def _input_css(self) -> str:
        return (
            f"background:{C['panel2']};color:{C['text']};"
            f"border:1px solid {C['border']};border-radius:5px;"
            f"padding:6px 10px;font-family:'JetBrains Mono';font-size:11px;"
        )

    # ── Provider-driven model list ────────────────────────────────────

    def _on_provider_changed(self, name: str):
        self._model_combo.clear()
        cfg = PROVIDERS.get(name) or {}
        for label, slug in cfg.get("models", []):
            self._model_combo.addItem(label, slug)
        # Show the custom-slug field when the OpenRouter "Custom…"
        # option is selected
        self._model_combo.currentIndexChanged.connect(self._refresh_custom_visibility)
        self._refresh_custom_visibility()
        # V3.1.5 — three flavours of "free":
        #   • APEX-credits  (cfg.credits=True)  → no key needed, badge
        #     shows current credit balance, charged 10 ◊ per gen
        #   • Provider free tier (Gemini / Groq) → user pastes their own
        #     free-tier key, badge says "FREE TIER", call costs nothing
        #   • Paid (Anthropic / OpenAI / OpenRouter) → user pastes a key,
        #     no badge
        is_free     = bool(cfg.get("free"))
        is_credits  = bool(cfg.get("credits"))
        self._free_badge.setVisible(is_free)
        if hasattr(self, "_key_edit"):
            self._key_edit.setEnabled(not is_credits)
            if is_credits:
                self._key_edit.setPlaceholderText(
                    "Not required — generation costs APEX credits")
            elif name.startswith("🎁  Google"):
                self._key_edit.setPlaceholderText(
                    "Get a free key at aistudio.google.com/apikey")
            elif name.startswith("🎁  Groq"):
                self._key_edit.setPlaceholderText(
                    "Get a free key at console.groq.com/keys")
            else:
                self._key_edit.setPlaceholderText("")
        if is_credits:
            self._refresh_credit_balance()
        elif is_free:
            # Provider free tier — show a static "FREE TIER" badge
            self._free_badge.setText("FREE TIER  ✨")

    def _refresh_credit_balance(self):
        """Fetch /api/makebot/price so the badge shows live cost + balance."""
        from PyQt6.QtCore import QThread as _QT, pyqtSignal as _Sig
        from ui.login import load_auth, load_server_url
        tok = (load_auth() or {}).get("token") or ""
        url = f"{load_server_url()}/api/makebot/price"

        class _BalWorker(_QT):
            done = _Sig(int, int)   # cost, balance
            def run(self_):
                import requests
                try:
                    r = requests.get(url,
                        headers={"Authorization": f"Bearer {tok}"} if tok else None,
                        timeout=6)
                    if r.ok:
                        d = r.json()
                        self_.done.emit(int(d.get("cost", 10)),
                                        int(d.get("balance", 0)))
                    else:
                        self_.done.emit(10, -1)
                except Exception:
                    self_.done.emit(10, -1)

        def _on(cost, balance):
            if balance < 0:
                self._free_badge.setText(f"◊ {cost} / gen  ·  balance: ?")
            else:
                self._free_badge.setText(
                    f"◊ {cost} / gen  ·  balance: {balance:,}")

        self._bal_worker = _BalWorker()
        self._bal_worker.done.connect(_on)
        self._bal_worker.start()

    def _on_mode_changed(self, _idx: int):
        mode = self._mode_combo.currentData()
        is_improve = (mode == "improve")
        self._existing_lbl.setVisible(is_improve)
        self._existing_combo.setVisible(is_improve)
        if is_improve:
            self._refresh_existing_bots()
            self._desc.setPlaceholderText(
                "Describe what you want IMPROVED. E.g. 'Add a 2 % "
                "trailing stop. Skip Mondays. Use Haiku instead of "
                "Sonnet to cut costs.'")
        else:
            self._desc.setPlaceholderText(
                "Buy SPY at market open if the S&P futures gapped up "
                "more than 0.3 %. Sell at 15:45 ET. Use 50 % of "
                "available cash. Skip if VIX > 25.")

    def _refresh_existing_bots(self):
        """Populate the 'existing bot' dropdown from local custom bots
        + built-in bots, so users can hand any of them to the AI for
        improvement."""
        if not hasattr(self, "_existing_combo"):
            return
        self._existing_combo.clear()
        # Built-ins (read from the bundled .py files via D.BOT_SCRIPTS)
        try:
            for side, path in D.BOT_SCRIPTS.items():
                self._existing_combo.addItem(
                    f"⟦built-in⟧  {side}", str(path))
        except Exception:
            pass
        # Custom user bots
        try:
            reg = D.load_settings().get("bot_registry", {})
            for c in reg.get("custom", []):
                self._existing_combo.addItem(
                    f"⟦custom⟧  {c.get('label', c['id'])}",
                    c.get("script", ""))
        except Exception:
            pass

    def _refresh_custom_visibility(self, *_):
        slug = self._model_combo.currentData()
        self._custom_model_edit.setVisible(slug == "__custom__")

    def _resolved_model_slug(self) -> str:
        slug = self._model_combo.currentData()
        if slug == "__custom__":
            return self._custom_model_edit.text().strip()
        return slug or ""

    # ── Generate action ──────────────────────────────────────────────

    def _on_generate(self):
        prov = self._provider_combo.currentText()
        cfg = PROVIDERS.get(prov) or {}
        is_credits = bool(cfg.get("credits"))    # APEX-credits flow
        is_free    = bool(cfg.get("free"))       # APEX OR free-tier provider
        model = self._resolved_model_slug()
        key   = self._key_edit.text().strip()
        prompt = self._desc.toPlainText().strip()

        if not model:
            QMessageBox.warning(self, "Missing model",
                "Pick (or type) a model first.")
            return
        # APEX-credits: no key needed at all.
        # Provider free-tier (Gemini / Groq): user still needs their
        # own (free) API key — paste it after signing up.
        if not is_credits and not key:
            QMessageBox.warning(self, "Missing key",
                "Paste your API key for the chosen provider. For the "
                "free tiers, grab one from the link in the key-field "
                "placeholder — it takes ~1 minute.")
            return
        if len(prompt) < 20:
            QMessageBox.warning(self, "Description too short",
                "Describe the bot in at least a couple of sentences "
                "so the model has something to work with.")
            return

        # If we're in "Improve" mode, prepend the chosen bot's source
        # so the model has full context.
        if self._mode_combo.currentData() == "improve":
            src_path = self._existing_combo.currentData()
            if not src_path or not Path(src_path).exists():
                QMessageBox.warning(self, "Pick an existing bot",
                    "Choose a bot from the dropdown to improve.")
                return
            try:
                existing = Path(src_path).read_text(encoding="utf-8")
            except Exception as e:
                QMessageBox.warning(self, "Read failed",
                    f"Could not read {src_path}: {e}")
                return
            prompt = (
                "Here is the existing bot's full source. Improve it per "
                "the instructions below, but KEEP the APEX bot contract "
                "(main() entry-point, env-var keys, print-flush logs, "
                "single file).\n\n"
                "=== EXISTING SOURCE ===\n"
                f"{existing}\n"
                "=== END EXISTING SOURCE ===\n\n"
                "=== REQUESTED CHANGES ===\n"
                f"{prompt}\n"
                "=== END REQUESTED CHANGES ===\n\n"
                "Output the full improved file. Do not output prose, "
                "markdown fences, or partial diffs — only the complete "
                "new Python source."
            )

        self._gen_btn.setEnabled(False)
        self._gen_btn.setText("Generating…")
        self._status.setText("Calling the model — this can take 20-40 s.")
        self._status.setStyleSheet(f"color:{C['muted']};font-size:11px;")

        self._worker = _GenerateWorker(
            provider=prov, model=model, key=key, prompt=prompt)
        self._worker.progress.connect(
            lambda msg: self._status.setText(msg))
        self._worker.done.connect(self._on_generated)
        self._worker.start()

    def _on_generated(self, ok: bool, payload: str):
        self._gen_btn.setEnabled(True)
        self._gen_btn.setText("✨  Generate bot")
        if not ok:
            self._status.setText(payload)
            self._status.setStyleSheet(f"color:{C['red']};font-size:11px;")
            return
        self._status.setText("Done — review the code below.")
        self._status.setStyleSheet(f"color:{C['green']};font-size:11px;")
        self._code.setPlainText(payload)
        # V3.1.4 — if we used the APEX-credits provider, refresh the
        # badge so the user sees the updated balance immediately.
        prov = self._provider_combo.currentText()
        cfg  = PROVIDERS.get(prov) or {}
        if cfg.get("credits"):
            self._refresh_credit_balance()

    # ── Save / publish ───────────────────────────────────────────────

    def _on_save_local(self):
        code = self._code.toPlainText().strip()
        name = self._name_edit.text().strip()
        if not code:
            self._toast("Nothing to save — generate or paste code first.", err=True)
            return
        if not name:
            self._toast("Give the bot a name first.", err=True)
            return
        # Slug-ify
        slug = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                       for ch in name.strip().lower())
        bots_dir = DATA_DIR / "bots"
        bots_dir.mkdir(exist_ok=True)
        dest = bots_dir / f"{slug}.py"
        dest.write_text(code, encoding="utf-8")

        # Register in the user's bot registry so MORE BOTS picks it up
        try:
            s = D.load_settings()
            reg = s.get("bot_registry",
                        {"active": [], "silenced": [], "custom": []})
            existing = [c["id"] for c in reg.get("custom", [])]
            if slug not in existing:
                reg.setdefault("custom", []).append({
                    "id":     slug,
                    "label":  name,
                    "script": str(dest),
                    "color":  C["purple"],
                })
                s["bot_registry"] = reg
                with open(D.SETTINGS_FILE, "w", encoding="utf-8") as f:
                    json.dump(s, f, indent=2)
        except Exception as e:
            print(f"[make-bot] registry update failed: {e}")

        self._toast(f"Saved {slug}.py to your library. "
                    f"See it in MORE BOTS → AVAILABLE TO ADD.")

    def _on_save_and_publish(self):
        """Save locally first, then ask the parent ApexWindow to
        publish using the existing marketplace flow (which prompts
        for tags / description and authenticates against the server)."""
        self._on_save_local()
        code = self._code.toPlainText().strip()
        if not code:
            return
        name = self._name_edit.text().strip()
        slug = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                       for ch in name.strip().lower())
        path = DATA_DIR / "bots" / f"{slug}.py"
        if not path.exists():
            return
        # Walk up to the ApexWindow and use MoreBotsTab._publish_bot_with_path
        win = self.window()
        more = getattr(win, "more_bots_tab", None)
        if more and hasattr(more, "_publish_bot_with_path"):
            more._publish_bot_with_path(str(path))
        else:
            self._toast("Saved locally. Open MORE BOTS to publish manually.",
                        err=False)

    # ── Toast helper ─────────────────────────────────────────────────

    def _toast(self, msg: str, err: bool = False):
        color = C["red"] if err else C["green"]
        self._save_msg.setText(msg)
        self._save_msg.setStyleSheet(f"color:{color};font-size:11px;")
        QTimer.singleShot(6000, lambda: self._save_msg.setText(""))
