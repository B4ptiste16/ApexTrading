"""
APEX · bot performance & optimal-value recommendations  (v4.6.28+)
─────────────────────────────────────────────────────────────────
Records per-bot performance across users + computes the optimal
value for each tunable setting (min_confidence, ai_model, universe,
etc.) by pooling outcomes.

Schema
──────
bot_runs:
  id, user_id, slug, ai_provider, ai_model, min_confidence,
  universe_used, asset_type, started_at, ended_at,
  start_equity, end_equity, profit, return_pct

bot_optimums:
  slug, setting_name, setting_value, sample_count, mean_return_pct,
  computed_at

Flow
────
1. Every bot, on clean shutdown OR at midnight UTC, writes a
   `bot_runs` row with its settings + the equity change since the
   last row.
2. Once an hour a background thread aggregates: for each
   (slug, setting_name, setting_value) tuple with >=5 samples,
   compute the mean return.
3. Client endpoint /api/bots/<slug>/optimums returns the per-setting
   optimal value (the one with the highest mean return that has at
   least 5 samples).
4. UI hint next to each setting reads from that endpoint and shows
   "(optimal: <value> · based on N users · +X% mean return)".
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import defaultdict
from typing import Optional


# Min samples before we'll recommend a setting value as "optimal".
# Below this, we don't have enough data to be confident.
MIN_SAMPLES_FOR_OPTIMUM = 5


def _conn():
    from . import database
    return sqlite3.connect(database.DB_PATH)


def init_tables():
    """Create bot_runs + bot_optimums if they don't exist."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS bot_runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                slug            TEXT NOT NULL,
                ai_provider     TEXT,
                ai_model        TEXT,
                min_confidence  REAL,
                universe_used   TEXT,
                asset_type      TEXT,
                started_at      REAL NOT NULL,
                ended_at        REAL,
                start_equity    REAL,
                end_equity      REAL,
                profit          REAL,
                return_pct      REAL
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_bot_runs_slug
            ON bot_runs(slug)
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS bot_optimums (
                slug            TEXT NOT NULL,
                setting_name    TEXT NOT NULL,
                setting_value   TEXT NOT NULL,
                sample_count    INTEGER NOT NULL,
                mean_return_pct REAL NOT NULL,
                computed_at     REAL NOT NULL,
                PRIMARY KEY (slug, setting_name, setting_value)
            )
        """)
        c.commit()


def record_run(*, user_id: int, slug: str,
               ai_provider: str = "",
               ai_model: str = "",
               min_confidence: Optional[float] = None,
               universe_used: str = "",
               asset_type: str = "",
               start_equity: float = 0.0,
               end_equity: float = 0.0,
               started_at: Optional[float] = None,
               ended_at: Optional[float] = None) -> None:
    """Insert one completed bot-run record. Called by bot_runner when
    a bot is stopped, OR by a daily cron that snapshots running bots."""
    if started_at is None:
        started_at = time.time()
    if ended_at is None:
        ended_at = time.time()
    profit = end_equity - start_equity
    return_pct = (profit / start_equity * 100) if start_equity else 0.0
    with _conn() as c:
        c.execute("""
            INSERT INTO bot_runs
            (user_id, slug, ai_provider, ai_model, min_confidence,
             universe_used, asset_type, started_at, ended_at,
             start_equity, end_equity, profit, return_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, slug.upper(), ai_provider, ai_model,
              min_confidence, universe_used, asset_type,
              started_at, ended_at, start_equity, end_equity,
              profit, return_pct))
        c.commit()


def compute_optimums(min_samples: int = MIN_SAMPLES_FOR_OPTIMUM) -> int:
    """Aggregate every bot_runs row by (slug, setting). For each
    (setting_name, setting_value), compute mean return + sample count.
    Writes everything to bot_optimums. Returns number of rows
    written."""
    init_tables()
    # Group every run by setting
    by_key = defaultdict(list)  # (slug, setting_name, setting_value) -> [return_pct, …]
    with _conn() as c:
        rows = c.execute("""
            SELECT slug, ai_provider, ai_model, min_confidence,
                   universe_used, asset_type, return_pct
            FROM bot_runs WHERE return_pct IS NOT NULL
        """).fetchall()
    for (slug, ai_provider, ai_model, min_conf, universe,
         asset_type, ret) in rows:
        ret = float(ret or 0)
        if ai_provider:
            by_key[(slug, "ai_provider", ai_provider)].append(ret)
        if ai_model:
            by_key[(slug, "ai_model", ai_model)].append(ret)
        if min_conf is not None:
            # Bucket confidence to 0.05 — otherwise every fractional
            # value becomes its own bucket with sample_count=1
            bucket = round(float(min_conf) * 20) / 20
            by_key[(slug, "min_confidence",
                    f"{bucket:.2f}")].append(ret)
        if universe:
            by_key[(slug, "universe_used", universe)].append(ret)
        if asset_type:
            by_key[(slug, "asset_type", asset_type)].append(ret)
    # Write aggregates
    now = time.time()
    written = 0
    with _conn() as c:
        for (slug, name, value), returns in by_key.items():
            if len(returns) < min_samples:
                continue
            mean = sum(returns) / len(returns)
            c.execute("""
                INSERT INTO bot_optimums
                (slug, setting_name, setting_value, sample_count,
                 mean_return_pct, computed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug, setting_name, setting_value) DO UPDATE
                SET sample_count = excluded.sample_count,
                    mean_return_pct = excluded.mean_return_pct,
                    computed_at = excluded.computed_at
            """, (slug, name, value, len(returns), mean, now))
            written += 1
        c.commit()
    return written


def get_optimums(slug: str) -> dict:
    """Return optimal settings for one bot. Shape:
      {
        "slug":      "LONG",
        "settings":  {
          "ai_model": {
            "optimal": "claude-sonnet-4-5", "mean_return": 12.3,
            "samples": 18,
            "all": [{"value": "...", "samples": N, "mean": X}, …]
          },
          ...
        }
      }"""
    init_tables()
    out = {"slug": slug.upper(), "settings": {}}
    with _conn() as c:
        rows = c.execute("""
            SELECT setting_name, setting_value, sample_count,
                   mean_return_pct
            FROM bot_optimums WHERE slug = ?
            ORDER BY setting_name, mean_return_pct DESC
        """, (slug.upper(),)).fetchall()
    by_setting = defaultdict(list)
    for name, value, samples, mean in rows:
        by_setting[name].append({
            "value": value, "samples": samples, "mean": float(mean),
        })
    for name, entries in by_setting.items():
        # Sort already done by SQL ORDER BY mean_return_pct DESC
        best = entries[0]
        out["settings"][name] = {
            "optimal": best["value"],
            "mean_return": best["mean"],
            "samples": best["samples"],
            "all": entries,
        }
    return out


# ── Background aggregator (called from server/app.py startup) ──

def start_cron():
    """Spawn the hourly aggregator. Idempotent."""
    name = "bot_optimums"
    for t in threading.enumerate():
        if t.name == name:
            return
    def _loop():
        # Wait 30s after import so the FastAPI app is fully up
        time.sleep(30)
        while True:
            try:
                written = compute_optimums()
                if written:
                    print(f"[bot_optimums] aggregated — {written} "
                          f"(slug, setting, value) tuples updated",
                          flush=True)
            except Exception as e:
                print(f"[bot_optimums] cron error: {e}", flush=True)
            time.sleep(3600)
    threading.Thread(target=_loop, daemon=True, name=name).start()
    print(f"[bot_optimums] started (hourly aggregation)", flush=True)
