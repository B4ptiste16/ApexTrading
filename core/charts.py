"""
APEX Charts
Generates Plotly charts as self-contained HTML strings.
Embedded in QWebEngineView widgets inside the Qt app.
"""

import json
import math
import datetime
import numpy as np
import pandas as pd

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None

# Cache the user's local tzinfo at import time. This is the only "local"
# we need because Plotly will draw whatever times we hand it; the chart
# axis label honours the data, not the browser locale.
_LOCAL_TZ = datetime.datetime.now().astimezone().tzinfo


def _utc_to_local_strings(times) -> list:
    """Convert a UTC-aware datetime Series to naive local-time ISO
    strings so Plotly displays them in the user's wall-clock time
    (Alpaca always returns UTC; without this the chart x-axis is
    shifted by the user's timezone offset)."""
    try:
        s = pd.to_datetime(times, utc=True)
        local = s.dt.tz_convert(_LOCAL_TZ).dt.tz_localize(None)
        return local.astype(str).tolist()
    except Exception:
        return [str(t) for t in times]


def _local_market_hours() -> tuple[float, float]:
    """US market session (09:30-16:00 ET) expressed in the user's local
    time as (open_hour, close_hour) fractional hours. Used to feed
    Plotly's xaxis.rangebreaks so the chart hides non-trading hours
    accurately wherever in the world the user is."""
    if _ET is None:
        return (13.5, 20.0)        # fallback: UTC
    try:
        now_et = datetime.datetime.now(_ET)
        o = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
        c = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
        ol = o.astimezone(_LOCAL_TZ)
        cl = c.astimezone(_LOCAL_TZ)
        return (ol.hour + ol.minute / 60.0,
                cl.hour + cl.minute / 60.0)
    except Exception:
        return (13.5, 20.0)


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
    """Wrap traces + layout into a self-contained HTML page.

    V4.6.110 — the figure JSON is tagged in a <script type="application/json">
    block so the desktop ChartView can pull it out and, after the first paint,
    morph the chart in place with Plotly.react() (window.__apexReact) instead of
    reloading the whole web view. react() diffs the data and only redraws what
    changed, so a price tick updates smoothly with no blank flash / page shift."""
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
<script id="apex-fig" type="application/json">{fig_json}</script>
<script>
  var _opts = {{ responsive: true, displayModeBar: false, scrollZoom: false }};
  var fig = JSON.parse(document.getElementById('apex-fig').textContent);
  Plotly.newPlot('chart', fig.data, fig.layout, _opts);
  // In-place update entry point used by the desktop for subsequent refreshes.
  window.__apexReact = function(figStr) {{
    try {{
      var f = JSON.parse(figStr);
      Plotly.react('chart', f.data, f.layout, _opts);
    }} catch (e) {{}}
  }};
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
    band — translated to the user's local timezone so it lines up with
    the local-time x-axis labels.
    The DAILY-resolution periods (3M/6M/1Y) only need the weekend hide."""
    breaks = [{"bounds": ["sat", "mon"]}]
    if period in {"1D", "1W", "1M"}:
        open_h, close_h = _local_market_hours()
        # Plotly hides from `close_h` to next-day `open_h` (wraps midnight)
        breaks.append({"bounds": [close_h, open_h], "pattern": "hour"})
    return breaks


def equity_curve(equity_df: pd.DataFrame, side: str, period: str,
                  events: list | None = None) -> str:
    """v3.1.2 — line shows PERFORMANCE (profit/loss) rather than raw
    equity, so cash deposits don't fake jumps. The full equity number
    still appears in the hover tooltip.

    `events` is an optional list of (timestamp, kind, label) tuples
    where kind ∈ {"buy", "win", "loss"}. Each is drawn as a thin vertical
    line on the chart so the user can see at a glance when trades fired."""
    if equity_df.empty:
        return empty_chart(f"No equity data — {period}")
    color = BOT_COLOR.get(side, G)
    has_pl = "profit_loss" in equity_df.columns

    # PERFORMANCE line: use Alpaca's deposit-adjusted profit_loss if we
    # have it; otherwise fall back to (equity - first-equity).
    eq = equity_df["equity"].tolist()
    if has_pl:
        pl = equity_df["profit_loss"].tolist()
    else:
        first = eq[0] if eq else 0
        pl = [v - first for v in eq]

    fv_pl, lv_pl = pl[0], pl[-1]
    fv_eq        = eq[0] if eq else 0
    delta        = lv_pl - fv_pl
    base         = fv_eq if fv_eq else 1
    pct          = (delta / base) * 100

    line_color = color if lv_pl >= fv_pl else R
    x_local    = _utc_to_local_strings(equity_df["time"])

    # Customdata holds (equity, profit_loss) for the unified hover tooltip
    custom = list(zip(eq, pl))

    data = [{
        "type": "scatter",
        "x":    x_local,
        "y":    pl,
        "mode": "lines",
        "name": "Performance",
        "line": {"width": 2.5, "color": line_color},
        "fill": "tozeroy",
        "fillcolor": f"rgba({_rgb(line_color)},0.07)",
        "customdata": custom,
        "hovertemplate": (
            "<b>P/L: $%{customdata[1]:+,.2f}</b><br>"
            "Equity: $%{customdata[0]:,.2f}<br>"
            "%{x}<extra></extra>"
        ),
    }]

    yr     = _tight_yrange(pl) if any(p != 0 for p in pl) else None
    # Reference line at y=0 (= deposits baseline)
    shapes = [{
        "type": "line", "x0": 0, "x1": 1, "y0": 0, "y1": 0,
        "xref": "paper", "yref": "y",
        "line": {"color": MUTED, "width": 1, "dash": "dot"},
    }]
    annots = [{
        "x": 0, "y": 0, "xref": "paper", "yref": "y",
        "text": "even", "showarrow": False,
        "font": {"size": 8, "color": MUTED},
        "xanchor": "left", "yanchor": "bottom",
    }]

    # v3.1.2 — vertical event markers (one shape per trade)
    # V4.6.111 — keep only events INSIDE the visible window. The equity series
    # already reflects the selected period, so an out-of-range trade would
    # stretch the x-axis far past the curve and squash it into the corner
    # (the "1D curve but events from two weeks ago" bug).
    if events:
        try:
            _tmin = pd.to_datetime(equity_df["time"].iloc[0],  utc=True)
            _tmax = pd.to_datetime(equity_df["time"].iloc[-1], utc=True)
            _filtered = []
            for _e in events:
                try:
                    _ets = pd.to_datetime(_e[0], utc=True)
                    if _tmin <= _ets <= _tmax:
                        _filtered.append(_e)
                except Exception:
                    _filtered.append(_e)
            events = _filtered
        except Exception:
            pass
    if events:
        for ts, kind, label in events:
            if hasattr(ts, "tz_convert"):
                ts_local = ts.tz_convert(_LOCAL_TZ).tz_localize(None)
            else:
                ts_local = pd.to_datetime(ts, utc=True).tz_convert(
                    _LOCAL_TZ).tz_localize(None)
            evt_color = {"buy": OR2, "win": G, "loss": R}.get(kind, MUTED)
            shapes.append({
                "type": "line",
                "x0": str(ts_local), "x1": str(ts_local),
                "y0": 0, "y1": 1, "yref": "paper",
                "line": {"color": evt_color, "width": 1.2, "dash": "dot"},
                "opacity": 0.55,
            })
        # Add an invisible scatter trace at the bottom of the chart so the
        # user can hover near each event line and see what it was.
        ev_x = []
        ev_lbl = []
        ev_colors = []
        for ts, kind, label in events:
            if hasattr(ts, "tz_convert"):
                ev_x.append(str(ts.tz_convert(_LOCAL_TZ).tz_localize(None)))
            else:
                ev_x.append(str(pd.to_datetime(ts, utc=True).tz_convert(
                    _LOCAL_TZ).tz_localize(None)))
            ev_lbl.append(label)
            ev_colors.append({"buy": OR2, "win": G, "loss": R}.get(kind, MUTED))
        if yr:
            ev_y = [yr[0] + (yr[1] - yr[0]) * 0.03] * len(ev_x)
        else:
            mn = min(pl) if pl else 0
            ev_y = [mn] * len(ev_x)
        data.append({
            "type": "scatter",
            "x": ev_x, "y": ev_y,
            "mode": "markers",
            "marker": {"size": 7, "color": ev_colors, "symbol": "triangle-up",
                       "line": {"color": "white", "width": 0.5}},
            "text": ev_lbl,
            "hovertemplate": "%{text}<br>%{x}<extra></extra>",
            "showlegend": False,
        })

    title = (f"Performance — {period}   "
             f"${lv_pl:+,.2f}   ({pct:+.2f}%)   "
             f"·  Equity ${eq[-1]:,.2f}")

    layout = _base_layout(
        280, title, line_color, {
            "showlegend": False,
            "hovermode": "x unified",
            "shapes": shapes,
            "annotations": annots,
            "xaxis": {"gridcolor": BORDER, "zeroline": False,
                      "automargin": False,
                      "rangebreaks": _market_hours_rangebreaks(period)},
            "yaxis": {"gridcolor": BORDER, "zeroline": True,
                      "zerolinecolor": MUTED, "zerolinewidth": 1,
                      "automargin": False, "tickprefix": "$",
                      **({"range": yr} if yr else {})},
        })
    return _make_html(data, layout)


def combined_history_chart(df: pd.DataFrame, period: str) -> str:
    """Total portfolio PERFORMANCE (deposit-adjusted profit/loss across
    all 3 accounts) over the selected period.

    v3.1.2 — switched from raw equity to profit_loss so the line doesn't
    spike when a new broker account is linked (adding cash adjusts the
    baseline but does not show as growth). The actual equity value still
    appears in the hover tooltip."""
    if df is None or df.empty:
        return empty_chart(f"No portfolio data — {period}")
    has_pl = "profit_loss" in df.columns
    eq     = df["equity"].tolist()
    pl     = df["profit_loss"].tolist() if has_pl else [v - eq[0] for v in eq]
    fv_pl  = float(pl[0])
    lv_pl  = float(pl[-1])
    delta  = lv_pl - fv_pl
    base   = eq[0] if eq else 1
    pct    = (delta / base) * 100 if base else 0.0
    c      = G if lv_pl >= fv_pl else R

    custom = list(zip(eq, pl))

    data = [{
        "type": "scatter",
        "x":    _utc_to_local_strings(df["time"]),
        "y":    pl,
        "mode": "lines",
        "name": "Total P/L",
        "line": {"width": 2.5, "color": c},
        "fill": "tozeroy",
        "fillcolor": f"rgba({_rgb(c)},0.07)",
        "customdata": custom,
        "hovertemplate": (
            "<b>P/L: $%{customdata[1]:+,.2f}</b><br>"
            "Equity: $%{customdata[0]:,.2f}<br>"
            "%{x}<extra></extra>"
        ),
    }]
    yr = _tight_yrange(pl) if any(p != 0 for p in pl) else None
    shapes = [{
        "type": "line", "x0": 0, "x1": 1, "y0": 0, "y1": 0,
        "xref": "paper", "yref": "y",
        "line": {"color": MUTED, "width": 1, "dash": "dot"},
    }]
    layout = _base_layout(
        300,
        f"Total Portfolio — {period}   "
        f"${lv_pl:+,.2f}   ({pct:+.2f}%)   "
        f"·  Equity ${eq[-1]:,.2f}",
        c, {
            "showlegend": False,
            "hovermode": "x unified",
            "shapes": shapes,
            "xaxis": {"gridcolor": BORDER, "zeroline": False,
                      "automargin": False,
                      "rangebreaks": _market_hours_rangebreaks(period)},
            "yaxis": {"gridcolor": BORDER, "zeroline": True,
                      "zerolinecolor": MUTED, "zerolinewidth": 1,
                      "automargin": False, "tickprefix": "$",
                      **({"range": yr} if yr else {})},
        })
    return _make_html(data, layout)


def lifetime_chart(snaps: pd.DataFrame, side: str) -> str:
    if snaps.empty or "portfolio_value" not in snaps.columns:
        return empty_chart("No snapshot data yet")
    color = BOT_COLOR.get(side, G)
    pv    = snaps["portfolio_value"].values
    peak  = pd.Series(pv).cummax().tolist()
    ret   = (pv[-1]/pv[0]-1)*100 if pv[0]>0 else 0
    yr    = _tight_yrange(pv.tolist())

    data = [
        {
            "type": "scatter",
            "x": _utc_to_local_strings(snaps["time"]),
            "y": pv.tolist(),
            "mode": "lines", "name": "Portfolio",
            "line": {"width": 2.5, "color": color},
            "fill": "tozeroy",
            "fillcolor": f"rgba({_rgb(color)},0.08)",
            "hovertemplate": "<b>$%{y:,.2f}</b><br>%{x}<extra></extra>",
        },
        {
            "type": "scatter",
            "x": _utc_to_local_strings(snaps["time"]),
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
        # V4.6.120 — hide overnight/weekend gaps so the line draws close-to-close.
        "xaxis": {"gridcolor":BORDER,
                  "rangebreaks": _market_hours_rangebreaks("1W")},
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
        "x": _utc_to_local_strings(snaps["time"]),
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
        "xaxis": {"gridcolor":BORDER,
                  "rangebreaks": _market_hours_rangebreaks("1W")},
        "yaxis": {"gridcolor":BORDER,"ticksuffix":"%",
                  "zeroline":True,"zerolinecolor":MUTED,"automargin":False},
    })
    return _make_html(data, layout)


def bracket_gauge(brackets: dict, positions: list,
                   meta: dict | None = None) -> str:
    """
    Day bot: horizontal bracket visualiser.
    Green zone = take profit path, Red zone = stop loss path.
    Diamond = current price.

    v1.2.2 — iterate over LIVE positions and pull the bracket levels
    from (in order of preference):
      1. `brackets` — daybot's saved open_brackets dict (real Alpaca
         TP/SL orders the bot placed itself).
      2. `meta`     — per-stock ATR-derived levels from data.position_meta()
         (used when a position exists on Alpaca but isn't tracked in
         daybot_state.json, e.g. it was bought manually or the state
         hasn't synced since the bot last ran).
      3. Fallback 2.5 % / +5 % bands if neither is available.
    Brackets in state but whose position is gone (liquidated) are
    dropped silently — previously they rendered as frozen "+0.0%"
    diamonds because current price fell back to entry.
    """
    if not positions:
        return empty_chart("No open positions", 260)

    pos_map = {p["symbol"]: p for p in positions}
    meta = meta or {}

    effective: dict = {}
    for sym, pos in pos_map.items():
        entry = float(pos.get("avg_entry_price", 0))
        qty   = float(pos.get("qty", 0))
        if entry <= 0:
            continue
        b = brackets.get(sym)
        if b and b.get("stop") and b.get("tp"):
            effective[sym] = {
                "entry": float(b.get("entry", entry)),
                "stop":  float(b["stop"]),
                "tp":    float(b["tp"]),
                "qty":   float(b.get("qty", qty)),
            }
        elif sym in meta:
            m = meta[sym]
            effective[sym] = {
                "entry": entry,
                "stop":  float(m.get("stop_price", entry * 0.975)),
                "tp":    float(m.get("tp_price",   entry * 1.025)),
                "qty":   qty,
            }
        else:
            effective[sym] = {
                "entry": entry,
                "stop":  round(entry * 0.975, 2),
                "tp":    round(entry * 1.025, 2),
                "qty":   qty,
            }

    if not effective:
        return empty_chart("No open positions", 260)

    brackets = effective
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

    color = BOT_COLOR.get(side, G)
    data  = []
    n     = len(positions)
    meta  = meta or {}

    for i, p in enumerate(sorted(positions, key=lambda x: x["symbol"])):
        ticker  = p["symbol"]
        entry   = float(p.get("avg_entry_price", 0) or 0)
        current = float(p.get("current_price", entry) or 0)
        # V4.6.60 — some brokers (e.g. IBKR cloud sub-portfolios) don't expose
        # an average entry price. The gauge is built as a % from entry, so a
        # zero entry used to raise ZeroDivisionError — which the caller caught
        # silently, leaving a stale "No open positions" while OTHER charts on
        # the page rendered fine. Fall back to the current price (position
        # shown at break-even) so the gauge always renders.
        if entry <= 0:
            entry = current
        if entry <= 0:
            continue  # no usable price at all — can't place this row
        unr     = float(p.get("unrealized_pl", 0) or 0)
        unr_pct = float(p.get("unrealized_plpc", 0) or 0) * 100

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
    color = BOT_COLOR.get(side, G)
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

    color  = BOT_COLOR.get(side, G)
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
            "x": _utc_to_local_strings(snaps["time"]),
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


def returns_by_confidence(log_df, side: str) -> str:
    """V4.6.68 — telemetry: average portfolio change per AI-confidence bucket
    (a proxy for 'returns as a function of confidence used'). Built from the
    bot's structured log (confidence + portfolio_before/after)."""
    color = BOT_COLOR.get(side, G)
    try:
        if log_df is None or getattr(log_df, "empty", True):
            return empty_chart("No AI calls logged yet", 240)
        df = log_df.copy()
        for col in ("confidence", "pv_before", "pv_after"):
            if col not in df.columns:
                return empty_chart("Telemetry builds as the bot runs", 240)
        df = df[df["confidence"].notna() & df["pv_before"].notna()
                & df["pv_after"].notna()]
        df = df[df["pv_before"].astype(float) > 0]
        if df.empty:
            return empty_chart("Telemetry builds as the bot runs", 240)
        df["ret"] = (df["pv_after"].astype(float)
                     / df["pv_before"].astype(float) - 1.0) * 100.0
        df["bucket"] = (df["confidence"].astype(float) * 10).round() / 10.0
        g = df.groupby("bucket")["ret"].mean().reset_index().sort_values("bucket")
        xs = [f"{b:.0%}" for b in g["bucket"]]
        ys = [round(float(v), 3) for v in g["ret"]]
        bars = [{
            "type": "bar", "x": xs, "y": ys,
            "marker": {"color": [G if v >= 0 else R for v in ys]},
            "hovertemplate": "conf %{x}: %{y:.3f}%<extra></extra>",
        }]
        layout = _base_layout(260, "Avg return by AI confidence", color, {
            "xaxis": {"title": {"text": "AI confidence"}, "gridcolor": BORDER},
            "yaxis": {"title": {"text": "avg Δ %"}, "gridcolor": BORDER,
                      "ticksuffix": "%", "zeroline": True,
                      "zerolinecolor": MUTED},
        })
        return _make_html(bars, layout)
    except Exception as e:
        return empty_chart(f"telemetry error: {e}", 240)


# ── Find Stocks — single-symbol price chart ────────────────────────────────

def _ma(values: list, window: int) -> list:
    """Simple moving average aligned to the end of the series (None until the
    window fills). Returns a list the same length as *values*."""
    out = [None] * len(values)
    if len(values) < window:
        return out
    s = sum(values[:window])
    out[window - 1] = s / window
    for i in range(window, len(values)):
        s += values[i] - values[i - window]
        out[i] = s / window
    return out


def _x_axis_values(idx, intraday: bool) -> list:
    """Convert a (tz-aware) yfinance index to plain strings Plotly can place.

    Intraday: convert to the user's local wall-clock so the candles line up with
    the local-time rangebreaks (and so the whole session shows, not a 25-minute
    sliver — the old code left the index in ET while the rangebreaks were local).
    Daily+: use the date only (no tz shift that could nudge a bar to the wrong
    day)."""
    s = pd.Series(pd.to_datetime(idx, utc=True))
    if intraday:
        local = s.dt.tz_convert(_LOCAL_TZ).dt.tz_localize(None)
        return local.astype(str).tolist()
    return s.dt.tz_convert(_LOCAL_TZ).dt.strftime("%Y-%m-%d").tolist()


_TRADE_MARKERS = {
    # kind → (color, plotly symbol, label)
    "buy":       (G,   "triangle-up",   "Bought"),
    "add":       (G,   "diamond",       "Bought more"),
    "sell_some": (OR2, "triangle-down", "Sold some"),
    "sell_all":  (R,   "x",             "Sold"),
}


def position_growth_chart(df: pd.DataFrame, events: list, ref_price: float,
                          symbol: str, height: int = 360) -> str:
    """A single holding's growth as % from its cost basis, with markers on the
    curve for each trade: bought / bought more / sold some / sold.

    df         — daily OHLC frame (needs 'Close') covering the holding period.
    events     — list of {time, price, kind, qty} where kind ∈ _TRADE_MARKERS.
    ref_price  — cost basis the % growth is measured from (falls back to the
                 first close)."""
    if df is None or getattr(df, "empty", True):
        return empty_chart(f"No price history for {symbol}")
    try:
        x = _x_axis_values(df.index, intraday=False)
        closes = [float(c) for c in df["Close"].tolist()]
        ref = float(ref_price) if ref_price else closes[0]
        if not ref:
            ref = closes[0] or 1.0
        growth = [(c / ref - 1.0) * 100.0 for c in closes]

        last = growth[-1]
        up = last >= 0
        line_color = G if up else R

        data = [{
            "type": "scatter", "x": x, "y": growth, "mode": "lines",
            "name": symbol, "line": {"width": 2.4, "color": line_color},
            "fill": "tozeroy", "fillcolor": f"rgba({_rgb(line_color)},0.07)",
            "hovertemplate": "%{y:+.2f}%<br>%{x}<extra></extra>",
        }]

        # Cost-basis reference line at 0%.
        shapes = [{
            "type": "line", "x0": 0, "x1": 1, "y0": 0, "y1": 0,
            "xref": "paper", "yref": "y",
            "line": {"color": MUTED, "width": 1, "dash": "dot"},
        }]
        annots = [{
            "x": 0, "y": 0, "xref": "paper", "yref": "y",
            "text": "cost basis", "showarrow": False,
            "font": {"size": 8, "color": MUTED},
            "xanchor": "left", "yanchor": "bottom",
        }]

        # One marker trace per kind so each gets its own colour + symbol.
        by_kind: dict[str, dict] = {}
        for ev in events or []:
            kind = ev.get("kind", "buy")
            color, msym, klabel = _TRADE_MARKERS.get(
                kind, _TRADE_MARKERS["buy"])
            t = ev.get("time")
            try:
                xs = pd.to_datetime(t, utc=True).tz_convert(
                    _LOCAL_TZ).strftime("%Y-%m-%d")
            except Exception:
                xs = str(t)[:10]
            price = float(ev.get("price") or 0) or ref
            y = (price / ref - 1.0) * 100.0
            qty = ev.get("qty")
            qtxt = f" {qty:g} sh" if isinstance(qty, (int, float)) else ""
            label = f"{klabel}{qtxt} @ ${price:,.2f}"
            d = by_kind.setdefault(kind, {
                "type": "scatter", "mode": "markers", "x": [], "y": [],
                "text": [], "name": klabel,
                "marker": {"size": 12, "color": color, "symbol": msym,
                           "line": {"color": "#0b0e14", "width": 1}},
                "hovertemplate": "%{text}<br>%{x}<extra></extra>",
            })
            d["x"].append(xs)
            d["y"].append(y)
            d["text"].append(label)
        data.extend(by_kind.values())

        sign = "+" if last >= 0 else ""
        title = f"{symbol} — position growth   {sign}{last:.2f}%"

        layout = _base_layout(height, title, line_color, {
            "showlegend": False, "hovermode": "closest",
            "shapes": shapes, "annotations": annots,
            "xaxis": {"gridcolor": BORDER, "zeroline": False,
                      "automargin": False, "type": "date",
                      "rangebreaks": [{"bounds": ["sat", "mon"]}]},
            "yaxis": {"gridcolor": BORDER, "zeroline": True,
                      "zerolinecolor": MUTED, "zerolinewidth": 1,
                      "automargin": False, "ticksuffix": "%"},
        })
        return _make_html(data, layout)
    except Exception as e:
        return empty_chart(f"chart error: {e}")


def price_history_chart(df: pd.DataFrame, symbol: str, period: str,
                        chart_type: str = "candle", height: int = 360) -> str:
    """Broker-style price chart for a single stock: candlesticks (or a line)
    with MA20/MA50 overlays on top and a volume sub-panel beneath.

    `df` is a yfinance OHLCV frame (DatetimeIndex, columns Open/High/Low/Close
    /Volume). `period` is one of the Find-Stocks selector labels (1D…5Y)."""
    if df is None or getattr(df, "empty", True):
        return empty_chart(f"No data for {symbol} — {period}")

    try:
        intraday = period in {"1D", "1W"}
        try:
            x = _x_axis_values(df.index, intraday)
        except Exception:
            x = [str(t) for t in df.index]

        closes = [float(c) for c in df["Close"].tolist()]
        opens  = [float(c) for c in df["Open"].tolist()]  if "Open"  in df else closes
        highs  = [float(c) for c in df["High"].tolist()]  if "High"  in df else closes
        lows   = [float(c) for c in df["Low"].tolist()]   if "Low"   in df else closes
        vols   = [float(c) for c in df["Volume"].tolist()] if "Volume" in df else []

        first, last = closes[0], closes[-1]
        delta = last - first
        pct   = (delta / first * 100.0) if first else 0.0
        up    = last >= first
        line_color = G if up else R

        # Moving averages first — they feed the y-range so the lines never clip.
        ma_series = []
        for win, col, nm in ((20, OR2, "MA20"), (50, PU, "MA50")):
            ma = _ma(closes, win)
            if any(v is not None for v in ma):
                ma_series.append((ma, col, nm))

        data = []
        if chart_type == "line":
            data.append({
                "type": "scatter", "x": x, "y": closes, "mode": "lines",
                "name": symbol, "line": {"width": 2.2, "color": line_color},
                "fill": "tozeroy",
                "fillcolor": f"rgba({_rgb(line_color)},0.06)",
                "hovertemplate": "$%{y:,.2f}<br>%{x}<extra></extra>",
            })
        else:
            data.append({
                "type": "candlestick", "x": x,
                "open": opens, "high": highs, "low": lows, "close": closes,
                "name": symbol,
                "increasing": {"line": {"color": G}, "fillcolor": G},
                "decreasing": {"line": {"color": R}, "fillcolor": R},
                "yaxis": "y",
            })

        for ma, col, nm in ma_series:
            data.append({
                "type": "scatter", "x": x, "y": ma, "mode": "lines", "name": nm,
                "line": {"width": 1.2, "color": col},
                "hovertemplate": nm + " $%{y:,.2f}<extra></extra>",
                "connectgaps": False,
            })

        # Volume sub-panel
        if vols and any(v for v in vols):
            vcolors = [G if closes[i] >= opens[i] else R for i in range(len(closes))]
            data.append({
                "type": "bar", "x": x, "y": vols,
                "marker": {"color": vcolors, "line": {"width": 0}},
                "opacity": 0.5, "name": "Volume", "yaxis": "y2",
                "hovertemplate": "Vol %{y:,.0f}<extra></extra>",
            })

        # ── Tight, centred y-range ─────────────────────────────────────────
        # Zoom the price axis to the data (incl. MA lines) with a little
        # padding, so the curve fills the panel instead of floating as a flat
        # line near the top. A line chart keys off closes; candles off hi/lo.
        if chart_type == "line":
            yvals = list(closes)
        else:
            yvals = list(highs) + list(lows)
        for ma, _c, _n in ma_series:
            yvals += [v for v in ma if v is not None]
        ylo, yhi = min(yvals), max(yvals)
        spread = (yhi - ylo) or (yhi * 0.01 or 1.0)
        pad = spread * 0.08
        yrange = [ylo - pad, yhi + pad]

        sign = "+" if delta >= 0 else ""
        title = (f"{symbol} — {period}    ${last:,.2f}    "
                 f"{sign}{delta:,.2f} ({pct:+.2f}%)")

        # Rangebreaks hide non-trading time so candles sit close-to-close. Now
        # that x is in local wall-clock, the local-hour bounds line up correctly.
        rb = []
        if intraday:
            rb = _market_hours_rangebreaks(period)
        elif period in {"1M", "3M", "6M", "1Y"}:
            rb = [{"bounds": ["sat", "mon"]}]

        layout = _base_layout(height, title, line_color, {
            "showlegend": False,
            "hovermode": "x unified",
            "xaxis": {"gridcolor": BORDER, "zeroline": False,
                      "automargin": False, "rangeslider": {"visible": False},
                      "rangebreaks": rb, "type": "date"},
            "yaxis": {"gridcolor": BORDER, "zeroline": False, "automargin": False,
                      "tickprefix": "$", "domain": [0.26, 1.0],
                      "side": "right", "range": yrange},
            "yaxis2": {"gridcolor": "rgba(0,0,0,0)", "zeroline": False,
                       "automargin": False, "domain": [0.0, 0.18],
                       "showticklabels": False},
        })
        return _make_html(data, layout)
    except Exception as e:
        return empty_chart(f"chart error: {e}")
