"""
APEX UNIVERSE MANAGER v2
─────────────────────────────────────────────────────────────────
Runs ONCE per week (every Monday at market open).
Automatically maintains universe files for all three bots:

  daybot_universe.txt     ← day bot active tickers
  longbot_universe.txt    ← long bot active tickers
  shortbot_universe.txt   ← short bot active tickers
  daybot_watchlist.txt    ← candidates being monitored

What makes this version smarter:
  • Dynamic discovery — fetches real market movers, top gainers,
    most active stocks TODAY. Finds stocks not in any hardcoded list.
  • Fixed seed pool as a safety net (known good names)
  • Scores every candidate per bot type (DAY/LONG/SHORT have
    different scoring — same stock ranked differently per strategy)
  • Claude Haiku decides what to add/remove/watchlist
  • All files written automatically

Cost: ~$0.009/week (3 Claude calls × $0.003 each)
      ~$0.47/year total

─────────────────────────────────────────────────────────────────
HOW TO SET UP AUTOMATIC WEEKLY RUNS (Windows):

  1. Open Task Scheduler (search in Start menu)
  2. Click "Create Basic Task" on the right
  3. Name: "APEX Universe Manager"
  4. Trigger: Weekly → Monday → 9:35 AM
  5. Action: Start a program
  6. Program: python
  7. Arguments: universe_manager.py
  8. Start in: C:\\Users\\Baptiste\\Downloads\\Trade_bot\\10
     (your actual folder path)
  9. Click Finish

To run manually anytime:
  python universe_manager.py          ← update all bots
  python universe_manager.py DAY      ← update day bot only
  python universe_manager.py LONG     ← update long bot only
  python universe_manager.py SHORT    ← update short bot only
  python universe_manager.py STATUS   ← just print current status

─────────────────────────────────────────────────────────────────
"""

import os
import sys
import json
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
from anthropic import Anthropic

# V4.6.28 — load .env from APEX_DATA_DIR explicitly AND with
# override=True. Plain load_dotenv() walks the cwd tree, which in
# the frozen build may not include the data dir, AND it DOES NOT
# override existing empty env vars — APEX's QProcessEnvironment
# pre-populates ANTHROPIC_API_KEY="" before the bot starts, which
# load_dotenv (default override=False) refused to replace. Result:
# 'Could not resolve authentication method' on every cloud bot
# Anthropic call. override=True forces .env values to win.
_dd = os.environ.get("APEX_DATA_DIR", "")
if _dd:
    load_dotenv(os.path.join(_dd, ".env"), override=True)
load_dotenv(override=True)   # fallback — also walks cwd

_anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
if not _anthropic_key:
    print("[universe-manager] ERROR: ANTHROPIC_API_KEY missing from "
          ".env. Open Tools → AI PROVIDER KEYS, paste your Anthropic "
          "key, save. Then re-run.", flush=True)
    sys.exit(2)
anthropic_client = Anthropic(api_key=_anthropic_key)

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS   = 900

UNIVERSE_FILES = {
    "DAY":   "daybot_universe.txt",
    "LONG":  "longbot_universe.txt",
    "SHORT": "shortbot_universe.txt",
}
WATCHLIST_FILE = "daybot_watchlist.txt"
LOG_FILE       = "universe_manager_log.jsonl"

# Universe size limits
MIN_TICKERS = 20
MAX_TICKERS = 55

# Max new tickers Claude can add per weekly run
MAX_ADD_PER_RUN = 8

# Stock filters — anything failing these is ignored
MIN_ATR_PCT  = 0.015    # must move >1.5%/day on average
MIN_VOLUME   = 400_000  # minimum avg daily volume
MIN_PRICE    = 3.0      # no penny stocks
MAX_PRICE    = 5000.0   # no Berkshire A

# ─────────────────────────────────────────
# SEED POOL (safety net — known good names)
# Dynamic discovery adds to this every run
# ─────────────────────────────────────────

SEED_POOL = [
    # Mega-cap tech
    "AAPL","MSFT","GOOGL","AMZN","META","NVDA","TSLA","AVGO",
    "ORCL","CRM","ADBE","NFLX","NOW","INTU","IBM",
    # Semiconductors
    "AMD","QCOM","INTC","MU","TXN","ARM","MRVL","AMAT",
    "LRCX","KLAC","SMCI","ASML","TSM","DELL",
    # AI / Cloud / Cyber
    "PLTR","CRWD","PANW","NET","DDOG","SNOW","ZS","MDB",
    "OKTA","APP","HUBS","GTLB","CFLT",
    # High-beta growth
    "MSTR","COIN","HOOD","SOFI","UPST","AFRM","RKLB",
    "ASTS","IONQ","RGTI","QBTS","SOUN","BBAI","ACHR","JOBY",
    # Finance
    "GS","JPM","MS","BAC","BLK","BX","KKR","V","MA",
    "AXP","PYPL","SCHW","IBKR",
    # Consumer
    "SHOP","ABNB","UBER","LYFT","DASH","ETSY","W","DUOL",
    "HIMS","CAVA","CELH","NKE","SBUX","CMG",
    # Healthcare / biotech
    "LLY","UNH","ISRG","VRTX","REGN","AMGN","MRNA",
    "SRPT","EXAS","BEAM",
    # Energy / industrial
    "XOM","CVX","OXY","VST","CEG","NEE","CCJ","OKLO",
    "SMR","GE","CAT","DE","URI","VRT","AVAV",
    # Defense
    "RTX","LMT","NOC","GD","AXON","LDOS",
    # China ADRs
    "BABA","PDD","JD","BIDU","NIO","XPEV","LI",
    # Crypto-linked
    "MARA","RIOT","CLSK","HUT","BITF",
    # Solar / clean energy
    "ENPH","SEDG","FSLR","RUN","PLUG","BE",
    # ETFs
    "QQQ","SPY","SMH","ARKK","XBI","SOXL","TQQQ",
    "LABU","FNGU","TECL","UVXY","IWM","DIA",
    # Space / emerging
    "LUNR","RDW","WOLF","HIMS","CELH","DUOL","CAVA",
]

SEED_POOL = list(dict.fromkeys(SEED_POOL))


# ─────────────────────────────────────────
# DYNAMIC DISCOVERY
# Fetches real market movers — finds stocks
# not in any hardcoded list
# ─────────────────────────────────────────

def discover_market_movers() -> list:
    """
    Fetch genuinely new tickers from live market data.
    Uses yfinance screeners — completely free.
    Returns list of ticker strings.
    """
    discovered = []

    screens = [
        "day_gainers",      # top % gainers today
        "most_actives",     # highest volume today
        "day_losers",       # top % losers (short candidates)
        "growth_technology_stocks",  # trending growth names
        "aggressive_small_caps",     # small caps moving
    ]

    for screen in screens:
        try:
            result = yf.screen(screen, count=30)
            quotes = result.get("quotes", [])
            tickers = [q.get("symbol","") for q in quotes if q.get("symbol")]
            discovered.extend(tickers)
            print(f"  [discover] {screen}: {len(tickers)} tickers")
            time.sleep(0.5)
        except Exception as e:
            print(f"  [discover] {screen} failed: {e}")

    # Clean up — remove options/warrants/preferred shares
    cleaned = []
    for t in discovered:
        t = t.upper().strip()
        # Skip if contains special characters typical of non-stocks
        if any(c in t for c in ["/",".","-","^","+"]) :
            continue
        if len(t) > 5:
            continue
        cleaned.append(t)

    unique = list(dict.fromkeys(cleaned))
    print(f"  [discover] Total unique discovered: {len(unique)}")
    return unique


# ─────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────

def score_ticker(ticker: str, bot_type: str) -> dict:
    """
    Fetch 3 months of data and compute a score for the given bot type.
    Returns None if ticker fails any hard filter.
    """
    try:
        tk   = yf.Ticker(ticker)
        hist = tk.history(period="3mo", auto_adjust=True)

        if hist.empty or len(hist) < 20:
            return None

        c = hist["Close"].values.astype(float)
        h = hist["High"].values.astype(float)
        l = hist["Low"].values.astype(float)
        v = hist["Volume"].values.astype(float)

        price   = c[-1]
        avg_vol = v[-20:].mean()

        # ── Hard filters ──────────────────────────────────
        if price < MIN_PRICE or price > MAX_PRICE:
            return None
        if avg_vol < MIN_VOLUME:
            return None

        # ATR
        tr      = np.maximum(
            h[1:] - l[1:],
            np.maximum(np.abs(h[1:]-c[:-1]), np.abs(l[1:]-c[:-1]))
        )
        atr     = float(tr[-14:].mean()) if len(tr) >= 14 else float(tr.mean())
        atr_pct = atr / price * 100 if price > 0 else 0

        if atr_pct < MIN_ATR_PCT * 100:
            return None

        # Returns
        w_ret = round((c[-1]/c[-5]  - 1)*100, 2) if len(c) >= 5  else 0
        m_ret = round((c[-1]/c[-20] - 1)*100, 2) if len(c) >= 20 else 0
        q_ret = round((c[-1]/c[-60] - 1)*100, 2) if len(c) >= 60 else 0

        # RSI (14-period)
        d  = np.diff(c[-15:])
        ag = np.where(d > 0, d,  0).mean()
        al = np.where(d < 0, -d, 0).mean()
        rsi = round(100 - 100/(1+ag/al), 1) if al > 0 else 100.0

        # Volatility
        vol = round(float(np.diff(np.log(c[-20:])).std() * 100), 3)

        # ── Score per bot type ────────────────────────────
        if bot_type == "DAY":
            # Wants: high ATR (moves a lot), high volume, volatile
            # Direction doesn't matter — brackets work both ways
            score = (
                atr_pct * 20 +
                abs(w_ret) * 0.5 +
                abs(m_ret) * 0.3 +
                vol * 5 +
                min(avg_vol / 1_000_000, 10) * 2
            )

        elif bot_type == "LONG":
            # Wants: positive momentum, oversold RSI, growing
            score = (
                max(0, m_ret) * 0.4 +
                max(0, w_ret) * 0.5 +
                max(0, 50 - rsi) * 0.3 +
                vol * 3
            )

        elif bot_type == "SHORT":
            # Wants: negative momentum, overbought RSI, overextended
            score = (
                max(0, -m_ret) * 0.4 +
                max(0, -w_ret) * 0.5 +
                max(0, rsi - 55) * 0.4 +
                vol * 3
            )
        else:
            score = 0.0

        return {
            "ticker":    ticker,
            "price":     round(price, 2),
            "atr_pct":   round(atr_pct, 2),
            "vol_m":     round(avg_vol / 1_000_000, 2),
            "w_ret":     w_ret,
            "m_ret":     m_ret,
            "q_ret":     q_ret,
            "rsi":       rsi,
            "volatility":vol,
            "score":     round(float(score), 2),
        }

    except Exception as e:
        return None


def scan_all(bot_type: str, current: list,
             discovered: list) -> list:
    """
    Score seed pool + discovered + current universe.
    Returns sorted list (best first).
    """
    all_tickers = list(dict.fromkeys(
        SEED_POOL + discovered + current
    ))
    print(f"  Scoring {len(all_tickers)} tickers for {bot_type}...")

    results = []
    for t in all_tickers:
        d = score_ticker(t, bot_type)
        if d:
            results.append(d)

    results.sort(key=lambda x: x["score"], reverse=True)
    print(f"  → {len(results)} passed filters | "
          f"best: {results[0]['ticker']} "
          f"(score {results[0]['score']:.1f})" if results else "  → 0 passed")
    return results


# ─────────────────────────────────────────
# FILE I/O
# ─────────────────────────────────────────

def _read_text_lines(path: str) -> list:
    """Read a text file as utf-8; tolerate legacy cp1252 bytes (em-dash 0x97
    etc.) written by older builds. On fallback, rewrite the file as utf-8
    so future reads stay clean."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.readlines()
    except UnicodeDecodeError:
        pass
    try:
        with open(path, encoding="cp1252") as f:
            data = f.readlines()
    except UnicodeDecodeError:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = f.readlines()
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(data)
        print(f"  [encoding] repaired {path} → utf-8")
    except Exception:
        pass
    return data


def read_universe(bot: str) -> list:
    path = UNIVERSE_FILES.get(bot, "")
    if not path or not os.path.exists(path):
        return []
    tickers = []
    for line in _read_text_lines(path):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tickers.append(line.upper().split()[0])
    return list(dict.fromkeys(tickers))


def write_universe(bot: str, tickers: list,
                   notes: dict, added: list, removed: list):
    path = UNIVERSE_FILES[bot]
    now  = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# APEX {bot} BOT — Universe\n",
        f"# Updated by universe_manager: {now}\n",
        f"# Added:   {', '.join(added)   or 'none'}\n",
        f"# Removed: {', '.join(removed) or 'none'}\n",
        f"# Total:   {len(tickers)} tickers\n",
        "# Bot picks up changes on its next run — no restart needed\n",
        "#\n",
    ]
    for t in tickers:
        note = notes.get(t, "")
        lines.append(f"{t:<8}  # {note}\n" if note else f"{t}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    print(f"  [{bot}] Written {len(tickers)} tickers → {path}")


def read_watchlist() -> dict:
    if not os.path.exists(WATCHLIST_FILE):
        return {}
    result = {}
    for line in _read_text_lines(WATCHLIST_FILE):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts  = line.split("#", 1)
        ticker = parts[0].strip().upper()
        note   = parts[1].strip() if len(parts) > 1 else ""
        if ticker:
            result[ticker] = note
    return result


def append_watchlist(ticker: str, note: str):
    now = datetime.now().strftime("%Y-%m-%d")
    with open(WATCHLIST_FILE, "a", encoding="utf-8") as f:
        f.write(f"{ticker:<8}  # {note} | added {now}\n")
    print(f"  [watchlist] + {ticker}: {note}")


def promote_from_watchlist(ticker: str):
    """Mark a watchlist ticker as promoted when it enters the universe."""
    if not os.path.exists(WATCHLIST_FILE):
        return
    lines = _read_text_lines(WATCHLIST_FILE)
    now = datetime.now().strftime("%Y-%m-%d")
    new_lines = []
    for line in lines:
        t = line.strip().upper().split()[0] if line.strip() else ""
        if t == ticker.upper():
            new_lines.append(f"# PROMOTED {now}: {line.strip()}\n")
        else:
            new_lines.append(line)
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


def log_run(data: dict):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(data) + "\n")


def ensure_watchlist_exists():
    if not os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            f.write("# APEX DAY BOT — Watchlist\n")
            f.write("# Stocks being monitored for potential universe addition\n")
            f.write("# Format: TICKER  # reason | date added\n")
            f.write("# The bot does NOT scan this file — it is for your reference\n\n")
            f.write("# ── CANDIDATES ──\n\n")
            f.write("# ── ON HOLD ──\n\n")
            f.write("# ── GRAVEYARD ──\n")
        print(f"  Created {WATCHLIST_FILE}")


# ─────────────────────────────────────────
# CLAUDE DECISION
# ─────────────────────────────────────────

def ask_claude(bot_type: str, current: list,
               scored: list, watchlist: dict) -> dict:
    """
    Send a compact table to Claude Haiku.
    Returns add/remove/watchlist decisions.
    """
    # Current universe rows (with their scores)
    scored_map    = {d["ticker"]: d for d in scored}
    current_rows  = []
    for t in current:
        d = scored_map.get(t)
        if d:
            current_rows.append(
                f"{t:<6} score={d['score']:5.1f} "
                f"w={d['w_ret']:+5.1f}% m={d['m_ret']:+5.1f}% "
                f"atr={d['atr_pct']:.1f}% vol={d['vol_m']:.1f}M"
            )
        else:
            current_rows.append(f"{t:<6} [failed filters this week]")

    # New candidates (not in current)
    new_rows = []
    for d in scored:
        if d["ticker"] not in current:
            new_rows.append(
                f"{d['ticker']:<6} score={d['score']:5.1f} "
                f"w={d['w_ret']:+5.1f}% m={d['m_ret']:+5.1f}% "
                f"atr={d['atr_pct']:.1f}% vol={d['vol_m']:.1f}M "
                f"rsi={d['rsi']:.0f}"
            )
            if len(new_rows) >= 20:
                break

    # Watchlist rows
    watch_rows = []
    for t, note in list(watchlist.items())[:10]:
        d = scored_map.get(t)
        if d:
            watch_rows.append(
                f"{t:<6} score={d['score']:5.1f} "
                f"w={d['w_ret']:+5.1f}% [{note[:40]}]"
            )
        else:
            watch_rows.append(f"{t:<6} [no data] [{note[:40]}]")

    bot_desc = {
        "DAY":   "bracket order strategy — wants high ATR (>1.5%), high volume, volatile stocks that move a lot intraday regardless of direction",
        "LONG":  "long momentum strategy — wants positive weekly/monthly returns, oversold RSI, growing companies with strong fundamentals",
        "SHORT": "short selling strategy — wants negative momentum, overbought RSI (>60), overextended stocks likely to fall",
    }

    prompt = f"""You manage the weekly stock universe for the APEX {bot_type} trading bot.
Strategy: {bot_desc[bot_type]}

CURRENT UNIVERSE ({len(current)} tickers):
{chr(10).join(current_rows) if current_rows else "empty"}

TOP NEW CANDIDATES (discovered this week, not in universe):
{chr(10).join(new_rows) if new_rows else "none passed filters"}

WATCHLIST (being monitored):
{chr(10).join(watch_rows) if watch_rows else "empty"}

Return ONLY valid JSON. No markdown. No text outside the JSON.

{{"add":["TICKER"],"remove":["TICKER"],"watchlist":["TICKER"],"reasoning":"one sentence max"}}

Rules:
- "add": up to {MAX_ADD_PER_RUN} tickers to add to active universe
- "remove": current tickers to remove (low score, wrong fit, gone quiet)
- "watchlist": interesting but not ready — add to monitoring list
- Keep total between {MIN_TICKERS} and {MAX_TICKERS} tickers
- Only use tickers from the lists above
- Remove tickers that failed filters or have consistently low scores
- Prioritise tickers discovered from real market movers over seed pool
- reasoning: one sentence"""

    response = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()
    raw = raw.replace("```json","").replace("```","").strip()
    print(f"  Claude: {raw[:300]}")

    try:
        result = json.loads(raw)
        # Ensure all keys exist
        result.setdefault("add", [])
        result.setdefault("remove", [])
        result.setdefault("watchlist", [])
        result.setdefault("reasoning", "")
        return result
    except Exception as e:
        print(f"  JSON error: {e} — keeping universe unchanged")
        return {"add":[],"remove":[],"watchlist":[],"reasoning":"parse error"}


# ─────────────────────────────────────────
# APPLY CHANGES
# ─────────────────────────────────────────

def apply(bot_type: str, current: list, decision: dict,
          scored_map: dict) -> tuple:
    """
    Apply Claude's decisions safely.
    Returns (new_universe, added, removed, notes, to_watch).
    """
    to_add    = [t.upper() for t in decision.get("add", [])
                 if t.upper() not in current]
    to_remove = [t.upper() for t in decision.get("remove", [])
                 if t.upper() in current]
    to_watch  = [t.upper() for t in decision.get("watchlist", [])
                 if t.upper() not in current]

    # Remove first
    new = [t for t in current if t not in to_remove]

    # Safety: never go below minimum
    if len(new) < MIN_TICKERS:
        restore = to_remove[:MIN_TICKERS - len(new)]
        new    += restore
        to_remove = [t for t in to_remove if t not in restore]
        if restore:
            print(f"  [{bot_type}] Safety restore: {restore}")

    # Add (cap at max)
    actually_added = []
    for t in to_add:
        if len(new) >= MAX_TICKERS:
            break
        if t not in new and t in scored_map:
            new.append(t)
            actually_added.append(t)

    # Build notes
    notes = {}
    for t in new:
        d = scored_map.get(t)
        if d:
            notes[t] = (
                f"score={d['score']:.0f} "
                f"atr={d['atr_pct']:.1f}% "
                f"vol={d['vol_m']:.1f}M "
                f"w={d['w_ret']:+.1f}% "
                f"m={d['m_ret']:+.1f}%"
            )

    return new, actually_added, to_remove, notes, to_watch


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────

def run_manager(bots=None):
    if bots is None:
        bots = ["DAY", "LONG", "SHORT"]

    print("=" * 65)
    print(f"APEX UNIVERSE MANAGER v2")
    print(f"Run: {datetime.now().strftime('%A %d %B %Y — %H:%M')}")
    print(f"Updating: {', '.join(bots)}")
    print("=" * 65)

    ensure_watchlist_exists()

    # ── Step 1: Dynamic discovery (done ONCE, shared across all bots) ──
    print("\n[1/4] Discovering market movers...")
    discovered = discover_market_movers()
    print(f"      {len(discovered)} new tickers found in market today")

    # ── Step 2: Read current state ──
    print("\n[2/4] Reading current universes...")
    watchlist = read_watchlist()
    for bot in bots:
        current = read_universe(bot)
        print(f"      {bot}: {len(current)} tickers | "
              f"Watchlist: {len(watchlist)} tickers")

    log_data = {
        "time":      datetime.now(timezone.utc).isoformat(),
        "discovered":len(discovered),
        "updates":   {}
    }

    # ── Step 3: Score + Claude decision per bot ──
    for i, bot_type in enumerate(bots, 3):
        print(f"\n[{i}/{ len(bots)+2 }] Processing {bot_type} BOT...")

        current = read_universe(bot_type)

        # Score everything
        scored_list = scan_all(bot_type, current, discovered)
        scored_map  = {d["ticker"]: d for d in scored_list}

        if not scored_list:
            print(f"  No valid candidates — skipping {bot_type}")
            continue

        # Ask Claude
        print("  Asking Claude Haiku...")
        decision = ask_claude(bot_type, current, scored_list, watchlist)

        print(f"  + Add:    {decision['add']}")
        print(f"  - Remove: {decision['remove']}")
        print(f"  ~ Watch:  {decision['watchlist']}")
        print(f"  ↳ {decision['reasoning']}")

        # Apply
        new_uni, added, removed, notes, to_watch = apply(
            bot_type, current, decision, scored_map
        )

        # Write universe file
        write_universe(bot_type, new_uni, notes, added, removed)

        # Update watchlist
        current_watch = read_watchlist()
        for t in to_watch:
            if t not in current_watch and t in scored_map:
                d      = scored_map[t]
                reason = (f"score={d['score']:.0f} "
                          f"atr={d['atr_pct']:.1f}% "
                          f"w={d['w_ret']:+.1f}%")
                append_watchlist(t, reason)

        # Promote watchlist entries that were added
        for t in added:
            if t in current_watch:
                promote_from_watchlist(t)
                print(f"  [watchlist] Promoted {t} → {bot_type} universe")

        log_data["updates"][bot_type] = {
            "before":    len(current),
            "after":     len(new_uni),
            "added":     added,
            "removed":   removed,
            "watchlisted":to_watch,
            "reasoning": decision.get("reasoning",""),
        }

        print(f"  Done: {len(current)} → {len(new_uni)} tickers "
              f"(+{len(added)} -{len(removed)})")

        # Small delay between bots to respect rate limits
        if i < len(bots) + 2:
            time.sleep(3)

    # ── Step 4: Summary ──
    print(f"\n{'='*65}")
    print("COMPLETE — Summary:")
    for bot, info in log_data["updates"].items():
        print(f"  {bot}: {info['before']} → {info['after']} tickers  "
              f"(+{len(info['added'])} -{len(info['removed'])})")
        if info["added"]:
            print(f"    Added:   {', '.join(info['added'])}")
        if info["removed"]:
            print(f"    Removed: {', '.join(info['removed'])}")
        if info["reasoning"]:
            print(f"    Reason:  {info['reasoning']}")

    log_run(log_data)
    print(f"\nLog: {LOG_FILE}")
    print(f"Cost this run: ~${0.003 * len(bots):.3f}")


def print_status():
    print("\n── UNIVERSE STATUS ──────────────────────────────────")
    for bot, path in UNIVERSE_FILES.items():
        tickers = read_universe(bot)
        exists  = "✓" if os.path.exists(path) else "✗ (not created yet)"
        print(f"  {bot:6s}: {len(tickers):3d} tickers  {path} {exists}")
    watchlist = read_watchlist()
    exists    = "✓" if os.path.exists(WATCHLIST_FILE) else "✗"
    print(f"  {'WATCH':6s}: {len(watchlist):3d} tickers  "
          f"{WATCHLIST_FILE} {exists}")
    print()

    # Show log if exists
    if os.path.exists(LOG_FILE):
        lines = _read_text_lines(LOG_FILE)
        if lines:
            last = json.loads(lines[-1])
            print(f"  Last run: {last.get('time','unknown')[:16]}")
            for bot, info in last.get("updates",{}).items():
                print(f"  {bot}: added {info.get('added',[])} "
                      f"removed {info.get('removed',[])}")


# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────

if __name__ == "__main__":
    cmd = sys.argv[1].upper() if len(sys.argv) > 1 else "ALL"

    if cmd == "STATUS":
        print_status()
    elif cmd == "ALL":
        run_manager(["DAY","LONG","SHORT"])
    elif cmd in ("DAY","LONG","SHORT"):
        run_manager([cmd])
    else:
        print(f"Unknown: {cmd}")
        print("Usage: python universe_manager.py [ALL|DAY|LONG|SHORT|STATUS]")

