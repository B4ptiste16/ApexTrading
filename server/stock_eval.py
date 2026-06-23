"""
APEX Server — Shared weekly stock evaluations
─────────────────────────────────────────────────────────────────────
Generates a stock's AI evaluation ON THE SERVER so it's identical for every
user (not computed per-desktop). For each AI provider the configured "main"
account (default username 'baptou') has a key for, one structured review is
produced; the set is cached per ISO-week so a stock is only evaluated once a
week regardless of how many users open it.

Self-contained (no `core` import — `core` isn't on the server's path) and
defensive: a missing provider SDK or key simply drops that provider rather
than failing the whole evaluation.

Response shape returned by evaluate():
    {
      "symbol": "AAPL", "week": "2026-06-15", "generated_at": "...",
      "consensus": {"rating": "BUY", "score": 3.6, "n": 3},
      "models": [
        {"provider":"anthropic","label":"Claude","model":"...",
         "rating":"BUY","confidence":72,"summary":"...",
         "bullish":[...],"bearish":[...]},
        ...
      ],
      "error": null
    }
"""

from __future__ import annotations

import os
import re
import json
import time
import threading
import datetime
from pathlib import Path

from . import credentials as creds, database

# ── Cache location (alongside the server DB) ───────────────────────────────
EVAL_DIR = Path(os.environ.get(
    "APEX_STOCK_EVAL_DIR", str(database.DB_PATH.parent / "stock_evals")))

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

# ── Providers ──────────────────────────────────────────────────────────────
# provider → (credential key, default model, display label)
PROVIDERS = {
    "anthropic": ("ANTHROPIC_API_KEY",  "claude-haiku-4-5-20251001", "Claude"),
    "google":    ("GOOGLE_AI_API_KEY",  "gemini-2.0-flash",          "Gemini"),
    "xai":       ("XAI_API_KEY",        "grok-2-1212",               "Grok"),
    "groq":      ("GROQ_API_KEY",       "llama-3.3-70b-versatile",   "Llama"),
}

_RATING_SCORE = {
    "STRONG SELL": 1, "SELL": 2, "HOLD": 3, "NEUTRAL": 3,
    "BUY": 4, "STRONG BUY": 5,
}
_SCORE_RATING = {1: "Strong Sell", 2: "Sell", 3: "Hold", 4: "Buy", 5: "Strong Buy"}


# ── Week helpers ────────────────────────────────────────────────────────────

def current_week_monday() -> str:
    today = datetime.date.today()
    return (today - datetime.timedelta(days=today.weekday())).isoformat()


def _cache_path(symbol: str, week: str) -> Path:
    return EVAL_DIR / week / f"{symbol.upper()}.json"


def _read_cache(symbol: str, week: str) -> dict | None:
    try:
        p = _cache_path(symbol, week)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _write_cache(symbol: str, week: str, data: dict) -> None:
    try:
        p = _cache_path(symbol, week)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:
        print(f"[stock_eval] cache write failed: {e}")


# ── Main-account key resolution ─────────────────────────────────────────────

def _main_account_keys() -> dict[str, str]:
    """AI provider keys from the configured main account (default 'baptou'),
    with server-env fallbacks. Returns {provider: api_key} for those present."""
    blob: dict = {}

    uid_env = os.environ.get("APEX_STOCK_EVAL_UID")
    user = None
    try:
        if uid_env:
            user = database.get_user_by_id(int(uid_env))
        if user is None:
            uname = os.environ.get("APEX_STOCK_EVAL_USERNAME", "baptou")
            user = database.get_user_by_username(uname)
    except Exception as e:
        print(f"[stock_eval] main-account lookup failed: {e}")
    if user:
        try:
            blob = creds.load_credentials(user["id"]) or {}
        except Exception as e:
            print(f"[stock_eval] load main-account creds failed: {e}")
            blob = {}

    out: dict[str, str] = {}
    for prov, (cred_key, _model, _label) in PROVIDERS.items():
        key = (blob.get(cred_key)
               or os.environ.get(cred_key)
               or "")
        # Anthropic also honours the APEX-pooled server key.
        if prov == "anthropic" and not key:
            key = os.environ.get("APEX_ANTHROPIC_KEY", "")
        if key:
            out[prov] = key
    return out


# ── Prompt + parsing ─────────────────────────────────────────────────────────

def _f(v):
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f
    except Exception:
        return None


def _fmt(v, kind="num"):
    v = _f(v)
    if v is None:
        return "n/a"
    if kind == "pct":
        return f"{v:+.1f}%"
    if kind == "price":
        return f"${v:,.2f}"
    if kind == "cap":
        for u, d in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
            if abs(v) >= d:
                return f"${v/d:.2f}{u}"
        return f"${v:,.0f}"
    return f"{v:,.2f}"


def _data_block(snap: dict) -> str:
    sym = snap.get("symbol", "?")
    name = snap.get("name") or sym
    return "\n".join([
        f"Stock: {sym} ({name})",
        f"Sector: {snap.get('sector') or 'n/a'} / {snap.get('industry') or 'n/a'}",
        f"Price: {_fmt(snap.get('price'),'price')} (today {_fmt(snap.get('change_pct'),'pct')})",
        f"52-week range: {_fmt(snap.get('wk_low'),'price')} - {_fmt(snap.get('wk_high'),'price')}",
        f"Market cap: {_fmt(snap.get('market_cap'),'cap')}  P/E: {_fmt(snap.get('pe'))}  "
        f"EPS: {_fmt(snap.get('eps'))}  Beta: {_fmt(snap.get('beta'))}",
        f"Trailing returns - 1M {_fmt(snap.get('ret_1m'),'pct')}, 3M {_fmt(snap.get('ret_3m'),'pct')}, "
        f"6M {_fmt(snap.get('ret_6m'),'pct')}, 1Y {_fmt(snap.get('ret_1y'),'pct')}",
        f"50-day MA: {_fmt(snap.get('ma50'),'price')}  200-day MA: {_fmt(snap.get('ma200'),'price')}",
    ])


def _build_prompt(snap: dict) -> str:
    return (
        "You are an equity analyst. Based ONLY on the data below, evaluate this "
        "stock for a retail investor over the coming week.\n"
        "Respond with ONLY a single JSON object (no markdown, no prose) with keys:\n"
        '  "rating": one of "STRONG BUY","BUY","HOLD","SELL","STRONG SELL"\n'
        '  "confidence": integer 0-100\n'
        '  "summary": one sentence, max 140 characters\n'
        '  "bullish": array of 2-3 short strings (key positives)\n'
        '  "bearish": array of 2-3 short strings (key risks)\n\n'
        f"DATA:\n{_data_block(snap)}\n"
    )


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(text[i:j + 1])
    except Exception:
        # Strip trailing commas and retry once.
        try:
            cleaned = re.sub(r",\s*([}\]])", r"\1", text[i:j + 1])
            return json.loads(cleaned)
        except Exception:
            return None


def _normalize_review(provider: str, model: str, label: str, raw: str) -> dict:
    obj = _extract_json(raw) or {}
    rating = str(obj.get("rating", "")).upper().strip()
    if rating not in _RATING_SCORE:
        # Best-effort scan of the raw text.
        for r in ("STRONG BUY", "STRONG SELL", "BUY", "SELL", "HOLD"):
            if r in (raw or "").upper():
                rating = r
                break
        else:
            rating = "HOLD"
    try:
        conf = int(float(obj.get("confidence", 0)))
    except Exception:
        conf = 0
    conf = max(0, min(100, conf))

    def _list(v):
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()][:3]
        if isinstance(v, str) and v.strip():
            return [v.strip()]
        return []

    summary = str(obj.get("summary", "")).strip()
    if not summary and raw:
        summary = raw.strip()[:140]
    return {
        "provider":   provider,
        "label":      label,
        "model":      model,
        "rating":     rating.title(),
        "confidence": conf,
        "summary":    summary,
        "bullish":    _list(obj.get("bullish")),
        "bearish":    _list(obj.get("bearish")),
    }


# ── Provider calls ───────────────────────────────────────────────────────────

def _call_anthropic(prompt: str, model: str, key: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=model, max_tokens=350,
        messages=[{"role": "user", "content": prompt}])
    return "".join(getattr(p, "text", "") for p in resp.content)


def _call_google(prompt: str, model: str, key: str) -> str:
    import google.generativeai as genai
    genai.configure(api_key=key)
    gm = genai.GenerativeModel(model)
    resp = gm.generate_content(
        prompt,
        generation_config=genai.types.GenerationConfig(max_output_tokens=350))
    return resp.text


def _call_openai_compat(prompt: str, model: str, key: str, base_url: str) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=key, base_url=base_url)
    resp = client.chat.completions.create(
        model=model, max_tokens=350,
        messages=[{"role": "user", "content": prompt}])
    return resp.choices[0].message.content


def _call_provider(provider: str, prompt: str, model: str, key: str) -> str:
    if provider == "anthropic":
        return _call_anthropic(prompt, model, key)
    if provider == "google":
        return _call_google(prompt, model, key)
    if provider == "xai":
        return _call_openai_compat(prompt, model, key, "https://api.x.ai/v1")
    if provider == "groq":
        return _call_openai_compat(prompt, model, key,
                                   "https://api.groq.com/openai/v1")
    raise ValueError(f"unknown provider {provider}")


# ── Consensus ────────────────────────────────────────────────────────────────

def _consensus(models: list[dict]) -> dict:
    scores = [_RATING_SCORE.get(m["rating"].upper(), 3) for m in models]
    if not scores:
        return {"rating": "—", "score": 0.0, "n": 0}
    avg = sum(scores) / len(scores)
    return {"rating": _SCORE_RATING[round(avg)], "score": round(avg, 2),
            "n": len(models)}


# ── Public API ───────────────────────────────────────────────────────────────

def _symbol_lock(symbol: str) -> threading.Lock:
    with _locks_guard:
        lk = _locks.get(symbol)
        if lk is None:
            lk = _locks[symbol] = threading.Lock()
        return lk


def evaluate(symbol: str, snapshot: dict | None = None,
             force: bool = False) -> dict:
    """Return the shared weekly evaluation for *symbol*, generating it (once)
    if this week's cache is missing or `force` is set."""
    sym = (symbol or "").strip().upper()
    week = current_week_monday()
    if not sym:
        return {"symbol": sym, "week": week, "consensus": {"rating": "—", "n": 0},
                "models": [], "error": "No symbol"}

    if not force:
        cached = _read_cache(sym, week)
        if cached and cached.get("models"):
            return cached

    lock = _symbol_lock(f"{week}/{sym}")
    with lock:
        # Re-check inside the lock — another request may have just built it.
        if not force:
            cached = _read_cache(sym, week)
            if cached and cached.get("models"):
                return cached

        keys = _main_account_keys()
        if not keys:
            return {"symbol": sym, "week": week,
                    "consensus": {"rating": "—", "score": 0.0, "n": 0},
                    "models": [], "generated_at": "",
                    "error": "No AI provider keys configured on the server's "
                             "main account."}

        snap = snapshot or {"symbol": sym}
        snap.setdefault("symbol", sym)
        prompt = _build_prompt(snap)

        # V4.6.130 — keep NON-BOT Claude spend down. The stock evaluation is
        # informational, so by default it uses the FREE / cheap providers
        # (Groq, Gemini, xAI) and SKIPS Anthropic (paid). Anthropic is only used
        # when it's the sole key configured. Override the set with
        # APEX_STOCK_EVAL_PROVIDERS="anthropic,groq" (comma-separated) if you
        # explicitly want Claude in the panel.
        override = os.environ.get("APEX_STOCK_EVAL_PROVIDERS", "").strip()
        if override:
            use = [p for p in (x.strip().lower() for x in override.split(","))
                   if p in keys]
        else:
            free = [p for p in PROVIDERS if p in keys and p != "anthropic"]
            use = free if free else [p for p in PROVIDERS if p in keys]
        if not use:
            use = [p for p in PROVIDERS if p in keys]

        models: list[dict] = []
        errors: list[str] = []
        for prov in use:
            cred_key, model, label = PROVIDERS[prov]
            key = keys.get(prov)
            if not key:
                continue
            try:
                raw = _call_provider(prov, prompt, model, key)
                models.append(_normalize_review(prov, model, label, raw))
            except Exception as e:
                errors.append(f"{label}: {e}")
                print(f"[stock_eval] {prov} failed for {sym}: {e}")

        result = {
            "symbol":       sym,
            "week":         week,
            "generated_at": datetime.datetime.now().isoformat(timespec="minutes"),
            "consensus":    _consensus(models),
            "models":       models,
            "error":        None if models else
                            ("All providers failed: " + "; ".join(errors)
                             if errors else "No reviews generated."),
        }
        if models:
            _write_cache(sym, week, result)
        return result
