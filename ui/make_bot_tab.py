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
    QCheckBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from core      import data as D
from core.paths import DATA_DIR, ACCOUNT_DIR   # V4.6.101 — DATA_DIR=login root,
# ACCOUNT_DIR=this account's data (bots/). Auth reads use DATA_DIR; bots use ACCOUNT_DIR.
from ui.styles import COLORS
from ui.widgets import ScrollContent, SectionHeader, NoScrollComboBox

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
    """The 'Via APEX' option doesn't need an API key — it routes
    through the APEX server which uses a pooled Anthropic key.
    V4.6.16: passes `model` so the server can apply per-model pricing
    from MAKEBOT_MODEL_COSTS instead of a flat 10-credit charge."""
    from ui.login import load_auth, load_server_url
    tok = (load_auth() or {}).get("token") or ""
    return (
        {"Authorization": f"Bearer {tok}",
         "Content-Type":  "application/json",
         "X-Apex-Endpoint": f"{load_server_url()}/api/makebot/generate"},
        {"prompt": prompt, "system": system, "model": model},
    )


def _apex_free_extract(j: dict) -> str:
    return j.get("text", "") or ""


PROVIDERS = {
    "✨  Via APEX  (uses your APEX credits)": {
        # Sentinel URL — the worker replaces it with the X-Apex-Endpoint
        # header value at request time (so the server URL is dynamic).
        "url":     "__apex_dynamic__",
        "models":  [
            ("Claude Haiku  (BAPTOU-hosted)", "baptou-haiku"),
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
            ("Claude Fable 5     (newest, top tier)", "claude-fable-5"),
            ("Claude Opus 4.8    (top tier)",         "claude-opus-4-8"),
            ("Claude Sonnet 4.6  (balanced)",         "claude-sonnet-4-6"),
            ("Claude Haiku 4.5   (fast & cheap)",     "claude-haiku-4-5-20251001"),
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


_TRADE_TEMPLATE = '''"""
APEX-BOT-META
name:               {{name}}
description:        {{one-line pitch}}
method:             {{plain-english strategy summary}}
ai_used:            {{provider}}
compatible_models:  {{provider, others}}
asset_type:         {{stocks|crypto|etfs|futures|options}}
brokers:            {{alpaca, ibkr}}
universe:           {{public universe name, or (ai-selected)}}
requirements:       {{leave EMPTY — never list core/pandas/numpy/framework}}
"""

# YOU MAY EDIT ONLY THE decide(...) FUNCTION BELOW.
# The boilerplate above and the BotRunner call at the bottom are
# fixed; changing them breaks the runtime contract.

import os
import pandas as pd
import numpy as np
from core.bot_framework import BotRunner


def decide(symbol: str,
           bars: pd.DataFrame,
           position: dict,
           account: dict) -> dict:
    """Return ONE decision for this symbol on this tick.

    Inputs (provided by the framework — do NOT fetch your own data):
      symbol    str            e.g. "BTC-USD"  (yfinance format)
      bars      pd.DataFrame   OHLCV, single-level columns, ascending.
                               Columns: Open, High, Low, Close, Volume
      position  dict           {"qty": float, "side": "long"|"short"|"flat",
                                "avg_entry": float}
      account   dict           {"cash": float, "equity": float,
                                "buying_power": float}

    Output schema — return EXACTLY one of these shapes:
      {"action": "BUY",   "qty": int|float, "reason": str}  # open/add long
      {"action": "SELL",  "qty": int|float, "reason": str}  # close long
      {"action": "SHORT", "qty": int|float, "reason": str}  # open/add short
      {"action": "COVER", "qty": int|float, "reason": str}  # close short
      {"action": "HOLD",                    "reason": str}

    OPTIONAL — add "confidence": <0.0..1.0> to any BUY/SELL/SHORT/COVER. When
    present, the framework SKIPS the trade if confidence is below the user's
    "Minimum confidence to trade" setting. This makes the confidence slider
    work for your bot. Base it on how strong your signal is (e.g. how far
    price is from a moving average, regression R², etc.).

    Risk notes:
      • account["buying_power"] caps how big BUY/SHORT can be.
      • Don't open SHORT when position["side"] == "long"; SELL first.
      • Crypto bots: SHORT means SELL — Alpaca does not allow shorting
        crypto, so for asset_type=crypto, only emit BUY / SELL / HOLD.
      • Quantity is in WHOLE units for stocks (e.g. 10 shares) and
        FRACTIONAL allowed for crypto (e.g. 0.05 BTC).
    """
    # ── STRATEGY: customise THIS BLOCK ONLY ──────────────────────
    close = bars["Close"].squeeze()           # clean Series
    sma_short = close.rolling(20).mean().iloc[-1]
    sma_long  = close.rolling(50).mean().iloc[-1]
    price     = float(close.iloc[-1])

    if pd.isna(sma_short) or pd.isna(sma_long):
        return {"action": "HOLD", "reason": "not enough history"}

    if position["side"] == "flat" and sma_short > sma_long:
        # Size to ~5% of buying power, at least 1 unit
        qty = max(1, int(account["buying_power"] * 0.05 / price))
        return {"action": "BUY",
                "qty":    qty,
                "reason": f"sma20 ({sma_short:.2f}) > sma50 ({sma_long:.2f})"}
    if position["side"] == "long" and sma_short < sma_long:
        return {"action": "SELL",
                "qty":    position["qty"],
                "reason": "trend reversal, exit long"}
    return {"action": "HOLD",
            "reason": f"sma20={sma_short:.2f} sma50={sma_long:.2f} flat-or-trending"}
    # ── END STRATEGY BLOCK ───────────────────────────────────────


if __name__ == "__main__":
    BotRunner(
        asset_type="stocks",
        default_symbols=["AAPL", "NVDA", "MSFT"],
        universe_path=os.environ.get("APEX_BOT_UNIVERSE",
                                     "longbot_universe.txt"),
        tick_seconds=300,
        bar_period="6mo",
        bar_interval="1d",
        name="custom-bot",
    ).run(decide)
'''


_SYSTEM_PROMPT_TRADE = (
    "You are a senior algorithmic-trading engineer customising a "
    "Python trading bot for the APEX Trading Platform. The user "
    "describes the strategy and you produce ONLY the .py file "
    "contents — no markdown, no explanations, no triple-backtick "
    "fences.\n\n"
    "**Critical: you MUST start from the template below and modify ONLY "
    "two things**:\n"
    "  1. The {{placeholders}} inside the APEX-BOT-META docstring (name, "
    "description, method, ai_used, compatible_models, asset_type, "
    "universe, requirements). Replace EVERY {{placeholder}} with a real value.\n"
    "  2. The body of the `decide(...)` function between the\n"
    "     `# ── STRATEGY: customise THIS BLOCK ONLY ──` and\n"
    "     `# ── END STRATEGY BLOCK ──` markers.\n"
    "  3. The `BotRunner(...)` kwargs at the bottom (asset_type, "
    "default_symbols, universe_path, tick_seconds, bar_period, "
    "bar_interval, name) to match the strategy.\n\n"
    "**Do NOT** add `if __name__ == '__main__'` logic anywhere else. "
    "**Do NOT** import alpaca, yfinance, or any market-data library — "
    "the framework hands you clean `bars` and a `position` dict. "
    "**Do NOT** define a `main()` function — `BotRunner.run(decide)` "
    "is the entry point.\n\n"
    "═══ REFERENCE TEMPLATE (start from this) ═══\n\n"
    "{template}\n\n"
    "═══ APEX BOT CONTRACT (background) ═══\n\n"
    "{guide}\n\n"
    "═══ DECISION OUTPUT SCHEMA ═══\n\n"
    "`decide` MUST return exactly one of:\n"
    '  {{"action": "BUY",   "qty": <number>, "reason": "<text>"}}\n'
    '  {{"action": "SELL",  "qty": <number>, "reason": "<text>"}}\n'
    '  {{"action": "SHORT", "qty": <number>, "reason": "<text>"}}\n'
    '  {{"action": "COVER", "qty": <number>, "reason": "<text>"}}\n'
    '  {{"action": "HOLD",                  "reason": "<text>"}}\n'
    "Anything else (None, raw numbers, lists) is rejected by the runner.\n\n"
    "═══ FILL-IN RULES ═══\n\n"
    "• ai_used MUST be the literal provider that is generating this "
    "bot (e.g. 'groq', 'anthropic', 'openai', 'gemini').\n"
    "• TICKER UNIVERSE: if the user's request begins with a "
    "'TICKER UNIVERSE (mandatory): ...' block, you MUST set "
    "`default_symbols` in the BotRunner call to EXACTLY that list of "
    "tickers (no additions, no omissions) and set the META `universe:` "
    "field to the universe name given in that block. If NO such block "
    "is present, choose a small, sensible `default_symbols` list that "
    "fits the strategy and set `universe:` to "
    "'(ai-selected)'.\n"
    "• The META `description:` (one punchy line) and `method:` (2-3 "
    "sentences in plain English) are shown to the user on the bot's "
    "card and in the marketplace — write them for a human, not as "
    "placeholders. Make them accurate to the strategy you wrote.\n"
    "• `requirements:` lists ONLY third-party PyPI packages that are NOT "
    "already bundled. It must be EMPTY for almost every bot. NEVER put "
    "in requirements: the APEX framework or any stdlib/bundled lib — "
    "specifically NEVER list `core` (that's APEX's own package — "
    "`from core.bot_framework import BotRunner` is always importable; "
    "listing it makes the server run `pip install core`, which fails). "
    "Also never list pandas, numpy, requests, ib_async, alpaca, "
    "anthropic, openai, yfinance, matplotlib (all bundled). Only a "
    "genuinely extra package (e.g. ta) belongs here — and import it "
    "INSIDE decide(), never at module level.\n"
    "• asset_type='crypto' bots must NEVER emit SHORT or COVER — "
    "Alpaca disallows shorting crypto. Use BUY / SELL only.\n"
    "• Size qty defensively: `int(account['buying_power'] * pct / price)` "
    "with a `max(1, ...)` floor for stocks.\n"
    "• Never hardcode credentials. The framework handles env vars.\n"
    "• **Text-only AI is fully supported.** If the user picks Llama "
    "(Groq), GPT-4o-mini, Haiku without vision, or any other text "
    "model, build features as NUMBERS (SMA, RSI, MACD, returns, "
    "volume ratios) and ask the LLM for a JSON decision. NEVER ask "
    "a text-only model to look at a chart image — there is no image. "
    "Available env vars for HTTP calls: GROQ_API_KEY, ANTHROPIC_API_KEY, "
    "OPENAI_API_KEY, GOOGLE_AI_API_KEY. Use `requests.post(...)` and "
    "request `response_format={{'type':'json_object'}}` when the provider "
    "supports it (Groq + OpenAI do).\n"
    "• AI-driven bots: ALWAYS wrap the LLM call in try/except and fall "
    "back to HOLD on any error. A flaky API call must never crash the bot.\n\n"
    "═══ STRATEGY VIABILITY RULES (READ CAREFULLY) ═══\n\n"
    "An AI-generated strategy must produce ACTUAL trades, not be stuck "
    "in a no-op loop. Common dead-end patterns to AVOID:\n\n"
    "❌ Comparing a regression `prediction[-1]` against a fixed "
    "  multiple of a long-term MEAN (e.g. mean*1.1 / mean*0.9). The "
    "  regression line and the mean are on completely different scales — "
    "  the result is deterministic per regime (always BUY in bear / "
    "  always SHORT in bull) and skipped by validators.\n\n"
    "✅ Compare predictions to CURRENT PRICE, not to a fixed multiple "
    "  of historical mean. E.g.:\n"
    "      slope, trend_today = fit_regression(close)\n"
    "      if slope > 0 and price < trend_today * 0.98: BUY\n"
    "      elif position['side']=='long' and slope < 0: SELL\n\n"
    "❌ Always emitting the SAME action regardless of market state. "
    "  If your strategy returns BUY (or SHORT, or HOLD) for every "
    "  symbol on every tick, it's broken.\n\n"
    "✅ Have at least ONE clear entry condition AND ONE clear exit "
    "  condition. Print the actual feature values + the decision rule "
    "  so log readers can verify signals are firing.\n\n"
    "❌ For asset_type='crypto', generating SHORT or COVER actions. "
    "  Alpaca rejects them with a validator skip — your bot will run "
    "  for hours doing nothing if your only sell signal is SHORT.\n\n"
    "✅ For crypto: use BUY (to open a long), SELL (to close), HOLD. "
    "  Always have a non-SHORT path to take action.\n\n"
    "Before emitting your code, mentally walk through one tick: given "
    "today's actual BTC price (~$110k as of late 2025 / early 2026), "
    "do your thresholds produce a real trade signal, or do they "
    "compare numbers that can never cross? If they can never cross, "
    "rewrite the logic.\n\n"
    "═══ RUN-READY CHECKLIST (MUST ALL PASS) ═══\n\n"
    "The file you output MUST run on the very first try with no edits. "
    "Verify EVERY item before responding:\n"
    "1. It `compile()`s — balanced quotes/parens, no half-finished "
    "  docstrings, no markdown fences, no prose. Output ONLY the .py.\n"
    "2. Top-level imports are limited to: os, math, statistics, pandas "
    "  as pd, numpy as np, and `from core.bot_framework import BotRunner`. "
    "  ANY other package goes in requirements: AND is imported INSIDE "
    "  decide() in a try/except (so a missing dep can't crash startup).\n"
    "3. `decide()` ALWAYS returns a valid dict on every path — including "
    "  the very first ticks. Guard short history: "
    "  `if len(bars) < N or pd.isna(x): return {{'action':'HOLD',"
    "  'reason':'warming up'}}`.\n"
    "4. Quantities are valid: BUY/SHORT qty is a POSITIVE number "
    "  (`max(1, int(...))` for stocks; a positive float like 0.01 for "
    "  crypto). SELL/COVER use `position['qty']`. Never return qty<=0, "
    "  None, or NaN.\n"
    "5. Never size bigger than the wallet: cap notional with "
    "  `account['buying_power']`. Read prices from `bars['Close']` only — "
    "  do NOT fetch your own data or call a broker SDK.\n"
    "6. `default_symbols` matches asset_type: yfinance form. Stocks/ETFs "
    "  → 'AAPL'; crypto → 'BTC-USD'. If a TICKER UNIVERSE block was "
    "  given, default_symbols must equal that exact list.\n"
    "7. The APEX-BOT-META block has EVERY field filled with a real value "
    "  (no leftover {{placeholders}}), including a human-readable "
    "  description + method and the correct brokers list.\n"
    "8. You did NOT rename or re-signature `decide(symbol, bars, position, "
    "  account)` and did NOT add a `main()` — `BotRunner(...).run(decide)` "
    "  is the only entry point."
)


_SYSTEM_PROMPT_UNIVERSE = (
    "You are a senior algorithmic-trading engineer writing a single "
    "Python file that runs inside the APEX Trading Platform as a "
    "UNIVERSE generator (NOT a trading bot). The user describes how "
    "they want their universe of tickers filtered/scored, and your "
    "file's job is to OVERWRITE a single *_universe.txt file with the "
    "selected tickers, one per line, with optional `# note` annotations.\n\n"
    "═══ APEX BOT CONTRACT ═══\n\n"
    "{guide}\n\n"
    "═══ UNIVERSE-BOT RULES ═══\n\n"
    "• Output ONLY raw Python source. No prose before or after.\n"
    "• FIRST docstring is the APEX-BOT-META block. Required fields:\n"
    "  name, description, method, ai_used, compatible_models,\n"
    "  asset_type, universe, requirements. The `universe:` field is\n"
    "  the filename this script REWRITES (e.g. crypto_universe.txt).\n"
    "• Do NOT submit orders. Do NOT import alpaca.trading.client.\n"
    "  Universe bots only READ market data and WRITE the .txt file.\n"
    "• The .txt file lives under APEX_DATA_DIR (read from os.environ)\n"
    "  with the filename from META.universe. One ticker per line,\n"
    "  inline `# note` comments allowed.\n"
    "• Run once and exit cleanly (return from main()) — APEX schedules\n"
    "  the next run; the bot is not a daemon loop.\n"
    "• Use print(..., flush=True) for status logs.\n\n"
    "═══ CRITICAL: BUNDLED-LIBS ONLY FOR UNIVERSE BOTS ═══\n\n"
    "Universe bots execute LOCALLY in the user's frozen APEX install. "
    "The frozen Python interpreter CANNOT pip-install new packages. "
    "If you import a library that isn't bundled, the bot crashes with "
    "ModuleNotFoundError and produces NO universe file.\n\n"
    "Allowed bundled imports (everything else WILL crash):\n"
    "  requests       — HTTP, perfect for REST APIs (CoinGecko,\n"
    "                   Alpaca /v2/assets, Polygon, Finnhub, IEX, etc.)\n"
    "  yfinance       — historical bars + 24h volume for any ticker.\n"
    "                   Crypto symbols use BTC-USD format.\n"
    "  pandas, numpy  — data wrangling, sorting, scoring\n"
    "  alpaca-py      — fine for READING (assets, bars, account); never\n"
    "                   call .submit_order() in a universe bot.\n"
    "  json, os, re, time, datetime, math, statistics — stdlib only.\n\n"
    "FORBIDDEN imports for universe bots (will ModuleNotFoundError):\n"
    "  ccxt, talib, ta, scikit-learn, sklearn, openai, anthropic,\n"
    "  google.generativeai, beautifulsoup4, lxml, scrapy, selenium\n\n"
    "If the user asks for a CRYPTO universe, use Alpaca's\n"
    "/v2/assets?asset_class=crypto endpoint or CoinGecko's free public\n"
    "API via requests — both work with bundled libs. Do NOT use ccxt.\n\n"
    "If the user asks for fundamentals / earnings, use yfinance's\n"
    "Ticker.info / Ticker.financials — bundled, works offline-friendly.\n\n"
    "Always set requirements: (empty after the colon) for universe bots.\n"
    "The frozen interpreter can't honor declared requirements anyway.\n\n"
    "═══ CREDENTIAL LOADING (universe bots) ═══\n\n"
    "APEX's child processes do NOT inherit .env values automatically. "
    "If your universe bot needs Alpaca / API keys, load them yourself:\n\n"
    "    import os\n"
    "    try:\n"
    "        from dotenv import load_dotenv\n"
    "        load_dotenv(os.path.join(\n"
    "            os.environ.get('APEX_DATA_DIR', ''), '.env'))\n"
    "    except Exception:\n"
    "        pass\n\n"
    "Then read os.environ['ALPACA_API_KEY'] etc. as normal. Without this "
    "load_dotenv call your bot will see empty env vars and hit 401 / "
    "auth errors on every API call.\n\n"
    "═══ AUTH-FAILURE FALLBACK ═══\n\n"
    "Even with keys, an authenticated API endpoint can refuse the "
    "request (rate-limited, IP-blocked, scope error, paper-vs-live "
    "mismatch). Your bot MUST have a public-API fallback so a single "
    "auth failure doesn't leave the universe file unchanged. Examples:\n"
    "  - For crypto universes: try Alpaca /v2/assets first, fall back "
    "    to CoinGecko /coins/markets (no auth, free tier 50 req/min).\n"
    "  - For US-equity universes: try Alpaca, fall back to Polygon's "
    "    free /v3/reference/tickers or Yahoo via yfinance (which is\n"
    "    bundled).\n"
    "Pattern:\n"
    "    syms = try_authenticated_source()  # returns None on 401\n"
    "    if not syms:\n"
    "        syms = public_fallback_source()  # no auth\n"
    "    if not syms:\n"
    "        print('no source available — keeping existing universe')\n"
    "        return  # do NOT overwrite the .txt with garbage"
)


def _brokers_rule(brokers: list[str]) -> str:
    """V4.6.48 — broker-AGNOSTIC generation. Instead of emitting Alpaca- or
    IBKR-specific SDK code (which locked a bot to one broker), every bot is
    generated against the APEX framework's decide() contract and runs on
    WHICHEVER broker is active. The checkboxes only stamp META.brokers so the
    app knows where the bot is meant to run."""
    bl = [b for b in (brokers or []) if b in ("alpaca", "ibkr")] or ["alpaca", "ibkr"]
    blist = ", ".join(bl)
    ibkr_note = (
        "• This bot targets IBKR too. IBKR lists only major cryptos "
        "(BTC/ETH/LTC/BCH); the framework auto-skips coins IBKR can't trade, "
        "so for asset_type=crypto keep the universe to majors.\n"
        if "ibkr" in bl else "")
    return (
        "\n\n═══ BROKER COMPATIBILITY — KEEP IT BROKER-AGNOSTIC ═══\n\n"
        f"This bot must run on BOTH brokers it targets ({blist}) through the "
        "APEX framework. It MUST be broker-agnostic:\n"
        "• Do NOT import alpaca / alpaca-py / ib_insync / ib_async. Do NOT read "
        "ALPACA_API_KEY and do NOT connect to TWS/Gateway. The BotRunner "
        "framework owns ALL broker connection, auth and order routing.\n"
        "• Your ONLY output is the decide() decision dict; the framework "
        "executes it on whichever broker (Alpaca or IBKR) is active.\n"
        f"• Add this exact line to the APEX-BOT-META block: 'brokers: {blist}'.\n"
        f"{ibkr_note}"
        "• OPTIONAL: include 'confidence': <0..1 float> in the decision dict. "
        "When present, the framework skips the trade if it's below the user's "
        "Minimum-confidence setting — so the confidence slider works for your "
        "bot. Omit it only for purely mechanical strategies.\n"
    )


def _make_system_prompt(mode: str = "trade",
                        brokers: list[str] | None = None) -> str:
    """Return the system prompt for the requested mode + target brokers.

    mode:    "trade" for a trading bot, "universe" for a universe-file
             generator.
    brokers: list of target brokers (any of "alpaca", "ibkr"). The bot is
             generated broker-AGNOSTIC and stamps META.brokers; v4.6.48
             (was a single broker + SDK-specific code pre-v4.6.48)."""
    guide = _load_skeleton_guide() or (
        "(Bot skeleton guide unavailable.)")
    if mode == "universe":
        # Universe generators are broker-agnostic (they only write a
        # .txt) — broker rules don't apply.
        return _SYSTEM_PROMPT_UNIVERSE.format(guide=guide)
    base = _SYSTEM_PROMPT_TRADE.format(
        guide=guide, template=_TRADE_TEMPLATE)
    return base + _brokers_rule(brokers or ["alpaca", "ibkr"])


# ── Worker thread for the API call ───────────────────────────────────

class _GenerateWorker(QThread):
    done    = pyqtSignal(bool, str)     # ok, code-or-error-text
    progress = pyqtSignal(str)          # status line for the UI

    def __init__(self, *, provider: str, model: str, key: str,
                 prompt: str, mode: str = "trade",
                 brokers: list[str] | None = None):
        super().__init__()
        self.provider = provider
        self.model    = model
        self.key      = key
        self.prompt   = prompt
        self.mode     = mode   # "trade" or "universe" (v4.6.4)
        self.brokers  = brokers or ["alpaca", "ibkr"]  # v4.6.48 (multi-broker)

    def run(self):
        # V4.6.23 — wrap the whole worker body so any future bug
        # (system-prompt format error, dict-access on a None, etc.)
        # surfaces as a user-visible error string instead of an
        # APEX 'unexpected error' crash dialog.
        try:
            self._run_inner()
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.done.emit(
                False,
                f"Make Bot worker crashed: {type(e).__name__}: {e}")

    def _run_inner(self):
        cfg = PROVIDERS.get(self.provider)
        if not cfg:
            self.done.emit(False, f"Unknown provider: {self.provider}")
            return
        self.progress.emit(f"Calling {self.provider}…")
        # V4.6.4 — the system prompt now varies by mode so the AI knows
        # whether to generate a trading bot or a universe-file rewriter,
        # and either way it MUST emit an APEX-BOT-META block with
        # ai_used = the actual provider used to generate this bot.
        headers, body = cfg["build"](
            self.prompt,
            _make_system_prompt(self.mode, self.brokers),
            self.key, self.model)
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
        # V4.6.6 — robust code extraction. Some models (especially text
        # ones like Llama) wrap code in ```python fences AND add prose
        # before/after ("Here's your bot:" / "Hope this helps!"). Pull
        # out the largest fenced block if present; else strip front/back
        # prose by finding the first import/comment/docstring and last
        # `__main__` block.
        import re as _re
        text = text.strip()
        # 1. Prefer the largest ```python ... ``` fenced block
        fences = _re.findall(r"```(?:python|py)?\s*\n(.*?)```",
                             text, flags=_re.DOTALL)
        if fences:
            cleaned = max(fences, key=len).strip()
        else:
            cleaned = text
            # 2. No fences — strip leading prose until the first
            #    triple-quoted docstring or `import`/`from` line
            lines = cleaned.splitlines()
            for i, line in enumerate(lines):
                stripped = line.lstrip()
                if (stripped.startswith('"""') or
                    stripped.startswith("'''") or
                    stripped.startswith("import ") or
                    stripped.startswith("from ") or
                    stripped.startswith("#!") or
                    stripped.startswith("# ")):
                    cleaned = "\n".join(lines[i:])
                    break
            # 3. Strip trailing prose after the last meaningful
            #    Python construct (find last `if __name__` or last
            #    line that looks like Python).
            tail_lines = cleaned.splitlines()
            for i in range(len(tail_lines) - 1, -1, -1):
                t = tail_lines[i].rstrip()
                if not t:
                    continue
                if t.startswith(" ") or t.startswith("\t") \
                        or any(t.startswith(p) for p in (
                            "def ", "class ", "import ", "from ",
                            "if ", "for ", "while ", "try:", "return ",
                            ")", "}", "]", '"""', "'''", "#")) \
                        or "=" in t or t.endswith(":") \
                        or t.endswith(")") or t.endswith("'") or t.endswith('"'):
                    cleaned = "\n".join(tail_lines[:i+1])
                    break
        cleaned = cleaned.strip()
        # Sanity: ensure we still have valid-ish Python (must contain
        # at least one `def ` and an APEX-BOT-META marker if v4.6.4+).
        if "def " not in cleaned:
            self.done.emit(False,
                "Model output had no `def` — likely returned prose only. "
                "Try again or pick a different model.")
            return
        # V4.6.26 — compile()-validate the AI's output before saving.
        # AIs frequently produce unterminated triple-quotes, half-finished
        # docstrings, or run out of tokens mid-function. compile() catches
        # all of those and surfaces a precise (line, msg) so the user
        # knows what to fix or whether to re-generate.
        try:
            compile(cleaned, "<bot>", "exec")
        except SyntaxError as e:
            self.done.emit(
                False,
                f"AI output has a Python syntax error at line {e.lineno}: "
                f"{e.msg}. The model likely ran out of tokens mid-function. "
                f"Try regenerating, picking a higher-quality model, or "
                f"shortening your prompt so the model has room to finish.")
            return
        self.done.emit(True, cleaned)


# ── V4.6.73 — public themed universes (server-generated) ──────────────
# The Oracle server's universe_factory regenerates a set of THEMED public
# universes weekly (long_term / short / short_term / speculative / options /
# crypto). Make Bot lets the user pick one of these at CREATION so the new
# bot trades a curated, pre-scored ticker list instead of the AI inventing
# symbols. All helpers are best-effort: a network/auth failure just falls
# back to "let the AI choose tickers".

def _server_creds() -> tuple:
    """(token, base_url) from the desktop auth files. Mirrors
    core.ibkr_data._cloud_creds without importing it (avoids a heavy import
    on a UI tab)."""
    tok = None
    url = "http://localhost:8000"
    try:
        with open(DATA_DIR / "apex_auth.json", encoding="utf-8") as f:
            tok = json.load(f).get("token")
    except Exception:
        pass
    try:
        with open(DATA_DIR / "apex_server.json", encoding="utf-8") as f:
            url = json.load(f).get("url", url).rstrip("/")
    except Exception:
        pass
    return tok, url


def _fetch_public_universes() -> list:
    """[{name, total, blurb}] from GET /universes — best-effort, [] on fail."""
    tok, url = _server_creds()
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    try:
        r = requests.get(f"{url}/universes", headers=headers, timeout=8)
        if r.ok:
            data = r.json()
            return data.get("universes", data) if isinstance(data, dict) else data
    except Exception as e:
        print(f"[make-bot] fetch universes failed: {e}")
    return []


def _fetch_universe_tickers(name: str) -> list:
    """Plain ticker list for one public universe via GET /universes/{name}.
    Parses the TICKER  # score=.. | reason format. [] on failure."""
    tok, url = _server_creds()
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    try:
        r = requests.get(f"{url}/universes/{name}", headers=headers, timeout=8)
        if not r.ok:
            return []
        txt = ""
        try:
            j = r.json()
            txt = j.get("content", j.get("text", "")) if isinstance(j, dict) else ""
        except Exception:
            txt = r.text
        out = []
        for ln in txt.splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            sym = ln.split("#")[0].strip()
            if sym:
                out.append(sym)
        return out
    except Exception as e:
        print(f"[make-bot] fetch universe '{name}' failed: {e}")
    return []


# ── Tab widget ────────────────────────────────────────────────────────

class MakeBotTab(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll = ScrollContent()
        root.addWidget(self.scroll)
        self._worker: Optional[_GenerateWorker] = None
        self._meta_worker = None
        self._last_meta: dict = {}
        self._build()

    def refresh(self):  # called by ApexWindow on tab activate
        pass

    # ── Layout ────────────────────────────────────────────────────────

    def _build(self):
        s = self.scroll

        s.add(SectionHeader("MAKE YOUR OWN BOT", C["purple"]))
        intro = QLabel(
            "Describe a trading strategy in plain English and let an AI "
            "write the bot for you. Three quick choices: which AI builds "
            "it, which brokers it targets, and which ticker universe it "
            "trades (a curated public universe, or let the AI pick). The "
            "AI also writes the bot's name, description and method — the "
            "info shown on its card. The generated file follows the APEX "
            "bot contract and can be saved to your library or published "
            "to the public bot store."
        )
        intro.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        intro.setWordWrap(True)
        s.add(intro)

        # ── Mode toggle: Create new vs Improve existing ────────────
        mode_row = QHBoxLayout()
        mode_lbl = QLabel("Mode:")
        mode_lbl.setStyleSheet(f"color:{C['text']};font-size:11px;")
        self._mode_combo = NoScrollComboBox()
        self._mode_combo.addItem("✨  Create a new bot", "create")
        self._mode_combo.addItem("🔧  Improve an existing bot", "improve")
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        self._existing_lbl = QLabel("Existing bot:")
        self._existing_lbl.setStyleSheet(f"color:{C['text']};font-size:11px;")
        self._existing_combo = NoScrollComboBox()
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

        # ── V4.6.73 — Universe at creation ────────────────────────
        # Replaces the old "Trading bot vs Universe generator" chooser.
        # Make Bot now ALWAYS creates a trading bot; the bot's ticker
        # universe is assigned HERE, at creation, from the server's
        # weekly themed public universes — or "Let the AI choose", in
        # which case the model invents the symbols as before. Picking a
        # public universe feeds its scored ticker list into the prompt
        # and stamps META.universe so the bot trades exactly that list.
        uni_row = QHBoxLayout()
        uni_lbl = QLabel("Ticker universe:")
        uni_lbl.setStyleSheet(f"color:{C['text']};font-size:11px;")
        self._universe_combo = NoScrollComboBox()
        self._universe_combo.setMinimumWidth(300)
        self._universe_combo.addItem(
            "🤖  Let the AI choose the tickers", "")
        try:
            for u in _fetch_public_universes():
                nm = u.get("name", "")
                if not nm:
                    continue
                total = u.get("total", "")
                blurb = (u.get("blurb", "") or "").strip()
                label = f"🌐  {nm}"
                if total:
                    label += f"  ({total} tickers)"
                if blurb:
                    label += f"  —  {blurb}"
                self._universe_combo.addItem(label, nm)
        except Exception as e:
            print(f"[make-bot] universe combo populate failed: {e}")
        uni_row.addWidget(uni_lbl)
        uni_row.addSpacing(8)
        uni_row.addWidget(self._universe_combo)
        uni_row.addStretch()
        uw = QWidget(); uw.setLayout(uni_row)
        s.add(uw)
        uni_hint = QLabel(
            "Pick a curated, pre-scored public universe (regenerated "
            "weekly on the APEX server) so your bot trades a fixed, "
            "vetted list of tickers — or leave it on “Let the AI choose” "
            "to have the model pick symbols from your description. The "
            "chosen universe is baked into the bot at creation; you "
            "don't assign it again later.")
        uni_hint.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        uni_hint.setWordWrap(True)
        s.add(uni_hint)

        # ── V4.6.48 — Compatible brokers (checkboxes, multi-select) ──
        # The bot is generated broker-AGNOSTIC (it uses the APEX framework's
        # decide() contract, never a broker SDK directly), so a single bot
        # runs on whichever broker is active. These checkboxes just declare
        # which brokers it's MEANT for (stamped in META.brokers). Both are
        # ticked by default so every new bot works on Alpaca AND IBKR.
        broker_row = QHBoxLayout()
        broker_lbl = QLabel("Compatible brokers:")
        broker_lbl.setStyleSheet(f"color:{C['text']};font-size:11px;")
        self._broker_alpaca_check = QCheckBox("Alpaca")
        self._broker_ibkr_check   = QCheckBox("IBKR")
        self._broker_alpaca_check.setChecked(True)
        self._broker_ibkr_check.setChecked(True)
        for _cb in (self._broker_alpaca_check, self._broker_ibkr_check):
            _cb.setStyleSheet(f"color:{C['text']};font-size:11px;")
        broker_row.addWidget(broker_lbl)
        broker_row.addSpacing(8)
        broker_row.addWidget(self._broker_alpaca_check)
        broker_row.addSpacing(14)
        broker_row.addWidget(self._broker_ibkr_check)
        broker_row.addStretch()
        bw = QWidget(); bw.setLayout(broker_row)
        s.add(bw)
        broker_hint = QLabel(
            "Tick the brokers this bot should run on — leave both ticked so it "
            "works on Alpaca AND IBKR. The bot is built broker-agnostic; APEX "
            "runs it on whichever broker you're in (and on the cloud). Note: on "
            "IBKR, crypto is limited to major coins (BTC/ETH/LTC/BCH); APEX "
            "auto-skips coins IBKR doesn't list.")
        broker_hint.setStyleSheet(f"color:{C['muted']};font-size:10px;")
        broker_hint.setWordWrap(True)
        s.add(broker_hint)

        # ── Provider + Model + Key form ─────────────────────────────
        form = QFrame()
        form.setStyleSheet(
            f"background:{C['panel']};border:none;"
            f"border-radius:8px;")
        fg = QGridLayout(form)
        fg.setContentsMargins(16, 14, 16, 14)
        fg.setHorizontalSpacing(12)
        fg.setVerticalSpacing(10)

        # Provider
        fg.addWidget(self._lbl("AI provider"), 0, 0)
        self._provider_combo = NoScrollComboBox()
        for name in PROVIDERS:
            self._provider_combo.addItem(name)
        self._provider_combo.currentTextChanged.connect(self._on_provider_changed)
        fg.addWidget(self._provider_combo, 0, 1)

        # APEX-credits badge — purple → green gradient, only shown when
        # the "Via APEX" provider is picked. Shows live cost + balance
        # (the worker refreshes balance after each successful generation).
        self._free_badge = QLabel("◊ 10 / gen  ·  balance: —")
        # V4.6.78 — flat, sober chip (was a purple→green gradient) to match
        # the minimalist style used across the rest of the app.
        self._free_badge.setStyleSheet(
            f"background:{C['panel2']};color:{C['muted']};"
            f"font-weight:700;font-size:10px;"
            "letter-spacing:1px;padding:4px 10px;border-radius:6px;"
        )
        self._free_badge.setVisible(False)
        fg.addWidget(self._free_badge, 0, 2)

        # Model
        fg.addWidget(self._lbl("Model"), 1, 0)
        self._model_combo = NoScrollComboBox()
        fg.addWidget(self._model_combo, 1, 1)

        # Custom model slug (only for OpenRouter "__custom__")
        self._custom_model_edit = QLineEdit()
        self._custom_model_edit.setPlaceholderText(
            "e.g. anthropic/claude-3.5-sonnet")
        self._custom_model_edit.setVisible(False)
        self._custom_model_edit.setStyleSheet(self._input_css())
        fg.addWidget(self._custom_model_edit, 1, 2)

        # API key — V3.1.9: per-provider key memory. Each provider has
        # its own slot in settings → switching from Gemini to Claude
        # and back restores each one's key without retyping.
        fg.addWidget(self._lbl("API key"), 2, 0)
        self._key_edit = QLineEdit()
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_edit.setStyleSheet(self._input_css())
        # Remember key on every edit
        self._key_edit.editingFinished.connect(self._save_current_key)
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
            "external signals. You don't need to list tickers — that's "
            "the “Ticker universe” dropdown above. The clearer the spec, "
            "the better the generated code.")
        desc_help.setStyleSheet(f"color:{C['muted']};font-size:11px;")
        desc_help.setWordWrap(True)
        s.add(desc_help)

        self._desc = QPlainTextEdit()
        self._desc.setStyleSheet(
            f"background:{C['panel2']};color:{C['text']};"
            f"border:none;border-radius:6px;"
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
            f"border:none;border-radius:6px;"
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
            f"border:none;border-radius:5px;"
            f"padding:6px 10px;font-family:'JetBrains Mono';font-size:11px;"
        )

    # ── Provider-driven model list ────────────────────────────────────

    def _provider_slot(self, name: str) -> str:
        """Stable settings key per provider. The settings file has one
        slot per provider so each is remembered independently."""
        return "makebot_keys::" + name

    def _load_key_for(self, provider: str) -> str:
        try:
            s = D.load_settings().get("makebot_keys", {})
            if s.get(provider):
                return s[provider]
        except Exception:
            pass
        # Back-compat: if no per-provider entry exists yet, fall back to
        # the user's synced Anthropic key for the Anthropic provider.
        if "Anthropic" in provider:
            try:
                return D.read_env_keys().get("ANTHROPIC_API_KEY", "") or ""
            except Exception:
                pass
        return ""

    def _save_current_key(self):
        try:
            prov = self._provider_combo.currentText()
            cfg = PROVIDERS.get(prov) or {}
            if cfg.get("credits"):
                return  # via-APEX has no key to save
            key = self._key_edit.text().strip()
            s = D.load_settings()
            s.setdefault("makebot_keys", {})[prov] = key
            import json as _json
            with open(D.SETTINGS_FILE, "w", encoding="utf-8") as f:
                _json.dump(s, f, indent=2)
        except Exception as e:
            print(f"[make-bot] save key failed: {e}")

    def _on_provider_changed(self, name: str):
        self._model_combo.clear()
        cfg = PROVIDERS.get(name) or {}
        for label, slug in cfg.get("models", []):
            self._model_combo.addItem(label, slug)
        # Show the custom-slug field when the OpenRouter "Custom…"
        # option is selected
        self._model_combo.currentIndexChanged.connect(self._refresh_custom_visibility)
        self._refresh_custom_visibility()
        # V3.1.9 — restore the saved key for this provider
        if hasattr(self, "_key_edit"):
            self._key_edit.setText(self._load_key_for(name))
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
            reg = D.load_bot_registry()
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

        # V4.6.73 — if the user picked a public themed universe, fetch its
        # ticker list and bake it into the prompt so the generated bot
        # trades EXACTLY those symbols (and stamps META.universe). Done on
        # the UI thread before the worker starts; best-effort.
        uni_name = ""
        try:
            uni_name = (self._universe_combo.currentData() or "").strip()
        except Exception:
            uni_name = ""
        if uni_name:
            self._status.setText(f"Loading “{uni_name}” universe…")
            tickers = _fetch_universe_tickers(uni_name)
            if tickers:
                tick_str = ", ".join(tickers)
                prompt = (
                    f"TICKER UNIVERSE (mandatory): This bot must trade ONLY "
                    f"the following pre-vetted tickers and no others. Set the "
                    f"BotRunner `default_symbols` to EXACTLY this list and set "
                    f"the APEX-BOT-META `universe:` field to `{uni_name}`:\n"
                    f"{tick_str}\n\n"
                    f"=== STRATEGY (how to trade the universe above) ===\n"
                    f"{prompt}"
                )
            else:
                QMessageBox.warning(self, "Universe unavailable",
                    f"Couldn't load the '{uni_name}' universe from the "
                    f"server (offline or not signed in). Generating with "
                    f"AI-chosen tickers instead.")

        self._gen_btn.setEnabled(False)
        self._gen_btn.setText("Generating…")
        self._status.setText("Calling the model — this can take 20-40 s.")
        self._status.setStyleSheet(f"color:{C['muted']};font-size:11px;")

        # V4.6.73 — Make Bot only generates trading bots now (the old
        # "universe generator" mode was removed; public universes are
        # generated on the server). Always trade mode.
        mode = "trade"
        # V4.6.48 — compatible-broker checkboxes (multi-select). The bot is
        # generated broker-agnostic and stamps META.brokers. Default: both.
        brokers = []
        if getattr(self, "_broker_alpaca_check", None) and self._broker_alpaca_check.isChecked():
            brokers.append("alpaca")
        if getattr(self, "_broker_ibkr_check", None) and self._broker_ibkr_check.isChecked():
            brokers.append("ibkr")
        if not brokers:
            brokers = ["alpaca", "ibkr"]   # never generate a broker-less bot
        self._worker = _GenerateWorker(
            provider=prov, model=model, key=key, prompt=prompt,
            mode=mode, brokers=brokers)
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
        self._last_meta: dict = {}
        # V3.1.4 — if we used the APEX-credits provider, refresh the
        # badge so the user sees the updated balance immediately.
        prov = self._provider_combo.currentText()
        cfg  = PROVIDERS.get(prov) or {}
        if cfg.get("credits"):
            self._refresh_credit_balance()
        # Auto-generate publish metadata in background using the same AI
        self._generate_meta_async(payload, prov)

    def _generate_meta_async(self, code: str, prov: str):
        """Ask the same AI to summarise the bot for the publish form."""
        cfg = PROVIDERS.get(prov) or {}
        if cfg.get("credits"):
            return   # skip for APEX-credits to avoid charging extra credits
        key   = self._key_edit.text().strip()
        model = self._resolved_model_slug()
        if not key or not model:
            return

        meta_prompt = (
            "Here is a trading-bot Python file. Analyse it and output ONLY a "
            "valid JSON object (no markdown fences, no extra text) with these "
            "exact keys:\n"
            "  \"name\"        — short human-readable name, 3-5 words\n"
            "  \"description\" — one sentence describing what the bot does\n"
            "  \"tags\"        — comma-separated list of 3-5 relevant trading tags\n"
            "  \"philosophy\"  — EXACTLY one of: long, short, day, options, "
            "momentum, mean-reversion, scalping, swing, other\n\n"
            "The file:\n\n" + code[:6000]
        )

        class _MetaWorker(QThread):
            done = pyqtSignal(dict)
            def __init__(self_, p, m, k, pr):
                super().__init__()
                self_.provider = p; self_.model = m
                self_.key = k; self_.prompt = pr

            def run(self_):
                cfg2 = PROVIDERS.get(self_.provider) or {}
                try:
                    hdrs, body = cfg2["build"](
                        self_.prompt,
                        "You are a concise JSON generator. Output only JSON.",
                        self_.key, self_.model)
                    url = cfg2["url"]
                    if "{model}" in url:
                        url = url.format(model=self_.model)
                    r = requests.post(url, headers=hdrs, json=body, timeout=30)
                    if r.ok:
                        raw = cfg2["extract"](r.json()).strip()
                        for fence in ("```json", "```"):
                            if raw.startswith(fence):
                                raw = raw[len(fence):].lstrip("\n")
                        if raw.endswith("```"):
                            raw = raw[:-3].rstrip()
                        import json as _json
                        meta = _json.loads(raw)
                        self_.done.emit(meta)
                    else:
                        self_.done.emit({})
                except Exception as e:
                    print(f"[make-bot] meta gen: {e}")
                    self_.done.emit({})

        def _on_meta(meta: dict):
            if not meta:
                return
            self._last_meta = meta
            if not self._name_edit.text().strip() and meta.get("name"):
                self._name_edit.setText(meta["name"])
            self._status.setText(
                f"Done — AI named it \"{meta.get('name','')}\" "
                f"({meta.get('philosophy','')}). Review & edit, then save.")

        self._meta_worker = _MetaWorker(prov, model, key, meta_prompt)
        self._meta_worker.done.connect(_on_meta)
        self._meta_worker.start()

    # ── Save / publish ───────────────────────────────────────────────

    def _on_save_local(self):
        code = self._code.toPlainText().strip()
        name = self._name_edit.text().strip()
        if not code:
            self._toast("Nothing to save — generate or paste code first.", err=True)
            return
        # V4.6.26 — compile()-validate before writing to disk. Catches
        # manual edits (typos, missing colons, broken docstrings) that
        # the worker's pre-save check can't see because the edit
        # happened in the editor pane after generation.
        try:
            compile(code, "<bot>", "exec")
        except SyntaxError as e:
            self._toast(
                f"Syntax error at line {e.lineno}: {e.msg}. "
                f"Fix it before saving.", err=True)
            return
        if not name:
            self._toast("Give the bot a name first.", err=True)
            return
        # Slug-ify
        slug = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                       for ch in name.strip().lower())

        # V4.6.73 — Make Bot only produces trading bots now (universe
        # generators were removed; public universes come from the server
        # and are assigned at creation via the Ticker-universe dropdown).
        bots_dir = ACCOUNT_DIR / "bots"
        bots_dir.mkdir(parents=True, exist_ok=True)
        dest = bots_dir / f"{slug}.py"
        dest.write_text(code, encoding="utf-8")

        # Register in the user's bot registry so MORE BOTS picks it up
        try:
            reg = D.load_bot_registry()
            # V4.6.76 — read META.brokers (a LIST: alpaca / ibkr) so the
            # registry records which brokers this bot supports. The old code
            # read a singular `broker` key that doesn't exist in META, so it
            # ALWAYS defaulted to 'alpaca' — which wrongly hid dual-broker
            # bots from the IBKR tab. Store both the list and a derived
            # singular string for backward-compatible consumers.
            # V4.6.77 — the user's broker CHECKBOXES are authoritative (the AI
            # sometimes stamps both brokers in META even when the user picked
            # only one). Fall back to META.brokers only if the checkboxes
            # aren't available (e.g. pasted code path).
            bot_brokers = []
            try:
                if getattr(self, "_broker_alpaca_check", None) and \
                        self._broker_alpaca_check.isChecked():
                    bot_brokers.append("alpaca")
                if getattr(self, "_broker_ibkr_check", None) and \
                        self._broker_ibkr_check.isChecked():
                    bot_brokers.append("ibkr")
            except Exception:
                bot_brokers = []
            if not bot_brokers:
                try:
                    from core.bot_meta import parse_meta
                    m = parse_meta(code) or {}
                    bl = m.get("brokers")
                    if isinstance(bl, list) and bl:
                        bot_brokers = [str(x).strip().lower() for x in bl if str(x).strip()]
                    elif isinstance(bl, str) and bl.strip():
                        bot_brokers = [bl.strip().lower()]
                except Exception:
                    pass
            if not bot_brokers:
                bot_brokers = ["alpaca", "ibkr"]
            bot_broker = ("alpaca,ibkr" if len(bot_brokers) > 1
                          else (bot_brokers[0] if bot_brokers else "alpaca"))
            existing = [c["id"] for c in reg.get("custom", [])]
            if slug not in existing:
                reg.setdefault("custom", []).append({
                    "id":      slug,
                    "label":   name,
                    "script":  str(dest),
                    "color":   C["purple"],
                    "broker":  bot_broker,
                    "brokers": bot_brokers,
                })
                D.save_bot_registry(reg)
        except Exception as e:
            print(f"[make-bot] registry update failed: {e}")

        self._toast(f"Saved {slug}.py to your library. "
                    f"See it in MORE BOTS → AVAILABLE TO ADD.")

    def _on_save_and_publish(self):
        """Save locally first, then ask the parent ApexWindow to
        publish using the existing marketplace flow (which prompts
        for tags / description and authenticates against the server).
        Pre-fills the publish dialogs with any AI-generated metadata."""
        self._on_save_local()
        code = self._code.toPlainText().strip()
        if not code:
            return
        name = self._name_edit.text().strip()
        slug = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                       for ch in name.strip().lower())
        path = ACCOUNT_DIR / "bots" / f"{slug}.py"
        if not path.exists():
            return
        # Build prefill from AI-generated metadata + current UI state
        prov = self._provider_combo.currentText()
        meta = getattr(self, "_last_meta", {})
        prefill = {
            "name":        name or meta.get("name", ""),
            "description": meta.get("description", ""),
            "tags":        meta.get("tags", ""),
            "philosophy":  meta.get("philosophy", ""),
            "creator_ai":  prov,   # the AI that made this bot
        }
        # Walk up to the ApexWindow and use MoreBotsTab._publish_bot_with_path
        win = self.window()
        more = getattr(win, "more_bots_tab", None)
        if more and hasattr(more, "_publish_bot_with_path"):
            more._publish_bot_with_path(str(path), prefill=prefill)
        else:
            self._toast("Saved locally. Open MORE BOTS to publish manually.",
                        err=False)

    # ── Toast helper ─────────────────────────────────────────────────

    def _toast(self, msg: str, err: bool = False):
        color = C["red"] if err else C["green"]
        self._save_msg.setText(msg)
        self._save_msg.setStyleSheet(f"color:{color};font-size:11px;")
        QTimer.singleShot(6000, lambda: self._save_msg.setText(""))

