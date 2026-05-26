# How to build a custom bot for APEX (v4.6.20)

This file is the **complete, authoritative reference** for writing a
trading bot or universe generator that APEX can run. It is:

1. **Read by a human** who wants to write a bot from scratch.
2. **Pasted into the system prompt** of every Make Bot AI generation
   so the model knows every constraint without guessing.

Every rule below was added because a real generated bot violated it
and broke a real user. The rules are non-negotiable — APEX's runtime
enforces or detects almost all of them.

---

## 0. The two kinds of bots

| Kind | Job | Output | Runs on |
|---|---|---|---|
| **Trading bot** | Reads a universe of tickers, decides BUY/SELL/HOLD per tick, submits orders to Alpaca | Live orders | **Oracle (cloud)** when the user uses Run On Oracle, else locally in the frozen Python |
| **Universe generator** | Scans the market, picks tickers, writes them into a `*_universe.txt` file. **Never trades.** | A `*_universe.txt` file | **Locally** in the frozen Python (PyInstaller-bundled libs only) |

Both produce `.py` files. The universe generator's output (`*.txt`) is
the trading bot's input — assigning a universe bot to a trading bot in
the bot tab's Universe dropdown wires them together.

---

## 1. The contract — what APEX expects

A bot is a single Python file (`.py`, max 1 MB). It **must**:

1. Start with an `APEX-BOT-META` docstring block (§2).
2. Define a top-level `main()` function (no args).
3. Use `print(..., flush=True)` for all logs.
4. Read credentials from `os.environ`, **never hardcode keys**.
5. Honor `APEX_BOT_UNIVERSE` (trading bots) or write to `APEX_DATA_DIR/<META.universe>` (universe bots).

APEX launches it with `cwd` set to the user's APEX data folder
(`%LocalAppData%\APEX Trading Platform` on Windows). For cloud-run
custom bots on Oracle, cwd is `/opt/apex_bots` and the data folder is
`/opt/apex_users/user_<id>/`. **Always use `os.environ['APEX_DATA_DIR']`
to locate files; never assume cwd.**

---

## 2. The APEX-BOT-META block (mandatory)

The first docstring of the file MUST be the META block. Every field
is parsed by APEX and the server. Example for a trading bot:

```python
"""
APEX-BOT-META
name:               CRYPTO trend bot
description:        Linear-regression trend follower on top crypto pairs
method:             Fit 30-day regression. BUY on positive trend + 2% dip below trendline. SELL when trend flips negative.
ai_used:            groq
compatible_models:  groq, anthropic, openai
asset_type:         crypto
universe:           crypto_universe.txt
requirements:       scikit-learn
"""
```

| Field | Allowed values | Why it matters |
|---|---|---|
| `name` | Free text, ≤60 chars | Marketplace card label |
| `description` | Free text, ≤200 chars | One-line pitch on the marketplace + the bot tab |
| `method` | Free text | Plain-English how-it-decides |
| `ai_used` | `groq` / `anthropic` / `openai` / `gemini` / `none` | Provider that GENERATED this bot. Auto-moderation flags lies. |
| `compatible_models` | comma-list of providers | Providers that can RUN it at runtime if it makes AI calls |
| `asset_type` | `stocks` / `crypto` / `etfs` / `futures` / `options` / `universe` | Drives UI behavior — see §3 |
| `universe` | Filename of the `*_universe.txt` this bot **reads** (trading) or **writes** (universe gen) | Used for the file resolution |
| `requirements` | Pip packages beyond APEX's bundled set, comma-separated, or empty | Server auto-installs for cloud bots. Frozen local Python ignores. |

---

## 3. `asset_type` drives UI behavior

| `asset_type` | Auto-start scheduler? | Symbol format the bot uses |
|---|---|---|
| `stocks`, `etfs`, `futures`, `options` | ✅ Checkbox in Overview AUTOMATION | `AAPL`, `NVDA` (plain) |
| `crypto` | ❌ No checkbox — listed as `always-on (24/7)` | `BTC-USD` in code, framework translates to `BTC/USD` for Alpaca |
| `universe` (universe generators) | n/a (doesn't trade) | doesn't matter |

---

## 4. Trading bot template — **start from this** (do not invent your own boilerplate)

```python
"""
APEX-BOT-META
name:               <your name>
description:        <one-line pitch>
method:             <plain-english strategy summary>
ai_used:            <provider>
compatible_models:  <providers>
asset_type:         <stocks|crypto|etfs|futures|options>
universe:           <your_universe_file.txt>
requirements:       <pkgs or empty>
"""

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
import pandas as pd
import numpy as np
import yfinance as yf
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# ── 1. Load .env from APEX data dir so ALPACA_API_KEY is populated.
#       Child processes do NOT inherit .env automatically.
_data_dir = os.environ.get("APEX_DATA_DIR", "")
if _data_dir:
    load_dotenv(os.path.join(_data_dir, ".env"))
else:
    load_dotenv()

ALPACA_KEY      = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET   = os.environ["ALPACA_SECRET_KEY"]

# ── 2. Honor paper/live toggle. The header pill writes APEX_ALPACA_MODE.
ALPACA_IS_PAPER = (os.environ.get("APEX_ALPACA_MODE", "paper").lower() != "live")

# ── 3. Default symbols used only if no universe file is found.
DEFAULT_SYMBOLS = ["BTC-USD", "ETH-USD"]   # change for your asset_type


def load_universe():
    """Read tickers from APEX_BOT_UNIVERSE (set by the bot-tab universe
    dropdown). Falls back to DEFAULT_SYMBOLS if no file is found so the
    bot keeps trading even before the user runs their universe script."""
    fname = os.environ.get("APEX_BOT_UNIVERSE", "<your_universe_file.txt>")
    candidates = []
    if os.path.isabs(fname):
        candidates.append(Path(fname))
    if _data_dir:
        candidates.append(Path(_data_dir) / fname)
    candidates.append(Path.cwd() / fname)
    candidates.append(Path.cwd().parent / fname)
    for p in candidates:
        try:
            if p.exists():
                tickers = []
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    t = line.split("#", 1)[0].strip().split()[0].upper()
                    if t:
                        tickers.append(t)
                if tickers:
                    print(f"[bot] loaded {len(tickers)} tickers from {p}",
                          flush=True)
                    return tickers
        except Exception as e:
            print(f"[bot] error reading {p}: {e}", flush=True)
    print(f"[bot] no universe file '{fname}' — using {DEFAULT_SYMBOLS}",
          flush=True)
    return list(DEFAULT_SYMBOLS)


# ── 4. Crypto symbol mapping: yfinance uses BTC-USD, Alpaca uses BTC/USD.
def yf_to_alpaca(symbol):
    if "/" in symbol:
        return symbol
    if "-" in symbol:
        base, _, quote = symbol.partition("-")
        return f"{base}/{quote}"
    return symbol


def get_position_qty(client, alpaca_symbol):
    try:
        for p in client.get_all_positions():
            if p.symbol.upper() == alpaca_symbol.upper():
                return float(p.qty)
    except Exception:
        pass
    return 0.0


# ── 5. Order submission via MarketOrderRequest (NOT raw kwargs).
def submit_market_order(client, alpaca_symbol, side, qty=None, notional=None):
    try:
        kwargs = {"symbol": alpaca_symbol, "side": side,
                  "time_in_force": TimeInForce.GTC}
        if notional is not None:
            kwargs["notional"] = notional
        else:
            kwargs["qty"] = qty
        req = MarketOrderRequest(**kwargs)
        order = client.submit_order(order_data=req)
        print(f"  {side.name} {alpaca_symbol} "
              f"{'$'+str(notional) if notional else qty}  id={order.id}",
              flush=True)
    except Exception as e:
        print(f"  {side.name} {alpaca_symbol} FAILED: {e}", flush=True)


def main():
    print(f"[bot] starting - mode={'paper' if ALPACA_IS_PAPER else 'LIVE'}",
          flush=True)
    client = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=ALPACA_IS_PAPER)
    cycle = 0
    while True:
        cycle += 1
        # ── 6. Reload universe each cycle so script updates apply
        #       without a bot restart.
        symbols = load_universe()
        for symbol in symbols:
            try:
                # ── 7. Your strategy here. The decision MUST be one of:
                #       BUY (open long), SELL (close long),
                #       SHORT (open short — STOCKS ONLY, never crypto),
                #       COVER (close short), HOLD.
                #
                #       Decision must compare to CURRENT price, never to a
                #       fixed multiple of long-term mean (that pattern
                #       produces deterministic no-op loops).
                pass
            except Exception as e:
                print(f"  ERROR {symbol}: {e}", flush=True)
            time.sleep(2)  # gap between symbols
        print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] "
              f"cycle {cycle} done - sleeping 300s", flush=True)
        time.sleep(300)


if __name__ == "__main__":
    main()
```

---

## 5. Trading bot rules (must follow)

### 5a. Credentials

- **MUST call `load_dotenv(APEX_DATA_DIR/.env)` at module top**, before
  reading any env vars. Child processes don't inherit `.env` automatically.
- Read `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` from `os.environ`. Never
  hardcode.
- Honor `APEX_ALPACA_MODE` for paper vs. live: `paper=(mode != "live")`.

### 5b. Universe

- **MUST honor `APEX_BOT_UNIVERSE`** env var (set by the bot-tab dropdown).
  Fall back to `META.universe` filename, then to `DEFAULT_SYMBOLS`.
- **MUST reload the universe at the start of every cycle**, not just
  startup. Universe-script updates take effect on the next 5-min tick
  without a restart.
- Search order for the file: absolute path → `APEX_DATA_DIR/<file>` →
  `cwd/<file>` → `cwd.parent/<file>`.

### 5c. Orders

- **MUST use `MarketOrderRequest(...)`** wrapped with
  `client.submit_order(order_data=req)`. Raw kwargs (`symbol=`, `qty=`)
  to `submit_order` are not supported in modern alpaca-py.
- Check position BEFORE submitting BUY/SHORT (avoid duplicate entries).
- Always have an exit condition. A strategy that only enters and never
  exits is broken.

### 5d. Crypto-specific

- yfinance uses `BTC-USD`. Alpaca uses `BTC/USD`. Use the `yf_to_alpaca`
  helper from the template to convert before submitting orders.
- **Alpaca does NOT allow SHORTING crypto.** For `asset_type=crypto`,
  use only BUY / SELL / HOLD. Never emit SHORT or COVER.

### 5e. Strategy viability

- ❌ Don't compare `prediction[-1] > mean * 1.1`. Different scales →
  deterministic no-op loops.
- ✅ Compare predictions / signals to CURRENT price.
- ❌ Don't emit the same action every tick regardless of state.
- ✅ Have at least one clear entry AND one clear exit condition.
- Print feature values + the decision rule so the user can verify
  signals are firing.

---

## 6. Universe generator rules (different — pay attention)

Universe generators **run LOCALLY in the user's frozen APEX install**.
The frozen Python interpreter **cannot pip-install** new packages.

### 6a. Allowed imports (everything else CRASHES)

| Bundled | Use it for |
|---|---|
| `requests` | HTTP (Alpaca, CoinGecko, Polygon, Finnhub, IEX) |
| `yfinance` | Historical bars, 24h volume |
| `pandas`, `numpy` | Data wrangling |
| `alpaca-py` | READ-only — assets, bars, account. **No order submission.** |
| `json`, `os`, `re`, `time`, `datetime`, `math`, `statistics`, `pathlib` | stdlib |
| `python-dotenv` (`from dotenv import load_dotenv`) | Load `.env` |

### 6b. Forbidden imports

`ccxt`, `talib`, `ta`, `scikit-learn`, `sklearn`, `openai`, `anthropic`,
`google.generativeai`, `beautifulsoup4`, `lxml`, `scrapy`, `selenium`.

If you need crypto data, use Alpaca `/v2/assets?asset_class=crypto` or
CoinGecko's free public API. **Do not use ccxt** — it's not bundled.

### 6c. Must do

- **Load `.env`** from `APEX_DATA_DIR/.env` at module top.
- **Try authenticated source first, then fall back to a no-auth public
  API.** Example: Alpaca → CoinGecko for crypto, yfinance fallback for
  stocks.
- **Write notes in this exact format** so the Universe-tab breakdown
  table populates the Note column consistently:
  ```
  TICKER  # score=N vol=12.3B w=+5.2% m=+18.4%
  ```
  - `score` = your composite ranking (used for the Score column / sort)
  - `vol`   = 24h volume, formatted as `B` / `M` / `K`
  - `w`     = 7-day return, e.g. `+5.2%` / `-3.1%`
  - `m`     = 30-day return
  - You may add other fields (e.g. `atr=14.6%`) but always include vol/w/m
- **Write to `APEX_DATA_DIR/<META.universe>`**, never to `cwd`.
- **Run once, exit cleanly.** Not a daemon. APEX schedules re-runs.
- **If no data source succeeds, return WITHOUT overwriting the .txt.**
  Empty file = trading bot crashes.

### 6d. Universe generator template

```python
"""
APEX-BOT-META
name:               <your name>
description:        <one-line pitch>
method:             <plain-english>
ai_used:            none
compatible_models:  none
asset_type:         <stocks|crypto|...>
universe:           <your_universe_file.txt>
requirements:
"""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    _dd = os.environ.get("APEX_DATA_DIR", "")
    load_dotenv(os.path.join(_dd, ".env")) if _dd else load_dotenv()
except Exception:
    pass

import requests
import yfinance as yf
import pandas as pd

def fmt_volume(v):
    """Format USD volume as 12.3B / 450M / 8.2K."""
    try: v = float(v)
    except: return "?"
    if v >= 1e9: return f"{v/1e9:.1f}B"
    if v >= 1e6: return f"{v/1e6:.1f}M"
    if v >= 1e3: return f"{v/1e3:.1f}K"
    return f"{v:.0f}"

def try_authenticated_source():
    # Alpaca, Polygon, IEX, etc. Return None on auth failure.
    pass

def public_fallback_source():
    # CoinGecko / yfinance / Yahoo / etc. No auth required.
    pass

def main():
    syms = try_authenticated_source() or public_fallback_source()
    if not syms:
        print("[universe] no source - keeping existing universe", flush=True)
        return

    rows = []
    for sym in syms:
        try:
            t = yf.Ticker(sym)
            hist = t.history(period="35d", interval="1d", auto_adjust=False)
            if hist.empty or len(hist) < 8:
                continue
            close = float(hist["Close"].iloc[-1])
            vol_usd = float(hist["Volume"].iloc[-1]) * close
            w_pct = (close / float(hist["Close"].iloc[-8]) - 1) * 100
            m_pct = (close / float(hist["Close"].iloc[-min(31,len(hist))]) - 1) * 100
            import math
            score = math.log10(max(vol_usd, 1)) * 10
            rows.append({
                "sym":   sym,
                "score": score,
                "note":  f"vol={fmt_volume(vol_usd)} w={w_pct:+.1f}% m={m_pct:+.1f}%",
                "vol":   vol_usd,
            })
        except Exception as e:
            print(f"  skip {sym}: {e}", flush=True)
    if not rows:
        print("[universe] no scored symbols - keeping existing universe",
              flush=True)
        return

    df = pd.DataFrame(rows).sort_values("vol", ascending=False).head(20)

    data_dir = os.environ.get("APEX_DATA_DIR", ".")
    out = os.path.join(data_dir, "<your_universe_file.txt>")
    with open(out, "w", encoding="utf-8") as f:
        f.write("# asset_type: <your asset_type>\n")
        for _, r in df.iterrows():
            f.write(f"{r['sym']}  # score={r['score']:.0f} {r['note']}\n")
    print(f"[universe] wrote {len(df)} tickers to {out}", flush=True)
    print("[universe] DONE", flush=True)

if __name__ == "__main__":
    main()
```

---

## 7. Environment variables APEX exports to every bot

| Var | Set by | Meaning |
|---|---|---|
| `APEX_DATA_DIR` | Always | Absolute path of the user's data folder |
| `APEX_BOT_SIDE` | Always | `LONG` / `SHORT` / `DAY` / your slug |
| `APEX_BOT_UNIVERSE` | When user assigns one in the bot tab | Filename of the universe .txt the bot should read |
| `APEX_ALPACA_MODE` | Header paper/live pill | `paper` or `live` — drives `TradingClient(paper=…)` |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | From `.env` after `load_dotenv` | Bot's bundled key+secret |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GROQ_API_KEY`, `GOOGLE_AI_API_KEY` | From `.env` after `load_dotenv` | For AI-driven decision bots |
| `PYTHONIOENCODING` | Always `utf-8` | Ignore |

---

## 8. Quick checklist before drag-and-drop

### For trading bots

- [ ] APEX-BOT-META block at the top with all 9 fields
- [ ] `load_dotenv(APEX_DATA_DIR/.env)` at module top
- [ ] `load_universe()` honors `APEX_BOT_UNIVERSE`
- [ ] Universe reloaded at the top of every cycle
- [ ] `MarketOrderRequest(...)` wrapping `submit_order`
- [ ] Position lookup before BUY/SHORT
- [ ] Clear entry AND clear exit conditions
- [ ] Asset-type-correct symbol format + no SHORT for crypto
- [ ] `print(..., flush=True)` everywhere
- [ ] try/except around every external call

### For universe generators

- [ ] APEX-BOT-META block with `asset_type` matching the universe purpose
- [ ] `load_dotenv(APEX_DATA_DIR/.env)` at module top
- [ ] Imports limited to the bundled list in §6a
- [ ] Try-authenticated + public-fallback pattern
- [ ] Notes in the `score=N vol=X w=+Y% m=+Z%` format
- [ ] Writes to `APEX_DATA_DIR/<META.universe>`
- [ ] Returns without overwriting the .txt if no source succeeds
- [ ] Runs once, exits cleanly (not a daemon loop)
