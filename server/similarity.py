"""
APEX · Bot code similarity detection  (V3.3.0)
─────────────────────────────────────────────────────────────────────
Used by the marketplace publish flow to refuse near-duplicate uploads.
Algorithm: tokenize → normalize → 5-gram set → Jaccard similarity.

Robust to:
  • renamed identifiers / strings / numbers (all replaced with
    placeholders before n-gramming)
  • whitespace + comment churn (stripped entirely)
  • reordered top-level imports (n-grams catch local structure even
    when global layout changes)

NOT robust to:
  • semantic equivalence (e.g. rewriting a loop as a list comp)
  • intentional adversarial obfuscation (insert dead code, etc.)

Good enough for "is this a copy-paste of an existing bot with a few
identifiers renamed". Cost is O(N) over the existing marketplace; at
your current scale (<100 published bots) a check completes in a few
hundred ms.
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path
from typing import Iterable


def _fingerprint(src: str) -> set[int]:
    """Token 5-gram set for one source file. Identifiers / literals
    are normalised so 'def trade_long():' and 'def trade_short():'
    fingerprint identically (this is what the publish flow wants — we
    care about structural similarity, not surface names)."""
    toks = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                             tokenize.INDENT, tokenize.DEDENT,
                             tokenize.ENCODING, tokenize.ENDMARKER):
                continue
            if tok.type == tokenize.NAME:
                # Keep Python keywords distinct; collapse all other names
                kw = tok.string in {
                    "def","class","return","if","elif","else","for","while",
                    "import","from","as","try","except","finally","raise",
                    "with","yield","lambda","global","nonlocal","pass",
                    "break","continue","in","is","not","and","or","True",
                    "False","None","async","await",
                }
                toks.append(("KW", tok.string) if kw else ("NAME", "_"))
            elif tok.type == tokenize.STRING:
                toks.append(("STR", "_"))
            elif tok.type == tokenize.NUMBER:
                toks.append(("NUM", "_"))
            else:
                toks.append((tok.type, tok.string))
    except Exception:
        return set()
    if len(toks) < 5:
        return set()
    return {hash(tuple(toks[i:i+5])) for i in range(len(toks) - 4)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def compare_against_marketplace(
    candidate_src: str,
    marketplace_dir: Path,
    skip_slug: str | None = None,
    skip_slugs: list[str] | None = None,
) -> list[dict]:
    """Returns up to top-5 matches with score > 0.30, sorted descending.
    Caller decides what to do based on the scores (block / warn / pass).

    skip_slug:  legacy single-slug exclusion (re-publishing one bot).
    skip_slugs: V4.6.8 — list-form so the caller can exclude EVERY bot
                this user already published, allowing them to upload
                an improved version of their own work without hitting
                the >=85% same-author block."""
    cand_fp = _fingerprint(candidate_src)
    if not cand_fp:
        return []
    skip = set()
    if skip_slug:
        skip.add(skip_slug)
    if skip_slugs:
        skip.update(skip_slugs)
    matches = []
    for p in sorted(Path(marketplace_dir).glob("*.py")):
        if p.stem in skip:
            continue
        try:
            other = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        score = jaccard(cand_fp, _fingerprint(other))
        if score > 0.30:
            matches.append({"slug": p.stem, "score": round(score, 3)})
    matches.sort(key=lambda m: m["score"], reverse=True)
    return matches[:5]
