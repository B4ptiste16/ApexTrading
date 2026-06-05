"""
APEX server-side UNIVERSE FACTORY  (V4.6.74)
─────────────────────────────────────────────────────────────────
Generates a large set of THEMED public universes on the Oracle server, NOT
tied to any user account. Bots (created in Make Bot) reference one of these by
name, or ship their own custom universe. Regenerated weekly (Monday at the US
market open) by the auto-scheduler, or on demand via POST /admin/universes/regenerate.

Each universe is written in the SAME format the Universe tab uses:
    TICKER    # score=NN.N | <one-line reason>
so the desktop can read/display them like any other universe.

Two kinds of themes:
  • CROSS-CUTTING factor themes — scored across the whole liquid base
    (long_term, short, short_term, speculative, options, mega_cap,
     dividend_income, high_growth, low_volatility, momentum_leaders, meme).
  • SECTOR / INDUSTRY themes — a curated membership list per sector, then
    ranked by quality-momentum (semiconductors, ai_infrastructure,
    software_cloud, cybersecurity, fintech_payments, banks, ev_clean_energy,
    energy_oil, healthcare, biotech, consumer_discretionary, consumer_staples,
    industrials, communication, retail, reits, defense_aerospace,
    travel_leisure, china_adr, index_etfs, sector_etfs, crypto).

Scoring uses 3-month daily bars from yfinance (free, no account). Best-effort:
a ticker that can't be priced is skipped; a theme with no data keeps its
previous file.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

# Central, account-agnostic location for the public universes.
UNIVERSE_DIR = Path(os.environ.get("APEX_PUBLIC_UNIVERSE_DIR",
                                   "/opt/apex_data/universes"))

_PER_THEME = 35   # tickers emitted per cross-cutting stock theme

# ── Curated SECTOR / INDUSTRY membership lists ────────────────────────────
# Each is scored against its own members (not the whole market) so the
# universe stays on-theme. Lists are deliberately broad-but-liquid.
_SECTORS: dict[str, tuple[list, str]] = {
    "semiconductors": ([
        "NVDA", "AMD", "INTC", "MU", "QCOM", "AVGO", "TXN", "ADI", "LRCX",
        "KLAC", "AMAT", "ASML", "TSM", "MRVL", "ON", "MCHP", "NXPI", "STM",
        "GFS", "WOLF", "ARM", "SMCI", "QRVO", "SWKS", "TER", "ENTG", "LSCC",
        "ALGM", "AMKR", "UMC",
    ], "Chipmakers & semiconductor equipment."),
    "ai_infrastructure": ([
        "NVDA", "AVGO", "AMD", "TSM", "ASML", "ARM", "MU", "MRVL", "SMCI",
        "DELL", "VRT", "ANET", "CRDO", "MPWR", "CIEN", "COHR", "PSTG", "NBIS",
        "ORCL", "GOOGL", "MSFT", "META", "PLTR", "SNOW", "CRWV",
    ], "Picks-and-shovels for the AI build-out (compute, networking, power)."),
    "software_cloud": ([
        "MSFT", "CRM", "ADBE", "ORCL", "NOW", "SNOW", "DDOG", "MDB", "NET",
        "TEAM", "WDAY", "HUBS", "ZS", "OKTA", "PANW", "CRWD", "FTNT", "INTU",
        "SHOP", "TWLO", "ZM", "DOCU", "PATH", "GTLB", "S", "SNPS", "CDNS",
        "ADSK", "BILL", "APP",
    ], "Cloud & enterprise software (SaaS)."),
    "cybersecurity": ([
        "CRWD", "PANW", "ZS", "FTNT", "OKTA", "S", "NET", "CYBR", "RPD",
        "QLYS", "TENB", "VRNS", "CHKP", "GEN", "AKAM",
    ], "Cybersecurity pure-plays."),
    "fintech_payments": ([
        "V", "MA", "PYPL", "XYZ", "AXP", "FI", "GPN", "COIN", "HOOD", "SOFI",
        "AFRM", "NU", "TOST", "DLO", "FOUR", "WU", "PAGS", "STNE", "UPST",
    ], "Payments, fintech & digital banking."),
    "banks": ([
        "JPM", "BAC", "WFC", "C", "GS", "MS", "USB", "PNC", "TFC", "SCHW",
        "COF", "BK", "TD", "RY", "HSBC", "ALLY", "RF", "FITB", "KEY", "CFG",
    ], "Money-center & regional banks."),
    "ev_clean_energy": ([
        "TSLA", "RIVN", "LCID", "NIO", "LI", "XPEV", "ENPH", "FSLR", "SEDG",
        "RUN", "PLUG", "CHPT", "BE", "ALB", "FREY", "NEE", "BEP", "ARRY",
    ], "Electric vehicles & clean-energy."),
    "energy_oil": ([
        "XOM", "CVX", "COP", "SLB", "EOG", "OXY", "PSX", "MPC", "VLO", "HAL",
        "DVN", "FANG", "KMI", "WMB", "TRGP", "BKR", "OKE", "EQT", "CTRA", "APA",
    ], "Oil, gas & energy services."),
    "healthcare": ([
        "UNH", "LLY", "JNJ", "PFE", "MRK", "ABBV", "BMY", "AMGN", "GILD",
        "CVS", "TMO", "DHR", "ISRG", "MDT", "ABT", "ELV", "CI", "HCA", "SYK",
        "BSX", "ZTS", "VRTX", "REGN",
    ], "Large-cap healthcare, pharma & med-tech."),
    "biotech": ([
        "MRNA", "BNTX", "VRTX", "REGN", "BIIB", "ALNY", "EXEL", "INCY",
        "NBIX", "BMRN", "SRPT", "IONS", "UTHR", "HALO", "ARWR", "CRSP",
        "NTLA", "BEAM", "VKTX", "RXRX",
    ], "Biotech — clinical & commercial."),
    "consumer_discretionary": ([
        "AMZN", "HD", "NKE", "MCD", "SBUX", "LOW", "TGT", "BKNG", "CMG",
        "MAR", "GM", "F", "ORLY", "AZO", "ROST", "YUM", "DHI", "LEN", "EBAY",
        "LULU", "DRI", "RCL",
    ], "Consumer discretionary — retail, autos, leisure."),
    "consumer_staples": ([
        "WMT", "COST", "PG", "KO", "PEP", "MDLZ", "CL", "KMB", "GIS", "MO",
        "PM", "KHC", "STZ", "KDP", "HSY", "SYY", "ADM", "K", "CLX", "CHD",
    ], "Defensive consumer staples."),
    "industrials": ([
        "CAT", "DE", "GE", "HON", "BA", "LMT", "RTX", "UPS", "UNP", "MMM",
        "EMR", "ETN", "ITW", "CSX", "NSC", "GD", "NOC", "PH", "ROK", "FDX",
    ], "Industrials — machinery, transport, aerospace."),
    "communication": ([
        "GOOGL", "META", "NFLX", "DIS", "CMCSA", "T", "VZ", "TMUS", "WBD",
        "SPOT", "EA", "TTWO", "RBLX", "PINS", "SNAP", "MTCH", "OMC", "LYV",
    ], "Communication services — media, telecom, social."),
    "retail": ([
        "WMT", "COST", "TGT", "HD", "LOW", "TJX", "ROST", "DG", "DLTR",
        "BBY", "ULTA", "KR", "DKS", "FIVE", "BURL", "M", "GAP", "ANF",
    ], "Retailers — big-box, specialty & grocery."),
    "reits": ([
        "PLD", "AMT", "EQIX", "SPG", "O", "PSA", "CCI", "WELL", "DLR", "VICI",
        "EXR", "AVB", "EQR", "SBAC", "INVH", "ARE", "MAA", "KIM",
    ], "Real-estate investment trusts."),
    "defense_aerospace": ([
        "LMT", "RTX", "NOC", "GD", "BA", "LHX", "HII", "TDG", "AXON", "HWM",
        "LDOS", "TXT", "CW", "KTOS", "BWXT",
    ], "Defense & aerospace primes and suppliers."),
    "travel_leisure": ([
        "BKNG", "ABNB", "MAR", "HLT", "DAL", "UAL", "AAL", "LUV", "CCL",
        "RCL", "NCLH", "LVS", "MGM", "WYNN", "DKNG", "EXPE", "H", "CZR",
    ], "Airlines, hotels, cruises, gaming & travel."),
    "china_adr": ([
        "BABA", "PDD", "JD", "BIDU", "NIO", "LI", "XPEV", "TCOM", "BILI",
        "NTES", "TME", "YUMC", "BEKE", "ZTO", "FUTU", "TAL", "EDU",
    ], "US-listed Chinese ADRs."),
    "index_etfs": ([
        "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "MDY", "RSP", "QQQM",
        "SCHD", "VUG", "VTV",
    ], "Broad-market & style index ETFs."),
    "sector_etfs": ([
        "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB",
        "XLRE", "XLC", "SMH", "SOXX", "XBI", "KRE", "XOP", "ITB", "JETS",
    ], "SPDR/iShares sector & industry ETFs."),
}

# ── Cross-cutting liquid base (scored as a whole for the factor themes) ────
# Built from the union of every sector list + a few extra movers so the
# factor themes draw from a deep, deduplicated pool.
_EXTRA_BASE = [
    "AAPL", "BRK-B", "WMT", "JPM", "AVGO", "PLTR", "UBER", "ABNB", "DELL",
    "MARA", "RIOT", "CVNA", "GME", "AMC", "AI", "U", "ROKU", "DASH", "RDDT",
    "HOOD", "SOFI", "AFRM", "DKNG", "CELH", "ELF", "SMCI", "VST", "CEG",
]


def _all_base_stocks() -> list:
    seen = []
    pool = list(_EXTRA_BASE)
    for tickers, _blurb in _SECTORS.values():
        # ETF-only sectors still get priced; that's fine for the factor pool.
        pool.extend(tickers)
    for t in pool:
        if t not in seen:
            seen.append(t)
    return seen


_BASE_CRYPTO = [
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "ADA-USD", "AVAX-USD",
    "DOGE-USD", "LINK-USD", "DOT-USD", "LTC-USD", "BCH-USD", "POL-USD",
    "UNI-USD", "ATOM-USD", "ETC-USD", "XLM-USD", "NEAR-USD", "APT-USD",
    "FIL-USD", "ARB-USD",
]


def _metrics(tickers: list) -> dict:
    """3-month momentum % + annualised volatility % + last price per ticker.
    Downloads in chunks of 50 so a large universe doesn't trip yfinance."""
    try:
        import yfinance as yf
        import numpy as np  # noqa: F401  (kept for parity / future use)
    except Exception as e:
        print(f"[universe-factory] yfinance/numpy missing: {e}", flush=True)
        return {}
    out: dict = {}
    CHUNK = 50
    for i in range(0, len(tickers), CHUNK):
        batch = tickers[i:i + CHUNK]
        try:
            data = yf.download(batch, period="3mo", interval="1d",
                               progress=False, group_by="ticker", threads=True)
        except Exception as e:
            print(f"[universe-factory] download chunk {i} failed: {e}",
                  flush=True)
            continue
        multi = len(batch) > 1
        for t in batch:
            try:
                close = (data[t]["Close"].dropna() if multi
                         else data["Close"].dropna())
                if len(close) < 20:
                    continue
                mom = float(close.iloc[-1] / close.iloc[0] - 1.0) * 100.0
                rets = close.pct_change().dropna()
                vol = float(rets.std() * (252 ** 0.5) * 100.0)
                out[t] = {"mom": mom, "vol": vol,
                          "price": float(close.iloc[-1])}
            except Exception:
                continue
    return out


def _emit(theme: str, ranked: list, blurb: str) -> int:
    """Write one themed universe file (TICKER  # score=.. | reason)."""
    if not ranked:
        print(f"[universe-factory] {theme}: no data, keeping previous file",
              flush=True)
        return 0
    try:
        UNIVERSE_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            f"# APEX PUBLIC UNIVERSE — {theme}\n",
            f"# {blurb}\n",
            f"# Generated by universe_factory: {now}\n",
            f"# Total: {len(ranked)} tickers\n#\n",
        ]
        for t, score, reason in ranked:
            lines.append(f"{t:<10}  # score={score:.1f} | {reason}\n")
        (UNIVERSE_DIR / f"{theme}.txt").write_text("".join(lines),
                                                   encoding="utf-8")
        print(f"[universe-factory] wrote {theme}: {len(ranked)} tickers",
              flush=True)
        return len(ranked)
    except Exception as e:
        print(f"[universe-factory] emit {theme} failed: {e}", flush=True)
        return 0


def _rank(metrics: dict, scorer, reason_fn, members=None, n=_PER_THEME) -> list:
    """Rank `members` (or all of metrics) by scorer, descending."""
    pool = members if members is not None else list(metrics.keys())
    scored = [(t, scorer(metrics[t]), reason_fn(metrics[t]))
              for t in pool if t in metrics]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:n]


def generate_all() -> dict:
    """Regenerate every themed public universe. Returns {theme: count}."""
    counts: dict = {}

    base = _all_base_stocks()
    m = _metrics(base)

    # Common reason formatters
    def r_momvol(v):
        return f"3mo {v['mom']:+.0f}%, vol {v['vol']:.0f}%"

    def r_volprice(v):
        return f"vol {v['vol']:.0f}%, ${v['price']:.0f}"

    if m:
        # ── Cross-cutting factor themes (scored across the whole base) ──
        counts["long_term"] = _emit(
            "long_term",
            _rank(m, lambda v: v["mom"] - 0.30 * v["vol"], r_momvol),
            "Durable momentum, volatility-penalised — buy & hold.")
        counts["short"] = _emit(
            "short",
            _rank(m, lambda v: -v["mom"] - 0.10 * v["vol"], r_momvol),
            "Weakest momentum — short candidates.")
        counts["short_term"] = _emit(
            "short_term",
            _rank(m, lambda v: v["vol"] + abs(v["mom"]), r_momvol),
            "Most active / high churn — intraday & swing.")
        counts["speculative"] = _emit(
            "speculative",
            _rank(m, lambda v: v["vol"], r_momvol),
            "Highest volatility — high risk / high reward.")
        counts["options"] = _emit(
            "options",
            _rank(m, lambda v: v["vol"] if v["price"] > 15 else -1, r_volprice),
            "Liquid, high-volatility optionable names.")
        counts["momentum_leaders"] = _emit(
            "momentum_leaders",
            _rank(m, lambda v: v["mom"], r_momvol),
            "Strongest 3-month price momentum.")
        counts["mega_cap"] = _emit(
            "mega_cap",
            _rank(m, lambda v: v["mom"] - 0.15 * v["vol"], r_momvol,
                  members=[t for t in (
                      "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO",
                      "TSLA", "BRK-B", "LLY", "JPM", "V", "UNH", "XOM", "WMT",
                      "MA", "JNJ", "ORCL", "HD", "PG", "COST", "NFLX", "CVX",
                      "ABBV", "KO", "ADBE", "CRM", "AMD") if t in m]),
            "Mega-cap leaders, quality-momentum ranked.")
        counts["high_growth"] = _emit(
            "high_growth",
            _rank(m, lambda v: v["mom"] + 0.10 * v["vol"], r_momvol,
                  members=[t for t in (
                      "PLTR", "SNOW", "NET", "DDOG", "CRWD", "MDB", "ABNB",
                      "SHOP", "UBER", "DASH", "RDDT", "APP", "TOST", "AFRM",
                      "SOFI", "HOOD", "U", "AI", "PATH", "GTLB", "CELH",
                      "ELF", "NU", "DLO") if t in m]),
            "High-growth disruptors — momentum-weighted.")
        counts["low_volatility"] = _emit(
            "low_volatility",
            _rank(m, lambda v: -v["vol"] + 0.10 * v["mom"], r_momvol,
                  members=[t for t in (
                      "PG", "KO", "JNJ", "WMT", "COST", "PEP", "MCD", "MRK",
                      "ABBV", "CL", "KMB", "MDLZ", "GIS", "MO", "PM", "KHC",
                      "DUK", "SO", "NEE", "VZ", "T", "O", "WELL") if t in m]),
            "Defensive, low-volatility compounders.")
        counts["dividend_income"] = _emit(
            "dividend_income",
            _rank(m, lambda v: -v["vol"], r_momvol,
                  members=[t for t in (
                      "JNJ", "PG", "KO", "PEP", "XOM", "CVX", "MMM", "ABBV",
                      "MCD", "T", "VZ", "O", "MO", "PM", "KMB", "CL", "IBM",
                      "MRK", "PFE", "KMI", "WMB", "OKE", "DOW") if t in m]),
            "Income / dividend stalwarts (low-vol tilt).")
        counts["meme"] = _emit(
            "meme",
            _rank(m, lambda v: v["vol"] + abs(v["mom"]), r_momvol,
                  members=[t for t in (
                      "GME", "AMC", "PLTR", "RIVN", "LCID", "SOFI", "HOOD",
                      "DKNG", "RBLX", "CVNA", "MARA", "RIOT", "COIN", "AFRM",
                      "TSLA", "NIO", "SNAP", "U", "RDDT") if t in m]),
            "Retail-favourite high-beta movers.")

        # ── Sector / industry membership themes ──
        for theme, (members, blurb) in _SECTORS.items():
            counts[theme] = _emit(
                theme,
                _rank(m, lambda v: v["mom"] - 0.20 * v["vol"], r_momvol,
                      members=[t for t in members if t in m]),
                blurb)

    # ── Crypto ──
    mc = _metrics(_BASE_CRYPTO)
    if mc:
        counts["crypto"] = _emit(
            "crypto",
            _rank(mc, lambda v: v["mom"],
                  lambda v: f"3mo {v['mom']:+.0f}%, vol {v['vol']:.0f}%",
                  n=len(mc)),
            "Major coins by 3-month momentum.")
        counts["crypto_majors"] = _emit(
            "crypto_majors",
            _rank(mc, lambda v: v["mom"],
                  lambda v: f"3mo {v['mom']:+.0f}%, vol {v['vol']:.0f}%",
                  members=[c for c in ("BTC-USD", "ETH-USD", "SOL-USD",
                                       "XRP-USD", "ADA-USD", "AVAX-USD",
                                       "DOGE-USD", "LINK-USD", "DOT-USD",
                                       "LTC-USD", "BCH-USD") if c in mc],
                  n=11),
            "Top liquid coins (majors only).")
    return counts


def list_universes() -> list:
    """[{name, total, updated, header}] for every public universe file."""
    out = []
    if not UNIVERSE_DIR.exists():
        return out
    for p in sorted(UNIVERSE_DIR.glob("*.txt")):
        try:
            txt = p.read_text(encoding="utf-8")
            tickers = [ln.split("#")[0].strip()
                       for ln in txt.splitlines()
                       if ln.strip() and not ln.startswith("#")]
            hdr = next((ln[1:].strip() for ln in txt.splitlines()[1:2]), "")
            out.append({"name": p.stem, "total": len(tickers),
                        "blurb": hdr})
        except Exception:
            continue
    return out


def read_universe(name: str) -> str | None:
    p = UNIVERSE_DIR / f"{name}.txt"
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return None
