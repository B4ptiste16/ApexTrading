"""
APEX Charts
Generates Plotly charts as self-contained HTML strings.
Embedded in QWebEngineView widgets inside the Qt app.
"""

import json
import math
import numpy as np
import pandas as pd

# All charts return HTML strings — no Qt imports needed here

G   = "#3fb89a"
R   = "#c75c6b"
OR2 = "#d99a52"
Y   = "#d6c95e"
PU  = "#8a93c9"
BG     = "#0a0d12"
PANEL  = "#10141b"
BORDER = "#222a36"
TEXT   = "#d6dce6"
MUTED  = "#5a6478"

BOT_COLOR = {"LONG": G, "SHORT": R, "DAY": OR2}

PLOTLY_IMPORT = """
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
"""


def _rgb(h):
    h = h.lstrip("#")
    return f"{int(h[0:2],16)},{int(h[2:4],16)},{int(h[4:6],16)}"


def _tight_yrange(values, pad=0.003):
    """Return [min*(1-pad), max*(1+pad)] so small equity moves are visible."""
    clean = [v for v in values if v and v > 0]
    if len(clean) < 2:
        return None
    lo, hi = min(clean), max(clean)
    spread = hi - lo
    if spread < lo * 0.0005:          # nearly flat — give at least 0.1% room
        spread = lo * 0.001
    extra = spread * 0.15
    return [lo - extra, hi + extra]


def _prev_close_annotation(prev_val: float, x_end) -> list:
    """Grey dotted line at the first (prev-close) value + label."""
    if not prev_val or prev_val <= 0:
        return [], []
    shapes = [{
        "type": "line",
        "x0": 0, "x1": 1,
        "y0": prev_val, "y1": prev_val,
        "xref": "paper", "yref": "y",
        "line": {"color": MUTED, "width": 1, "dash": "dot"},
    }]
    annotations = [{
        "x": 0, "y": prev_val,
        "xref": "paper", "yref": "y",
        "text": f"prev  ${prev_val:,.0f}",
        "showarrow": False,
        "font": {"size": 8, "color": MUTED},
        "xanchor": "left",
        "yanchor": "bottom",
    }]
    return shapes, annotations


def _base_layout(height=300, title="", color=TEXT, extra=None):
    layout = {
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor":  "rgba(0,0,0,0)",
        "font":   {"family": "'JetBrains Mono', monospace", "size": 10, "color": TEXT},
        "margin": {"l": 48, "r": 10, "t": 36, "b": 28},
        "height": height,
        "xaxis":  {"gridcolor": BORDER, "zeroline": False, "automargin": False},
        "yaxis":  {"gridcolor": BORDER, "zeroline": False, "automargin": False},
        "title":  {"text": title, "font": {"size": 11, "color": color},
                   "x": 0, "pad": {"l": 0}},
        "template": "plotly_dark",
    }
    if extra:
        layout.update(extra)
    return layout


def _make_html(data: list, layout: dict) -> str:
    """Wrap traces + layout into a self-contained HTML page."""
    fig_json = json.dumps({"data": data, "layout": layout})
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: {BG}; overflow: hidden; }}
</style>
{PLOTLY_IMPORT}
</head>
<body>
<div id="chart" style="width:100%;height:100vh;"></div>
<script>
  var fig = {fig_json};
  Plotly.newPlot('chart', fig.data, fig.layout, {{
    responsive: true,
    displayModeBar: false,
    scrollZoom: false,
  }});
</script>
</body>
</html>"""


def empty_chart(msg="No data", height=280) -> str:
    layout = _base_layout(height)
    layout["annotations"] = [{
        "text": msg, "x": 0.5, "y": 0.5,
        "xref": "paper", "yref": "paper",
        "showarrow": False,
        "font": {"color": MUTED, "size": 13},
    }]
    return _make_html([], layout)


def _market_hours_rangebreaks(period: str) -> list:
    """Plotly x-axis rangebreaks that hide hours / days when no broker
    quote is published, so the equity line draws close-to-close with no
    flat overnight gaps. Skips weekends, plus the 16:00-09:30 ET overnight
    band (= 20:00-13:30 UTC) for intraday-resolution periods.
    The DAILY-resolution periods (3M/6M/1Y) only need the weekend hide."""
    breaks = [{"bounds": ["sat", "mon"]}]
    if period in {"1D", "1W", "1M"}:
        # Hide non-market hours (UTC). Approximate; covers both EDT/EST.
        breaks.append({"bounds": [20, 13.5], "pattern": "hour"})
    return breaks


def equity_curve(equity_df: pd.DataFrame, side: str, period: str) -> str:
    if equity_df.empty:
        return empty_chart(f"No equity data — {period}")
    color = BOT_COLOR[side]
    y = equity_df["equity"].tolist()
    fv, lv = y[0], y[-1]
    c = color if lv >= fv else R
    delta = lv - fv
    pct   = (lv / fv - 1) * 100 if fv else 0

    data = [{
        "type": "scatter",
        "x":    equity_df["time"].astype(str).tolist(),
        "y":    y,
        "mode": "lines",
        "name": "Equity",
        "line": {"width": 2.5, "color": c},
        "fill": "tozeroy",
        "fillcolor": f"rgba({_rgb(c)},0.07)",
        "hovertemplate": "<b>$%{y:,.2f}</b><br>%{x}<extra></extra>",
    }]

    yr = _tight_yrange(y)
    shapes, annots = _prev_close_annotation(fv, equity_df["time"].iloc[-1])
    layout = _base_layout(
        280,
        f"Equity — {period}   ${lv:,.2f}   ({delta:+,.2f} / {pct:+.2f}%)",
        c, {
            "showlegend": False,
            "hovermode": "x unified",
            "shapes": shapes,
            "annotations": annots,
            "xaxis": {"gridcolor": BORDER, "zeroline": False,
                      "automargin": False,
                      "rangebreaks": _market_hours_rangebreaks(period)},
            "yaxis": {"gridcolor": BORDER, "zeroline": False,
                      "automargin": False, "tickprefix": "$",
                      **({"range": yr} if yr else {})},
        })
    return _make_html(data, layout)


def combined_history_chart(df: pd.DataFrame, period: str) -> str:
    """Total portfolio value (all 3 accounts) over the selected period."""
    if df is None or df.empty:
        return empty_chart(f"No portfolio data — {period}")
    y     = df["equity"].tolist()
    fv    = float(y[0])
    lv    = float(y[-1])
    delta = lv - fv
    pct   = (lv / fv - 1) * 100 if fv else 0.0
    c     = G if lv >= fv else R

    data = [{
        "type": "scatter",
        "x":    df["time"].astype(str).tolist(),
        "y":    y,
        "mode": "lines",
        "name": "Total",
        "line": {"width": 2.5, "color": c},
        "fill": "tozeroy",
        "fillcolor": f"rgba({_rgb(c)},0.07)",
        "hovertemplate": "<b>$%{y:,.2f}</b><br>%{x}<extra></extra>",
    }]
    yr = _tight_yrange(y)
    shapes, annots = _prev_close_annotation(fv, df["time"].iloc[-1])
    layout = _base_layout(
        300,
        f"Total Portfolio — {period}   ${lv:,.2f}   ({delta:+,.2f} / {pct:+.2f}%)",
        c, {
            "showlegend": False,
            "hovermode": "x unified",
            "shapes": shapes,
            "annotations": annots,
            "xaxis": {"gridcolor": BORDER, "zeroline": False,
                      "automargin": False,
                      "rangebreaks": _market_hours_rangebreaks(period)},
            "yaxis": {"gridcolor": BORDER, "zeroline": False,
                      "automargin": False, "tickprefix": "$",
                      **({"range": yr} if yr else {})},
        })
    return _make_html(data, layout)


def lifetime_chart(snaps: pd.DataFrame, side: str) -> str:
    if snaps.empty or "portfolio_value" not in snaps.columns:
        return empty_chart("No snapshot data yet")
    color = BOT_COLOR[side]
    pv    = snaps["portfolio_value"].values
    peak  = pd.Series(pv).cummax().tolist()
    ret   = (pv[-1]/pv[0]-1)*100 if pv[0]>0 else 0
    yr    = _tight_yrange(pv.tolist())

    data = [
        {
            "type": "scatter",
            "x": snaps["time"].astype(str).tolist(),
            "y": pv.tolist(),
            "mode": "lines", "name": "Portfolio",
            "line": {"width": 2.5, "color": color},
            "fill": "tozeroy",
            "fillcolor": f"rgba({_rgb(color)},0.08)",
            "hovertemplate": "<b>$%{y:,.2f}</b><br>%{x}<extra></extra>",
        },
        {
            "type": "scatter",
            "x": snaps["time"].astype(str).tolist(),
            "y": peak,
            "mode": "lines", "name": "ATH",
            "line": {"width": 1, "color": MUTED, "dash": "dot"},
            "hovertemplate": "ATH: $%{y:,.2f}<extra></extra>",
        },
    ]
    layout = _base_layout(280, f"Lifetime  {ret:+.2f}%", color, {
        "showlegend": True,
        "legend": {"orientation":"h","y":1.05,"x":0,
                   "bgcolor":"rgba(0,0,0,0)","font":{"size":9}},
        "yaxis": {"gridcolor":BORDER,"zeroline":False,
                  "automargin":False,"tickprefix":"$",
                  **({"range": yr} if yr else {})},
    })
    return _make_html(data, layout)


def drawdown_chart(snaps: pd.DataFrame) -> str:
    if snaps.empty or "portfolio_value" not in snaps.columns:
        return empty_chart("No data", 200)
    pv   = snaps["portfolio_value"].values.astype(float)
    peak = np.maximum.accumulate(pv)
    dd   = (pv - peak) / peak * 100
    mi   = int(dd.argmin())

    data = [{
        "type": "scatter",
        "x": snaps["time"].astype(str).tolist(),
        "y": dd.tolist(),
        "mode": "lines", "name": "DD",
        "line": {"width": 1.8, "color": R},
        "fill": "tozeroy",
        "fillcolor": f"rgba({_rgb(R)},0.15)",
        "hovertemplate": "%{y:.2f}%<br>%{x}<extra></extra>",
    }]
    layout = _base_layout(200, "Drawdown from Peak", R, {
        "annotations": [{
            "x": str(snaps["time"].iloc[mi]),
            "y": float(dd[mi]),
            "text": f"Max: {dd[mi]:.2f}%",
            "showarrow": True,
            "arrowcolor": R,
            "font": {"color": R, "size": 9},
            "bgcolor": PANEL,
            "bordercolor": R,
        }],
        "yaxis": {"gridcolor":BORDER,"ticksuffix":"%",
                  "zeroline":True,"zerolinecolor":MUTED,"automargin":False},
    })
    return _make_html(data, layout)


def bracket_gauge(brackets: dict, positions: list) -> str:
    """
    Day bot: horizontal bracket visualiser.
    Green zone = take profit path, Red zone = stop loss path.
    Diamond = current price.
    """
    if not brackets:
        return empty_chart("No open brackets", 260)

    pos_map = {p["symbol"]: p for p in positions}
    # v1.2.1 — only render brackets whose underlying position still
    # exists on Alpaca. Liquidated positions linger in daybot_state.json
    # until the bot's next sync_brackets_with_alpaca() cycle; without
    # this filter the chart would show them frozen at "+0.0%" (current
    # price falls back to entry when no live position is found).
    brackets = {t: b for t, b in brackets.items() if t in pos_map}
    if not brackets:
        return empty_chart("No open brackets", 260)

    data    = []
    tickers = list(brackets.keys())
    n       = len(tickers)

    for i, ticker in enumerate(tickers):
        b       = brackets[ticker]
        entry   = float(b.get("entry", 0))
        stop    = float(b.get("stop",  0))
        tp      = float(b.get("tp",    0))
        qty     = float(b.get("qty",   0))
        pos     = pos_map.get(ticker, {})
        current = float(pos.get("current_price", entry))

        if entry <= 0:
            continue

        stop_pct = (stop    - entry) / entry * 100
        tp_pct   = (tp      - entry) / entry * 100
        cur_pct  = (current - entry) / entry * 100

        # Red zone
        data.append({
            "type": "bar", "orientation": "h",
            "name": "Stop Loss Zone" if i==0 else None,
            "x": [abs(stop_pct)], "y": [ticker],
            "base": stop_pct,
            "marker": {"color": f"rgba({_rgb(R)},0.22)",
                       "line": {"color": R, "width": 1.5}},
            "showlegend": i==0,
            "legendgroup": "sl",
            "hovertemplate": f"<b>{ticker}</b><br>Stop: ${stop:.2f} ({stop_pct:.1f}%)<extra></extra>",
        })

        # Green zone
        data.append({
            "type": "bar", "orientation": "h",
            "name": "Take Profit Zone" if i==0 else None,
            "x": [tp_pct], "y": [ticker],
            "base": 0,
            "marker": {"color": f"rgba({_rgb(G)},0.22)",
                       "line": {"color": G, "width": 1.5}},
            "showlegend": i==0,
            "legendgroup": "tp",
            "hovertemplate": f"<b>{ticker}</b><br>TP: ${tp:.2f} (+{tp_pct:.1f}%)<extra></extra>",
        })

        # Current price diamond
        mc = G if cur_pct >= 0 else R
        unr = (current - entry) * qty
        data.append({
            "type": "scatter",
            "x": [cur_pct], "y": [ticker],
            "mode": "markers+text",
            "marker": {"size": 14, "color": mc,
                       "symbol": "diamond",
                       "line": {"width": 2, "color": "white"}},
            "text": [f"  ${current:.2f} ({cur_pct:+.1f}%)"],
            "textposition": "middle right",
            "textfont": {"size": 9, "color": mc},
            "showlegend": False,
            "hovertemplate": (
                f"<b>{ticker}</b><br>"
                f"Current: ${current:.2f} ({cur_pct:+.1f}%)<br>"
                f"Entry: ${entry:.2f}<br>"
                f"Qty: {qty:.4f}<br>"
                f"Unrealized: ${unr:+,.2f}<extra></extra>"
            ),
        })

    h = max(200, n*60+80)
    layout = _base_layout(h, "Open Brackets — Live Position", OR2, {
        "barmode": "overlay",
        "shapes": [{"type":"line","x0":0,"x1":0,"y0":-0.5,"y1":n-0.5,
                    "xref":"x","yref":"y",
                    "line":{"color":"white","width":1,"dash":"dot"}}],
        "legend": {"orientation":"h","y":1.05,"x":0,
                   "bgcolor":"rgba(0,0,0,0)","font":{"size":9}},
        "xaxis": {"gridcolor":BORDER,"zeroline":True,"zerolinecolor":MUTED,
                  "zerolinewidth":1.5,"ticksuffix":"%","automargin":False,
                  "title":{"text":"% from entry"}},
        "yaxis": {"gridcolor":BORDER,"automargin":False},
    })
    return _make_html(data, layout)


def position_gauge(positions: list, side: str,
                   meta: dict | None = None) -> str:
    """
    Long/Short bots: shows entry → current → estimated stop/target
    as a horizontal gauge per position.

    `meta` (optional) is {symbol: {stop_pct, tp_pct, atr_pct, ...}} from
    data.position_meta(). When provided, the chart shows per-stock
    targets sized by each ticker's real ATR (so AAPL's target ≠ TSLA's).
    Without it, falls back to a flat 2.5 % ATR estimate.
    """
    if not positions:
        return empty_chart("No open positions", 240)

    color = BOT_COLOR[side]
    data  = []
    n     = len(positions)
    meta  = meta or {}

    for i, p in enumerate(sorted(positions, key=lambda x: x["symbol"])):
        ticker  = p["symbol"]
        entry   = float(p["avg_entry_price"])
        current = float(p.get("current_price", entry))
        unr     = float(p["unrealized_pl"])
        unr_pct = float(p.get("unrealized_plpc", 0)) * 100

        per_tkr = meta.get(ticker)
        if per_tkr:
            stop_pct = float(per_tkr["stop_pct"])
            tp_pct   = float(per_tkr["tp_pct"])
            stop_price = float(per_tkr.get("stop_price", entry * (1 + stop_pct/100)))
            tp_price   = float(per_tkr.get("tp_price",   entry * (1 + tp_pct/100)))
        else:
            # Flat 2.5% ATR fallback if per-ticker meta wasn't supplied
            atr = entry * 0.025
            if side == "SHORT":
                stop_price = entry + atr * 2.5
                tp_price   = entry - atr * 5.0
            else:
                stop_price = entry - atr * 2.5
                tp_price   = entry + atr * 5.0
            stop_pct = (stop_price - entry) / entry * 100
            tp_pct   = (tp_price   - entry) / entry * 100

        cur_pct  = (current    - entry) / entry * 100

        # Stop zone
        if side == "SHORT":
            data.append({
                "type": "bar", "orientation": "h",
                "name": "Stop Zone" if i==0 else None,
                "x": [abs(stop_pct)], "y": [ticker], "base": 0,
                "marker": {"color": f"rgba({_rgb(R)},0.18)",
                           "line": {"color": R, "width": 1}},
                "showlegend": i==0, "legendgroup": "sl",
                "hovertemplate": f"<b>{ticker}</b> Stop: ${stop_price:.2f} ({stop_pct:+.1f}%)<extra></extra>",
            })
            data.append({
                "type": "bar", "orientation": "h",
                "name": "Profit Zone" if i==0 else None,
                "x": [abs(tp_pct)], "y": [ticker], "base": tp_pct,
                "marker": {"color": f"rgba({_rgb(G)},0.18)",
                           "line": {"color": G, "width": 1}},
                "showlegend": i==0, "legendgroup": "tp",
                "hovertemplate": f"<b>{ticker}</b> Target: ${tp_price:.2f} ({tp_pct:.1f}%)<extra></extra>",
            })
        else:
            data.append({
                "type": "bar", "orientation": "h",
                "name": "Stop Zone" if i==0 else None,
                "x": [abs(stop_pct)], "y": [ticker], "base": stop_pct,
                "marker": {"color": f"rgba({_rgb(R)},0.18)",
                           "line": {"color": R, "width": 1}},
                "showlegend": i==0, "legendgroup": "sl",
                "hovertemplate": f"<b>{ticker}</b> Stop: ${stop_price:.2f} ({stop_pct:.1f}%)<extra></extra>",
            })
            data.append({
                "type": "bar", "orientation": "h",
                "name": "Profit Zone" if i==0 else None,
                "x": [tp_pct], "y": [ticker], "base": 0,
                "marker": {"color": f"rgba({_rgb(G)},0.18)",
                           "line": {"color": G, "width": 1}},
                "showlegend": i==0, "legendgroup": "tp",
                "hovertemplate": f"<b>{ticker}</b> Target: ${tp_price:.2f} (+{tp_pct:.1f}%)<extra></extra>",
            })

        # Current price diamond
        mc = G if unr >= 0 else R
        data.append({
            "type": "scatter",
            "x": [cur_pct], "y": [ticker],
            "mode": "markers+text",
            "marker": {"size": 13, "color": mc, "symbol": "diamond",
                       "line": {"width": 2, "color": "white"}},
            "text": [f"  ${current:.2f} ({unr_pct:+.1f}%)"],
            "textposition": "middle right",
            "textfont": {"size": 9, "color": mc},
            "showlegend": False,
            "hovertemplate": (
                f"<b>{ticker}</b><br>"
                f"Current: ${current:.2f}<br>"
                f"Entry: ${entry:.2f}<br>"
                f"Unrealized: ${unr:+,.2f} ({unr_pct:+.1f}%)<extra></extra>"
            ),
        })

    h = max(220, n*55+70)
    title = "Short Exposure Gauge" if side=="SHORT" else "Position Gauge — vs ATR Levels"
    layout = _base_layout(h, title, color, {
        "barmode": "overlay",
        "shapes": [{"type":"line","x0":0,"x1":0,"y0":-0.5,"y1":n-0.5,
                    "xref":"x","yref":"y",
                    "line":{"color":"white","width":1,"dash":"dot"}}],
        "legend": {"orientation":"h","y":1.05,"x":0,
                   "bgcolor":"rgba(0,0,0,0)","font":{"size":9}},
        "xaxis": {"gridcolor":BORDER,"zeroline":True,"zerolinecolor":MUTED,
                  "zerolinewidth":1.5,"ticksuffix":"%","automargin":False,
                  "title":{"text":"% from avg entry"}},
        "yaxis": {"gridcolor":BORDER,"automargin":False},
    })
    return _make_html(data, layout)


def pl_bar_chart(positions: list, orders_df, side: str) -> str:
    from core.data import realized_pl as rpl
    color = BOT_COLOR[side]
    all_t = {p["symbol"] for p in positions}
    if not orders_df.empty:
        all_t |= set(orders_df["Ticker"].unique())
    if not all_t:
        return empty_chart("No data", 240)

    items = []
    for t in all_t:
        pos = next((p for p in positions if p["symbol"]==t), None)
        unr = float(pos["unrealized_pl"]) if pos else 0.0
        real= rpl(orders_df, t)
        items.append((t, unr, real, unr+real))
    items.sort(key=lambda x: x[3], reverse=True)

    data = [
        {
            "type": "bar", "name": "Unrealized",
            "x": [d[0] for d in items],
            "y": [d[1] for d in items],
            "marker": {"color": [color if v>=0 else R for v in [d[1] for d in items]]},
            "hovertemplate": "%{x}<br>Unreal: <b>$%{y:,.2f}</b><extra></extra>",
        },
        {
            "type": "bar", "name": "Realized",
            "x": [d[0] for d in items],
            "y": [d[2] for d in items],
            "marker": {"color": [Y if v>=0 else OR2 for v in [d[2] for d in items]]},
            "hovertemplate": "%{x}<br>Real: <b>$%{y:,.2f}</b><extra></extra>",
        },
    ]
    layout = _base_layout(240, "P/L by Position", color, {
        "barmode": "group",
        "legend": {"orientation":"h","y":1.05,"x":0,
                   "bgcolor":"rgba(0,0,0,0)","font":{"size":8}},
        "xaxis": {"gridcolor":BORDER,"tickangle":-30,"automargin":False},
        "yaxis": {"gridcolor":BORDER,"tickprefix":"$",
                  "zeroline":True,"zerolinecolor":MUTED,"automargin":False},
    })
    return _make_html(data, layout)


def trade_timeline_chart(orders_df: pd.DataFrame, side: str) -> str:
    """Horizontal swimlane chart — one lane per ticker."""
    if orders_df.empty:
        return empty_chart("No trades yet", 280)

    color  = BOT_COLOR[side]
    filled = orders_df[orders_df["Filled"].notna()].copy()
    now    = pd.Timestamp.now(tz="UTC")
    bars   = []

    for t in sorted(filled["Ticker"].unique()):
        t_ord = filled[filled["Ticker"]==t].sort_values("Filled")
        buys  = t_ord[t_ord["Side"]=="BUY"]
        sells = t_ord[t_ord["Side"]=="SELL"]
        if buys.empty: continue

        first_buy  = buys["Filled"].iloc[0]
        last_sell  = sells["Filled"].iloc[-1] if not sells.empty else now
        still_open = sells.empty
        avg_buy    = ((buys["Qty"]*buys["Avg Fill"]).sum()/buys["Qty"].sum()
                      if buys["Qty"].sum()>0 else 0)
        avg_sell   = ((sells["Qty"]*sells["Avg Fill"]).sum()/sells["Qty"].sum()
                      if (not sells.empty and sells["Qty"].sum()>0) else 0)
        pl         = (avg_sell-avg_buy)*sells["Qty"].sum() if not sells.empty else 0
        notional   = buys["Notional"].sum()
        dur_h      = (last_sell-first_buy).total_seconds()/3600

        bars.append({
            "ticker":    t,
            "start":     first_buy,
            "end":       last_sell,
            "open":      still_open,
            "pl":        pl,
            "notional":  notional,
            "avg_buy":   avg_buy,
            "dur_h":     dur_h,
        })

    if not bars:
        return empty_chart("No completed trades", 280)

    bars.sort(key=lambda x: x["start"])
    data = []

    for b in bars:
        bc    = color if b["open"] else (G if b["pl"]>=0 else R)
        label = "OPEN" if b["open"] else f"P/L ${b['pl']:+,.0f}"
        data.append({
            "type": "scatter",
            "x": [str(b["start"]), str(b["end"])],
            "y": [b["ticker"], b["ticker"]],
            "mode": "lines+markers",
            "line": {"color": bc, "width": 10},
            "marker": {"size": 8, "color": bc},
            "showlegend": False,
            "hovertemplate": (
                f"<b>{b['ticker']}</b>  {label}<br>"
                f"Entry: {b['start'].strftime('%b %d %H:%M')}<br>"
                f"Exit: {'OPEN' if b['open'] else b['end'].strftime('%b %d %H:%M')}<br>"
                f"Duration: {b['dur_h']:.1f}h<br>"
                f"Notional: ${b['notional']:,.0f}<extra></extra>"
            ),
        })

    h = max(260, len(bars)*28+60)
    layout = _base_layout(h, "Hold Period Timeline", color, {
        "showlegend": False,
        "xaxis": {"gridcolor":BORDER,"title":{"text":"Date"}},
        "yaxis": {"gridcolor":BORDER,"automargin":False},
    })
    return _make_html(data, layout)


def combined_lifetime(snapshots: dict) -> str:
    """All three bots on one chart."""
    data = []
    for side, color in [("LONG",G),("SHORT",R),("DAY",OR2)]:
        snaps = snapshots.get(side, pd.DataFrame())
        if snaps.empty or "portfolio_value" not in snaps.columns:
            continue
        data.append({
            "type": "scatter",
            "x": snaps["time"].astype(str).tolist(),
            "y": snaps["portfolio_value"].tolist(),
            "mode": "lines", "name": side,
            "line": {"width": 2, "color": color},
            "fill": "tozeroy",
            "fillcolor": f"rgba({_rgb(color)},0.05)",
            "hovertemplate": f"<b>{side}</b><br>${{%{{y:,.2f}}}}<extra></extra>",
        })

    if not data:
        return empty_chart("No snapshot data", 260)

    layout = _base_layout(260, "All Bots — Lifetime Snapshot", TEXT, {
        "hovermode": "x unified",
        "legend": {"orientation":"h","y":1.05,"x":0,
                   "bgcolor":"rgba(0,0,0,0)"},
        "yaxis": {"gridcolor":BORDER,"tickprefix":"$","automargin":False},
    })
    return _make_html(data, layout)
