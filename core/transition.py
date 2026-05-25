"""
APEX · Bot Transition Cleanup
─────────────────────────────────────────────────────────────────
When a bot is pointed at an Alpaca paper account it didn't trade
on last session (e.g. the user moved which slot powers which bot,
or assigned an existing account to a new bot), the bot will inherit
positions opened by whatever bot ran there previously. A SHORT bot
inheriting a LONG bot's basket bleeds P/L trying to manage trades
it would never have opened.

Flow
────
1. Bot startup calls   `record_account_or_flag_transition(side, account_id)`
   • If state file has a different account_id for this side → flag
     "cleanup_pending" and persist the new account_id immediately so
     we never re-flag if the bot crash-loops.
2. Each tick the bot calls
       `maybe_run_cleanup(side, trading_client, ai_caller, philosophy)`
   • Runs when we are inside the cleanup window:
       - 15 min before next regular market open, OR
       - market is already open AND cleanup is still pending
   • Fetches every open position from the trading client.
   • Asks the bot's own AI: "Given your philosophy, for each
     inherited position decide HOLD / REDUCE / CLOSE."
   • Executes the decisions, clears the flag.
3. If the AI call fails, the conservative fallback is CLOSE every
   inherited position — a clean slate is safer than a wrong bot
   managing the wrong basket.

State lives in DATA_DIR/apex_transition_state.json:
    {
      "LONG":  {"account_id": "...", "cleanup_pending": false,
                 "last_cleanup_at": "2026-05-25T13:15:00Z"},
      "SHORT": {...},
      "CRYPTO":{...}
    }

Public functions used by longbot_v2 / shortbot_v2 / daybot / custom bots:
    record_account_or_flag_transition(side, account_id)
    maybe_run_cleanup(side, trading_client, ai_caller, philosophy)
"""

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional


# ── Data dir resolution ─────────────────────────────────────────
def _data_dir() -> Path:
    """Resolve APEX data dir without importing core.data (which would
    drag in PyQt at import time and slow down bot startup)."""
    p = os.environ.get("APEX_DATA_DIR")
    if p:
        return Path(p)
    if os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "APEX Trading Platform"
    return Path.home() / ".apex_trading_data"


STATE_FILE = _data_dir() / "apex_transition_state.json"


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(s: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[transition] save_state failed: {e}", flush=True)


# ── Public API ──────────────────────────────────────────────────

def record_account_or_flag_transition(side: str, account_id: str) -> bool:
    """Call at bot startup with the account id returned by Alpaca.
    Returns True if a transition was just detected (cleanup pending).
    Writes state immediately so a crash-loop after the flip doesn't
    re-trigger cleanup on every restart."""
    side = side.upper()
    state = _load_state()
    entry = state.get(side) or {}
    prev_id = entry.get("account_id")
    if not prev_id:
        # First time we've ever seen this bot — remember the account
        # but DON'T cleanup (we have no idea what's a fresh position
        # vs an inherited one if there's no prior record).
        state[side] = {
            "account_id":     account_id,
            "cleanup_pending": False,
            "last_cleanup_at": None,
        }
        _save_state(state)
        return False
    if prev_id == account_id:
        return entry.get("cleanup_pending", False)
    # Account flip detected
    print(f"[transition] {side} bot moved accounts "
          f"{prev_id[:8]}…  →  {account_id[:8]}…  "
          f"— flagging cleanup_pending", flush=True)
    state[side] = {
        "account_id":     account_id,
        "cleanup_pending": True,
        "previous_account_id": prev_id,
        "transition_detected_at": datetime.now(timezone.utc).isoformat(),
        "last_cleanup_at": entry.get("last_cleanup_at"),
    }
    _save_state(state)
    return True


def _in_cleanup_window(trading_client) -> tuple[bool, str]:
    """Return (should_run, reason). True when:
      - Market is open  (so cleanup runs immediately on first tick), OR
      - We're within 15 min of next regular market open.
    Returns False outside trading hours if the next open is more than
    15 minutes away, so the bot doesn't run cleanup hours early."""
    try:
        clock = trading_client.get_clock()
    except Exception as e:
        return (False, f"clock fetch failed: {e}")
    if clock.is_open:
        return (True, "market open")
    try:
        next_open = clock.next_open
        now       = clock.timestamp
        delta     = next_open - now
        if timedelta(0) <= delta <= timedelta(minutes=15):
            mins = int(delta.total_seconds() / 60)
            return (True, f"{mins} min before market open")
        return (False, f"next open in {int(delta.total_seconds()/60)} min")
    except Exception as e:
        return (False, f"window check failed: {e}")


def _build_cleanup_prompt(side: str, philosophy: str,
                          positions: list[dict]) -> str:
    lines = [
        f"You are the APEX {side} bot. You just inherited an Alpaca "
        f"paper account that was being traded by a different APEX bot "
        f"with a different strategy. Your trading philosophy:",
        "",
        f"  {philosophy}",
        "",
        f"Below are the {len(positions)} open positions you inherited. "
        f"For each one, decide whether to HOLD, REDUCE (close half the "
        f"shares), or CLOSE (exit fully) based on how well it fits YOUR "
        f"strategy — NOT the previous bot's. A position is only worth "
        f"keeping if you would have opened it yourself today given its "
        f"current side (long/short), size, and unrealized P/L.",
        "",
        "Positions:",
    ]
    for p in positions:
        lines.append(
            f"  - {p['symbol']}  side={p['side']}  qty={p['qty']}  "
            f"avg_entry=${p['avg_entry_price']}  "
            f"unrealized_pl=${p['unrealized_pl']}  "
            f"market_value=${p['market_value']}")
    lines += [
        "",
        "Reply with STRICT JSON only — an array of decisions, one per "
        "ticker, in this exact shape:",
        '  [{"ticker": "AAPL", "action": "HOLD|REDUCE|CLOSE", '
        '"reason": "short why"}]',
        "Do not wrap in markdown. Do not add commentary outside the JSON.",
    ]
    return "\n".join(lines)


def _safe_parse_decisions(text: str, positions: list[dict]) -> list[dict]:
    """Parse the AI response, falling back to CLOSE-ALL if anything
    fails or the structure is malformed."""
    import re
    fallback = [{"ticker": p["symbol"], "action": "CLOSE",
                 "reason": "AI parse failed — safe default"}
                for p in positions]
    if not text:
        return fallback
    # Strip ```json fences if the model added them
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        parsed = json.loads(t)
    except Exception:
        # Try to find the first [...] block in the response
        m = re.search(r"\[.*\]", t, re.DOTALL)
        if not m:
            return fallback
        try:
            parsed = json.loads(m.group(0))
        except Exception:
            return fallback
    if not isinstance(parsed, list):
        return fallback
    out = []
    for d in parsed:
        if not isinstance(d, dict):
            continue
        action = str(d.get("action", "")).upper()
        if action not in ("HOLD", "REDUCE", "CLOSE"):
            action = "CLOSE"
        out.append({
            "ticker": str(d.get("ticker", "")).upper(),
            "action": action,
            "reason": str(d.get("reason", "")).strip() or "(no reason given)",
        })
    return out or fallback


def _execute_decision(trading_client, position, decision: dict) -> str:
    """Place the order(s) implied by the decision. Returns a log string."""
    action = decision["action"]
    sym    = position["symbol"]
    qty    = abs(int(float(position["qty"])))
    pos_side = position["side"]  # "long" or "short"
    if action == "HOLD":
        return f"  HOLD   {sym:<6}  ({decision['reason']})"
    if action == "REDUCE":
        target = max(1, qty // 2)
        try:
            trading_client.close_position(sym, qty=str(target))
            return (f"  REDUCE {sym:<6}  closed {target}/{qty}  "
                    f"({decision['reason']})")
        except Exception as e:
            return f"  REDUCE {sym:<6}  FAILED: {e}"
    # CLOSE
    try:
        trading_client.close_position(sym)
        return (f"  CLOSE  {sym:<6}  exited {qty} {pos_side}  "
                f"({decision['reason']})")
    except Exception as e:
        return f"  CLOSE  {sym:<6}  FAILED: {e}"


def maybe_run_cleanup(side: str,
                      trading_client,
                      ai_caller: Callable[[str], Optional[str]],
                      philosophy: str) -> bool:
    """Run the cleanup pass if it's pending and we're in the window.
    Returns True if a cleanup actually executed.

    Args:
      side: bot side e.g. "LONG" / "SHORT" / "DAY" / "CRYPTO"
      trading_client: an alpaca TradingClient
      ai_caller: function(prompt: str) -> str | None  — wraps whichever
                 AI provider this bot uses (anthropic / google / etc.)
      philosophy: one-paragraph description of the bot's strategy.
    """
    side  = side.upper()
    state = _load_state()
    entry = state.get(side) or {}
    if not entry.get("cleanup_pending"):
        return False
    ok, why = _in_cleanup_window(trading_client)
    if not ok:
        return False
    print(f"[transition] {side} cleanup window reached "
          f"({why}) — reviewing inherited basket", flush=True)
    # Fetch positions
    try:
        positions_raw = trading_client.get_all_positions()
    except Exception as e:
        print(f"[transition] {side} get_all_positions failed: {e}",
              flush=True)
        return False
    positions = [{
        "symbol":           p.symbol,
        "side":             getattr(p, "side", "long"),
        "qty":              str(p.qty),
        "avg_entry_price":  str(getattr(p, "avg_entry_price", "?")),
        "unrealized_pl":    str(getattr(p, "unrealized_pl", "?")),
        "market_value":     str(getattr(p, "market_value", "?")),
    } for p in positions_raw]
    if not positions:
        print(f"[transition] {side} no inherited positions — "
              f"clearing flag", flush=True)
        entry["cleanup_pending"] = False
        entry["last_cleanup_at"] = datetime.now(timezone.utc).isoformat()
        state[side] = entry
        _save_state(state)
        return True
    prompt = _build_cleanup_prompt(side, philosophy, positions)
    print(f"[transition] {side} asking AI to review "
          f"{len(positions)} inherited positions…", flush=True)
    try:
        ai_text = ai_caller(prompt)
    except Exception as e:
        print(f"[transition] {side} AI call failed: {e} "
              f"— defaulting to CLOSE ALL", flush=True)
        ai_text = ""
    decisions = _safe_parse_decisions(ai_text or "", positions)
    by_ticker = {d["ticker"]: d for d in decisions}
    print(f"[transition] {side} executing cleanup decisions:", flush=True)
    for p in positions_raw:
        d = by_ticker.get(p.symbol.upper(),
                          {"action": "CLOSE", "reason": "no decision returned",
                           "ticker": p.symbol})
        # Convert position object → dict for executor
        pos_dict = {"symbol": p.symbol, "qty": str(p.qty),
                    "side": getattr(p, "side", "long")}
        print(_execute_decision(trading_client, pos_dict, d), flush=True)
    # Mark complete
    entry["cleanup_pending"] = False
    entry["last_cleanup_at"] = datetime.now(timezone.utc).isoformat()
    state[side] = entry
    _save_state(state)
    print(f"[transition] {side} cleanup complete", flush=True)
    return True
