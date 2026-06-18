"""
APEX marketplace bot generator (v4.6.115)
─────────────────────────────────────────
Produces a matrix of self-contained, valid APEX framework bots:
  5 strategy engines  ×  20 themed universes  =  100 bots.

Each bot:
  • is a real `core.bot_framework.BotRunner` bot with a `decide()` function,
  • embeds its theme's tickers as `default_symbols` so it trades out-of-box,
  • declares an honest APEX-BOT-META block (ai_used / asset_type / brokers / tags),
  • is priced in credits (1 credit = $0.01) across the $0.49–$9.99 band,
  • is classified with tags = "<strategy>, <theme>, <asset_type>".

~70 bots are ML-only (pure indicators, no AI key needed to run); ~30 are
AI-driven (call Claude each cycle, themed by the strategy thesis).

Output: tools/generated_market_bots/<slug>.py  +  catalog.json (name, tags,
price_credits, description, ai_used) for the publish step.
"""
from __future__ import annotations
import json
import os
from pathlib import Path

OUT = Path(__file__).resolve().parent / "generated_market_bots"
OUT.mkdir(parents=True, exist_ok=True)

# ── 20 themed universes (tickers + human description) ───────────────────────
THEMES: dict[str, tuple[list, str, str]] = {
    # key: (tickers, description, asset_type)
    "semiconductors": (["NVDA","AMD","INTC","MU","QCOM","AVGO","TXN","ADI","LRCX","KLAC","AMAT","ASML","TSM","MRVL","ON","MCHP","NXPI","ARM","SMCI","QRVO"], "chipmakers & semiconductor equipment", "stocks"),
    "ai_infrastructure": (["NVDA","AVGO","AMD","TSM","ASML","ARM","MU","MRVL","SMCI","DELL","VRT","ANET","CRDO","MPWR","ORCL","GOOGL","MSFT","META","PLTR","SNOW"], "picks-and-shovels for the AI build-out", "stocks"),
    "software_cloud": (["MSFT","CRM","ADBE","ORCL","NOW","SNOW","DDOG","MDB","NET","TEAM","WDAY","ZS","OKTA","PANW","CRWD","FTNT","INTU","SHOP","SNPS","CDNS"], "cloud & enterprise SaaS", "stocks"),
    "cybersecurity": (["CRWD","PANW","ZS","FTNT","OKTA","S","NET","CYBR","RPD","QLYS","TENB","VRNS","CHKP","GEN","AKAM"], "cybersecurity pure-plays", "stocks"),
    "fintech_payments": (["V","MA","PYPL","AXP","FI","GPN","COIN","HOOD","SOFI","AFRM","NU","TOST","FOUR","UPST"], "payments, fintech & digital banking", "stocks"),
    "banks": (["JPM","BAC","WFC","C","GS","MS","USB","PNC","TFC","SCHW","COF","BK","ALLY","RF","FITB","KEY","CFG"], "money-center & regional banks", "stocks"),
    "ev_clean_energy": (["TSLA","RIVN","LCID","NIO","LI","XPEV","ENPH","FSLR","SEDG","RUN","PLUG","CHPT","BE","ALB","NEE","ARRY"], "electric vehicles & clean-energy", "stocks"),
    "energy_oil": (["XOM","CVX","COP","SLB","EOG","OXY","PSX","MPC","VLO","HAL","DVN","FANG","KMI","WMB","TRGP","BKR","OKE","EQT"], "oil, gas & energy services", "stocks"),
    "healthcare": (["UNH","LLY","JNJ","PFE","MRK","ABBV","BMY","AMGN","GILD","CVS","TMO","DHR","ISRG","MDT","ABT","ELV","CI","HCA","SYK","VRTX"], "large-cap healthcare, pharma & med-tech", "stocks"),
    "biotech": (["MRNA","BNTX","VRTX","REGN","BIIB","ALNY","EXEL","INCY","NBIX","BMRN","SRPT","IONS","UTHR","HALO","ARWR","CRSP","NTLA","BEAM"], "biotech — clinical & commercial", "stocks"),
    "consumer_discretionary": (["AMZN","HD","NKE","MCD","SBUX","LOW","TGT","BKNG","CMG","MAR","GM","F","ORLY","AZO","ROST","YUM","LULU","RCL"], "consumer discretionary — retail, autos, leisure", "stocks"),
    "consumer_staples": (["WMT","COST","PG","KO","PEP","MDLZ","CL","KMB","GIS","MO","PM","KHC","STZ","KDP","HSY","SYY","ADM","CLX"], "defensive consumer staples", "stocks"),
    "industrials": (["CAT","DE","GE","HON","BA","LMT","RTX","UPS","UNP","MMM","EMR","ETN","ITW","CSX","NSC","GD","NOC","PH","ROK","FDX"], "industrials — machinery, transport, aerospace", "stocks"),
    "communication": (["GOOGL","META","NFLX","DIS","CMCSA","T","VZ","TMUS","WBD","SPOT","EA","TTWO","RBLX","PINS","SNAP","MTCH","LYV"], "communication services — media, telecom, social", "stocks"),
    "retail": (["WMT","COST","TGT","HD","LOW","TJX","ROST","DG","DLTR","BBY","ULTA","KR","DKS","FIVE","BURL"], "retailers — big-box, specialty & grocery", "stocks"),
    "reits": (["PLD","AMT","EQIX","SPG","O","PSA","CCI","WELL","DLR","VICI","EXR","AVB","EQR","SBAC","INVH","ARE","MAA","KIM"], "real-estate investment trusts", "stocks"),
    "defense_aerospace": (["LMT","RTX","NOC","GD","BA","LHX","HII","TDG","AXON","HWM","LDOS","TXT","CW","KTOS","BWXT"], "defense & aerospace primes and suppliers", "stocks"),
    "travel_leisure": (["BKNG","ABNB","MAR","HLT","DAL","UAL","AAL","LUV","CCL","RCL","NCLH","LVS","MGM","WYNN","DKNG","EXPE","CZR"], "airlines, hotels, cruises, gaming & travel", "stocks"),
    "china_adr": (["BABA","PDD","JD","BIDU","NIO","LI","XPEV","TCOM","BILI","NTES","TME","YUMC","BEKE","ZTO","FUTU"], "US-listed Chinese ADRs", "stocks"),
    "index_etfs": (["SPY","QQQ","IWM","DIA","VTI","VOO","MDY","RSP","QQQM","SCHD","VUG","VTV"], "broad-market & style index ETFs", "etfs"),
}

THEME_LABEL = {k: k.replace("_", " ").title() for k in THEMES}

# ── shared indicator helpers injected into every bot ────────────────────────
INDICATORS = '''
def _rsi(c, p=14):
    import numpy as np
    if len(c) < p + 1:
        return 50.0
    d = np.diff(c)
    up = np.where(d > 0, d, 0.0)[-p:].mean()
    dn = np.where(d < 0, -d, 0.0)[-p:].mean()
    if dn == 0:
        return 100.0
    return float(100 - 100 / (1 + up / dn))

def _ema(c, span):
    import numpy as np
    c = np.asarray(c, dtype=float)
    k = 2.0 / (span + 1)
    out = np.zeros_like(c)
    out[0] = c[0]
    for i in range(1, len(c)):
        out[i] = c[i] * k + out[i - 1] * (1 - k)
    return out

def _macd(c):
    import numpy as np
    if len(c) < 26:
        return 0.0, 0.0
    line = _ema(c, 12) - _ema(c, 26)
    sig = _ema(line, 9)
    return float(line[-1]), float(sig[-1])

def _qty(account, price, frac=0.2):
    cash = float((account or {}).get("cash", 0) or 0)
    if price <= 0 or cash <= 0:
        return 0
    return int((cash * frac) / price)
'''

# ── 5 ML strategy decide() bodies (pure indicators, no LLM) ─────────────────
ML_STRATEGIES = {
    "momentum_surge": {
        "label": "Momentum Surge",
        "tier": 3,
        "desc": "ML-only momentum: buys 20-day breakouts confirmed by a volume spike, rides the trend, exits on a 10-day low or fading momentum.",
        "method": "Enter when price prints a new 20-day high with 5-day return >2% and volume >1.4x the 20-day average. Exit on a 10-day low breach or 5-day return <-3%.",
        "body": '''
    import numpy as np
    if len(bars) < 30:
        return {"action": "HOLD", "reason": "need 30+ bars"}
    close = bars["Close"].values.astype(float); vol = bars["Volume"].values.astype(float)
    price = float(close[-1]); high20 = float(np.max(close[-21:-1]))
    ret5 = (price / close[-6] - 1) * 100 if len(close) > 6 else 0.0
    vavg = float(np.mean(vol[-20:])) if len(vol) >= 20 else float(np.mean(vol) or 1)
    vspike = (vol[-1] / vavg) if vavg > 0 else 1.0
    held = abs(float(position.get("qty", 0) or 0)) > 1e-9
    if not held:
        if price >= high20 and ret5 > 2 and vspike > 1.4:
            conf = min(1.0, 0.55 + ret5 / 25 + (vspike - 1.4) / 4)
            q = _qty(account, price, 0.2)
            if q < 1:
                return {"action": "HOLD", "reason": "position would be < 1 share"}
            return {"action": "BUY", "qty": q, "confidence": round(conf, 2),
                    "reason": f"breakout {price:.2f} >= 20d-high {high20:.2f} | 5d {ret5:+.1f}% | vol x{vspike:.1f}"}
        return {"action": "HOLD", "reason": f"no breakout (5d {ret5:+.1f}%, vol x{vspike:.1f})"}
    low10 = float(np.min(close[-11:-1]))
    if price <= low10 or ret5 < -3:
        return {"action": "SELL", "qty": abs(float(position.get("qty", 0))), "confidence": 0.8,
                "reason": f"exit: price {price:.2f} <= 10d-low {low10:.2f} or momentum fade ({ret5:+.1f}%)"}
    return {"action": "HOLD", "reason": f"riding momentum (5d {ret5:+.1f}%)"}
''',
    },
    "bollinger_reversion": {
        "label": "Bollinger Reversion",
        "tier": 1,
        "desc": "ML-only mean-reversion: buys when price closes below the lower Bollinger band (oversold) and sells back at the middle band.",
        "method": "Compute 20-day Bollinger bands (2 sigma). BUY when price < lower band. SELL when price >= middle band (the 20-day mean).",
        "body": '''
    import numpy as np
    if len(bars) < 25:
        return {"action": "HOLD", "reason": "need 25+ bars"}
    close = bars["Close"].values.astype(float); w = close[-20:]
    mid = float(w.mean()); sd = float(w.std()); price = float(close[-1])
    lower = mid - 2 * sd; upper = mid + 2 * sd
    held = abs(float(position.get("qty", 0) or 0)) > 1e-9
    if not held:
        if price < lower and sd > 0:
            conf = min(1.0, 0.55 + (lower - price) / (2 * sd))
            q = _qty(account, price, 0.2)
            if q < 1:
                return {"action": "HOLD", "reason": "position would be < 1 share"}
            return {"action": "BUY", "qty": q, "confidence": round(conf, 2),
                    "reason": f"oversold {price:.2f} < lower band {lower:.2f} (mid {mid:.2f})"}
        return {"action": "HOLD", "reason": f"in band (price {price:.2f}, lower {lower:.2f})"}
    if price >= mid:
        return {"action": "SELL", "qty": abs(float(position.get("qty", 0))), "confidence": 0.75,
                "reason": f"reverted to mean: {price:.2f} >= mid {mid:.2f}"}
    return {"action": "HOLD", "reason": f"holding for mean-reversion ({price:.2f} -> {mid:.2f})"}
''',
    },
    "rsi_dip_buyer": {
        "label": "RSI Dip Buyer",
        "tier": 1,
        "desc": "ML-only: buys oversold dips (RSI < 35) and sells into strength (RSI > 65). Classic momentum-oscillator swing.",
        "method": "14-period RSI. BUY when RSI crosses below 35 (oversold). SELL when RSI rises above 65 (overbought).",
        "body": '''
    import numpy as np
    if len(bars) < 20:
        return {"action": "HOLD", "reason": "need 20+ bars"}
    close = bars["Close"].values.astype(float); price = float(close[-1])
    rsi = _rsi(close, 14)
    held = abs(float(position.get("qty", 0) or 0)) > 1e-9
    if not held:
        if rsi < 35:
            conf = min(1.0, 0.55 + (35 - rsi) / 50)
            q = _qty(account, price, 0.2)
            if q < 1:
                return {"action": "HOLD", "reason": "position would be < 1 share"}
            return {"action": "BUY", "qty": q, "confidence": round(conf, 2),
                    "reason": f"oversold RSI {rsi:.0f} < 35 @ {price:.2f}"}
        return {"action": "HOLD", "reason": f"RSI {rsi:.0f} not oversold"}
    if rsi > 65:
        return {"action": "SELL", "qty": abs(float(position.get("qty", 0))), "confidence": 0.75,
                "reason": f"overbought RSI {rsi:.0f} > 65 @ {price:.2f}"}
    return {"action": "HOLD", "reason": f"holding (RSI {rsi:.0f})"}
''',
    },
    "macd_trend_rider": {
        "label": "MACD Trend Rider",
        "tier": 2,
        "desc": "ML-only trend follower: enters on a bullish MACD crossover and exits when MACD crosses back below its signal.",
        "method": "12/26/9 MACD. BUY when the MACD line crosses above the signal line. SELL when it crosses below.",
        "body": '''
    import numpy as np
    if len(bars) < 35:
        return {"action": "HOLD", "reason": "need 35+ bars"}
    close = bars["Close"].values.astype(float); price = float(close[-1])
    line, sig = _macd(close); prev_line, prev_sig = _macd(close[:-1])
    held = abs(float(position.get("qty", 0) or 0)) > 1e-9
    bull_cross = prev_line <= prev_sig and line > sig
    bear_cross = prev_line >= prev_sig and line < sig
    if not held:
        if bull_cross and price > 0:
            conf = min(1.0, 0.55 + abs(line - sig) / (abs(price) * 0.01 + 1e-9))
            conf = min(conf, 0.95)
            q = _qty(account, price, 0.2)
            if q < 1:
                return {"action": "HOLD", "reason": "position would be < 1 share"}
            return {"action": "BUY", "qty": q, "confidence": round(conf, 2),
                    "reason": f"bullish MACD cross ({line:.2f} > {sig:.2f}) @ {price:.2f}"}
        return {"action": "HOLD", "reason": f"no bull cross (MACD {line:.2f} vs {sig:.2f})"}
    if bear_cross:
        return {"action": "SELL", "qty": abs(float(position.get("qty", 0))), "confidence": 0.78,
                "reason": f"bearish MACD cross ({line:.2f} < {sig:.2f}) @ {price:.2f}"}
    return {"action": "HOLD", "reason": f"trend intact (MACD {line:.2f} vs {sig:.2f})"}
''',
    },
    "donchian_breakout": {
        "label": "Donchian Breakout",
        "tier": 2,
        "desc": "ML-only channel breakout: buys a 20-day high breakout and trails the exit at the 10-day low (turtle-style).",
        "method": "Donchian channels. BUY when price makes a new 20-day high. EXIT (SELL) when price makes a new 10-day low.",
        "body": '''
    import numpy as np
    if len(bars) < 25:
        return {"action": "HOLD", "reason": "need 25+ bars"}
    close = bars["Close"].values.astype(float); high = bars["High"].values.astype(float)
    low = bars["Low"].values.astype(float); price = float(close[-1])
    hi20 = float(np.max(high[-21:-1])); lo10 = float(np.min(low[-11:-1]))
    held = abs(float(position.get("qty", 0) or 0)) > 1e-9
    if not held:
        if price >= hi20:
            conf = min(1.0, 0.6 + (price / hi20 - 1) * 8)
            q = _qty(account, price, 0.2)
            if q < 1:
                return {"action": "HOLD", "reason": "position would be < 1 share"}
            return {"action": "BUY", "qty": q, "confidence": round(conf, 2),
                    "reason": f"20d breakout {price:.2f} >= {hi20:.2f}"}
        return {"action": "HOLD", "reason": f"below channel top ({price:.2f} < {hi20:.2f})"}
    if price <= lo10:
        return {"action": "SELL", "qty": abs(float(position.get("qty", 0))), "confidence": 0.8,
                "reason": f"trailing stop hit: {price:.2f} <= 10d-low {lo10:.2f}"}
    return {"action": "HOLD", "reason": f"in trend ({price:.2f}, stop {lo10:.2f})"}
''',
    },
}

# ── AI-driven decide() (calls Claude; themed by the strategy thesis) ─────────
AI_BODY = '''
    import os, json, numpy as np
    if len(bars) < 20:
        return {{"action": "HOLD", "reason": "need 20+ bars"}}
    close = bars["Close"].values.astype(float); price = float(close[-1])
    ret5 = (price / close[-6] - 1) * 100 if len(close) > 6 else 0.0
    ret20 = (price / close[-21] - 1) * 100 if len(close) > 21 else 0.0
    rsi = _rsi(close, 14)
    held = abs(float(position.get("qty", 0) or 0)) > 1e-9
    prompt = (
        "You are a disciplined {thesis} trader. Decide ONE action for this stock.\\n"
        f"Symbol {{symbol}} | price ${{price:.2f}} | 5d {{ret5:+.1f}}% | 20d {{ret20:+.1f}}% | RSI {{rsi:.0f}}\\n"
        f"Currently {{'HOLDING' if held else 'FLAT'}}.\\n"
        "Return ONLY JSON: {{{{\\"action\\":\\"BUY|SELL|HOLD\\",\\"confidence\\":0.0-1.0,\\"reason\\":\\"one line\\"}}}}\\n"
        "BUY only if flat and the {thesis} setup is clearly present; SELL only if holding and the thesis has broken."
    )
    try:
        # Provider-agnostic: uses whichever AI the user configured (Claude,
        # GPT, Groq or Gemini) via APEX's shared AI client.
        from core.ai_client import load_ai_config, call_ai_text
        prov, model, akey, _mode = load_ai_config()
        if not akey:
            return {{"action": "HOLD", "reason": "no AI key configured (set one in Tools)"}}
        raw = call_ai_text(prompt, prov, model, akey, 150)
        raw = raw.replace("```json", "").replace("```", "").strip()
        sig = json.loads(raw)
    except Exception as e:
        return {{"action": "HOLD", "reason": f"AI call failed: {{e}}"}}
    act = str(sig.get("action", "HOLD")).upper()
    conf = float(sig.get("confidence", 0) or 0)
    reason = str(sig.get("reason", ""))[:120]
    if act == "BUY" and not held:
        q = _qty(account, price, 0.2)
        if q < 1:
            return {{"action": "HOLD", "reason": "position would be < 1 share"}}
        return {{"action": "BUY", "qty": q, "confidence": conf, "reason": f"AI: {{reason}}"}}
    if act == "SELL" and held:
        return {{"action": "SELL", "qty": abs(float(position.get("qty", 0))), "confidence": conf, "reason": f"AI: {{reason}}"}}
    return {{"action": "HOLD", "reason": f"AI HOLD: {{reason}}"}}
'''


def _price_credits(is_ai: bool, tier: int, theme_idx: int) -> int:
    """Spread prices across 49–999 credits ($0.49–$9.99)."""
    if is_ai:
        return max(499, min(999, 499 + theme_idx * 26))          # premium tier
    base = {1: 49, 2: 199, 3: 299}.get(tier, 99)
    return max(49, min(499, base + theme_idx * 9 + (tier - 1) * 20))


def _bot_source(*, name, slug, desc, method, ai_used, asset_type, decide_body,
                symbols, theme_label, strat_label) -> str:
    meta = (
        '"""\nAPEX-BOT-META\n'
        f"name:               {name}\n"
        f"description:        {desc}\n"
        f"method:             {method}\n"
        f"ai_used:            {ai_used}\n"
        f"compatible_models:  {'anthropic, openai, groq, gemini' if ai_used != 'none' else 'none'}\n"
        f"asset_type:         {asset_type}\n"
        f"brokers:            alpaca, ibkr\n"
        f"universe:           {slug}_universe.txt\n"
        "requirements:       \n"
        '"""\n'
    )
    return (
        meta
        + "import os\nimport pandas as pd\nimport numpy as np\n"
        + "from core.bot_framework import BotRunner\n"
        + INDICATORS
        + "\n\ndef decide(symbol, bars, position, account):\n"
        + f'    """{strat_label} on {theme_label}."""'
        + decide_body
        + "\n\nif __name__ == \"__main__\":\n"
        + "    BotRunner(\n"
        + f"        asset_type={asset_type!r},\n"
        + f"        default_symbols={symbols!r},\n"
        + f"        universe_path=os.environ.get('APEX_BOT_UNIVERSE', {slug + '_universe.txt'!r}),\n"
        + "        tick_seconds=300,\n        bar_period='6mo',\n        bar_interval='1d',\n"
        + f"        name={slug!r},\n"
        + "    ).run(decide)\n"
    )


def main():
    catalog = []
    i = 0
    for strat_key, strat in ML_STRATEGIES.items():
        for theme_idx, (theme_key, (symbols, theme_desc, asset_type)) in enumerate(THEMES.items()):
            is_ai = (i % 10) < 3                      # ~30 of 100 are AI-driven
            theme_label = THEME_LABEL[theme_key]
            strat_label = strat["label"]
            if is_ai:
                name = f"AI {strat_label} — {theme_label}"
                slug = f"ai_{strat_key}_{theme_key}"
                desc = (f"AI-driven {strat_label.lower()}: Claude judges each {theme_desc} "
                        f"name every cycle for a {strat_key.split('_')[0]} setup. Needs your Anthropic key.")[:200]
                ai_used = "anthropic"
                body = AI_BODY.format(thesis=strat_label.lower())
                tags = [strat_key, theme_key, asset_type, "ai-driven"]
            else:
                name = f"{strat_label} — {theme_label}"
                slug = f"{strat_key}_{theme_key}"
                desc = ("ML-only " + strat["desc"].split("ML-only ")[-1] + f" Universe: {theme_desc}.")[:200]
                ai_used = "none"
                body = strat["body"]
                tags = [strat_key, theme_key, asset_type, "ml-only"]
            price = _price_credits(is_ai, strat["tier"], theme_idx)
            src = _bot_source(name=name, slug=slug, desc=desc, method=strat["method"],
                              ai_used=ai_used, asset_type=asset_type, decide_body=body,
                              symbols=symbols, theme_label=theme_label, strat_label=strat_label)
            (OUT / f"{slug}.py").write_text(src, encoding="utf-8")
            catalog.append({"slug": slug, "name": name, "description": desc,
                            "tags": tags, "price_credits": price, "ai_used": ai_used,
                            "asset_type": asset_type, "strategy": strat_key, "theme": theme_key})
            i += 1
    (OUT / "catalog.json").write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    print(f"generated {len(catalog)} bots into {OUT}")
    print(f"  ML-only: {sum(1 for c in catalog if c['ai_used'] == 'none')}  "
          f"AI-driven: {sum(1 for c in catalog if c['ai_used'] != 'none')}")
    prices = [c["price_credits"] for c in catalog]
    print(f"  price range: {min(prices)}–{max(prices)} credits "
          f"(${min(prices)/100:.2f}–${max(prices)/100:.2f})")


if __name__ == "__main__":
    main()
