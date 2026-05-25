# How to build a custom bot for APEX

This file is a complete reference for writing a trading bot that
APEX can run. It's intended to be:

1. **Read by a human** who wants to write a bot from scratch.
2. **Pasted into a chat with an AI** (Claude, GPT, etc.) to get
   help writing one — every constraint the AI needs to know is
   stated explicitly below.

When you're done with your `.py` file, drag and drop it onto the
APEX window. APEX will ask whether to install it locally or
publish it to the public bot library.

---

## 1. The contract — what APEX expects

A bot is a single Python file (`.py`, max 1 MB) that **exposes a
`main()` function** with no arguments, AND starts with an
**`APEX-BOT-META` block** declaring metadata (description, AI used,
dependencies, etc.) — see below. APEX launches it like this:

```
python -m your_bot_module
```

…via PyInstaller-bundled Python, **with `cwd` set to the user's
APEX data folder** (`%LocalAppData%\APEX Trading Platform` on
Windows, `~/.apex` on Linux). Your bot can read `.env` from this
folder for API keys.

### 1a. The mandatory APEX-BOT-META block (v4.6.4+)

Every bot **must** declare its metadata in a docstring at the top
of the file, starting with the literal line `APEX-BOT-META`. The
desktop app, the marketplace, and the Oracle server all parse this
block. It serves three purposes:

1. **Publishing info** — name / description / method show on cards.
2. **Compatibility** — `asset_type` and `compatible_models` tell
   APEX what universes and AI providers can pair with this bot.
3. **Dependencies** — `requirements` lists the pip packages the
   server should install before first start. Without this list,
   the server falls back to scanning your `import` statements and
   may guess wrong.

Schema (all fields are optional but **strongly recommended**):

| Key                 | Example                          | Meaning                                          |
|---------------------|----------------------------------|--------------------------------------------------|
| `name`              | `CRYPTO momentum bot`            | Human-readable label                             |
| `description`       | `Trades top BTC/ETH pairs on …`  | One-line pitch                                   |
| `method`            | `Linear regression + Groq vote`  | How the bot decides, in plain English            |
| `ai_used`           | `groq`                           | Provider used to GENERATE this bot               |
| `compatible_models` | `groq, anthropic`                | Providers that can RUN it                        |
| `asset_type`        | `crypto`                         | One of: stocks, crypto, etfs, futures, options   |
| `universe`          | `crypto_universe.txt`            | Universe file this bot expects                   |
| `requirements`      | `scikit-learn, ccxt`             | pip packages beyond APEX's bundled set           |

### Minimal skeleton

```python
"""
APEX-BOT-META
name:               My SPY bot
description:        Buys SPY at open, sells at close
method:             Single-position day-bracket on SPY
ai_used:            anthropic
compatible_models:  anthropic, openai
asset_type:         stocks
universe:           longbot_universe.txt
requirements:
"""

import os
import time
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()   # picks up ALPACA_API_KEY_LONG etc. from data dir

# 👇 These two env vars are guaranteed to exist when the bot is
#    started from APEX (the user's keys, scoped to this bot's slot).
ALPACA_KEY    = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET = os.environ["ALPACA_SECRET_KEY"]


def main():
    """Entry point. Must be defined at module level and take no args.
    APEX calls this in a fresh process and watches stdout. Return
    normally to exit; raise to signal an error."""
    from alpaca.trading.client import TradingClient
    client = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)

    while True:
        # do your work, sleep, etc.
        # Print logs to stdout — they show up in the APEX bot tab.
        print(f"[{datetime.now(timezone.utc).isoformat()}] checking…",
              flush=True)
        time.sleep(60)


if __name__ == "__main__":
    main()
```

---

## 2. What's available to your bot

When APEX bundles your bot script with the desktop app, the
following packages are guaranteed to be importable. (You don't
need to ship them — they're already in the installer.)

| Package           | Use                                       |
|-------------------|-------------------------------------------|
| `alpaca-py`       | Alpaca trading + market data              |
| `anthropic`       | Claude API (Haiku / Sonnet vision)        |
| `pandas`, `numpy` | Data wrangling                            |
| `yfinance`        | Historical bars / fallback quotes         |
| `requests`        | HTTP                                      |
| `python-dotenv`   | Read `.env`                               |

### Need extra packages? (v4.6.4+)

Any **cloud-run** bot can declare extra dependencies in its
`APEX-BOT-META → requirements:` line. The Oracle server preflights
`pip install` into the shared venv on the bot's first start and
caches a marker so subsequent starts skip the install:

```
requirements: scikit-learn, ccxt, ta-lib
```

If you forget to declare a requirement, the server's import scanner
will try to detect it from your `import` statements (covers common
packages like `sklearn` → `scikit-learn`, `cv2` → `opencv-python-headless`).
Declaring is more reliable.

**Local-run** custom bots use the frozen PyInstaller Python on the
user's machine — that interpreter can't pip-install new packages.
Limit local bots to the packages in the table above. If you need
something outside that list, the bot must run on the cloud.

---

## 3. Environment variables APEX sets

Before launching your bot, APEX exports these to the subprocess
environment:

| Var                    | Meaning                                              |
|------------------------|------------------------------------------------------|
| `ALPACA_API_KEY`       | Alpaca paper key for this bot's slot                 |
| `ALPACA_SECRET_KEY`    | Matching secret                                      |
| `ANTHROPIC_API_KEY`    | Claude API key (if the user set one)                 |
| `APEX_BOT_SIDE`        | e.g. `LONG` / `SHORT` / `DAY` / `CUSTOM`             |
| `APEX_DATA_DIR`        | Absolute path of the user's data folder              |
| `PYTHONIOENCODING`     | Always `utf-8` (you can ignore this)                 |

Read them with `os.environ["ALPACA_API_KEY"]` — they're already
loaded by the time `main()` runs.

---

## 4. Logging and persistence

- **stdout / stderr** — every line you `print(..., flush=True)`
  ends up in the bot's Output panel in real time. Use this for
  status messages.
- **State files** — if you want to persist anything across runs
  (last trade timestamp, accumulated P/L, etc.), write to
  `APEX_DATA_DIR/<your_bot_name>_state.json`. APEX preserves this
  file across updates.
- **Trade log** — append a JSON line per trade to
  `APEX_DATA_DIR/<your_bot_name>_trade_log.jsonl`. The Overview
  tab parses these automatically into the sold-trades feed.

---

## 5. Constraints — what your bot must NOT do

- **No GUI code.** No Qt, no Tkinter. Bots are headless.
- **No `sys.exit(0)` on success.** Just `return` from `main()` so
  APEX can detect clean exits vs. crashes.
- **No `os.system` / `subprocess.Popen` without a really good
  reason.** APEX will warn the user before installing a bot that
  shells out.
- **No file writes outside `APEX_DATA_DIR`.** Anything else will
  be rejected when publishing to the library.
- **No hardcoded keys.** Always read from env vars — the user's
  keys must stay in their own `.env`.
- **Max file size 1 MB.** If you need data files, generate them
  at runtime, don't ship them.

---

## 6. Publishing your bot

Two paths:

### Local only (just you)

Drag the `.py` onto the APEX window → "Add to my library". The
bot appears under MORE BOTS → AVAILABLE TO ADD. Click + to make
a tab for it.

### Public (everyone can browse + install it)

Drag the `.py` onto the APEX window → "Publish publicly…". APEX
prompts for:

- **Display name** — what shows on the marketplace card.
- **Description** — one line. Be specific about strategy.
- **Tags** — comma-separated, e.g. `long, mean-reversion, daily`.

You must be signed in to publish. APEX uploads the file to the
shared server and assigns a unique slug. Other users see your
bot in MORE BOTS → BROWSE PUBLIC BOTS and can install it with
one click.

You can delete your own published bots at any time from the
marketplace; downloads stop immediately.

---

## 7. Quick checklist before drag-and-drop

- [ ] Single `.py` file, under 1 MB
- [ ] **`APEX-BOT-META` docstring** at the top with name, description, method, ai_used, compatible_models, asset_type, universe, requirements
- [ ] `main()` function at module level, no args
- [ ] Reads API keys from `os.environ`, never hardcoded
- [ ] Uses `print(..., flush=True)` so logs appear live
- [ ] Returns from `main()` to exit cleanly
- [ ] Cloud bot: extra deps declared in `requirements:` — Local bot: only bundled packages
- [ ] Writes only inside `APEX_DATA_DIR`

If all boxes are ticked, drop it on APEX and you're done.

---

## 8. Building a UNIVERSE bot instead of a trading bot (v4.6.4+)

The Make Bot tab has a switch: **Trade Bot** (default) or **Universe Bot**.

A universe bot doesn't open trades — it produces a fresh `*_universe.txt`
file that a trading bot then consumes. Examples: nightly scanner that
picks the top 30 momentum stocks, weekly screen for high-beta crypto,
sentiment-driven shortlist.

A universe bot's META block declares:

```
APEX-BOT-META
name:         Crypto top-20 scanner
description:  Rewrites crypto_universe.txt nightly with the top-20 movers
method:       24h-volume rank + price-action filter
asset_type:   crypto
universe:     crypto_universe.txt   ← the file this bot REWRITES
ai_used:      groq
compatible_models: groq, anthropic
requirements: ccxt
```

The `asset_type` field is what APEX uses to enforce compatibility:
when you pair a trading bot with a universe in the bot tab, the
dropdown only lists universes whose `asset_type` matches the trading
bot's. A `crypto` bot will never accidentally consume a `stocks`
universe.
