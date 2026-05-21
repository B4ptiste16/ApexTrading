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


PROVIDERS = {
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
        try:
            r = requests.post(cfg["url"], headers=headers,
                              json=body, timeout=120)
        except requests.RequestException as e:
            self.done.emit(False, f"Network error: {e}")
            return
        if not r.ok:
            try:
                msg = r.json().get("error", {}).get("message") or r.text
            except Exception:
                msg = r.text
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
        model = self._resolved_model_slug()
        key   = self._key_edit.text().strip()
        prompt = self._desc.toPlainText().strip()

        if not model:
            QMessageBox.warning(self, "Missing model",
                "Pick (or type) a model first.")
            return
        if not key:
            QMessageBox.warning(self, "Missing key",
                "Paste your API key for the chosen provider.")
            return
        if len(prompt) < 20:
            QMessageBox.warning(self, "Description too short",
                "Describe the bot in at least a couple of sentences "
                "so the model has something to work with.")
            return

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
