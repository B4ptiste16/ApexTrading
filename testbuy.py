"""
APEX-BOT-META
name:               TEST BUY (broker check)
description:        One-shot $100 BTC buy to verify orders go through on a broker.
method:             On first cycle, market-buy ~$100 of BTC, then hold forever. No AI.
ai_used:            none
compatible_models:  none
asset_type:         crypto
brokers:            alpaca, ibkr
universe:           testbuy_universe.txt
requirements:
"""

# A deliberately trivial bot used to confirm that BUY orders actually reach
# the broker. It buys ~$100 of BTC once (capped to available cash by the
# framework), then holds. Run it on Alpaca AND on IBKR to verify both.

import os
from core.bot_framework import BotRunner

TEST_USD        = 100.0
DEFAULT_SYMBOLS = ["BTC-USD"]


def decide(symbol, bars, position, account):
    if float(position.get("qty", 0) or 0) > 0:
        return {"action": "HOLD", "reason": "test position already open"}
    try:
        price = float(bars["Close"].squeeze().iloc[-1])
    except Exception:
        return {"action": "HOLD", "reason": "no price"}
    if price <= 0:
        return {"action": "HOLD", "reason": "bad price"}
    qty = TEST_USD / price
    return {"action": "BUY", "qty": qty, "confidence": 1.0,
            "reason": f"TEST buy ${TEST_USD:g} of {symbol} @ ${price:.2f}"}


if __name__ == "__main__":
    BotRunner(
        asset_type="crypto",
        default_symbols=DEFAULT_SYMBOLS,
        universe_path=os.environ.get("APEX_BOT_UNIVERSE", "testbuy_universe.txt"),
        tick_seconds=120,
        bar_period="3mo",
        bar_interval="1d",
        name="testbuy",
    ).run(decide)
