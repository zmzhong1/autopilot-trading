#!/usr/bin/env python3
"""Per-trade context: read the StockNews thesis and the portfolio position for a
ticker so every proposal is *grounded in* and *acknowledges* the research and
the existing book — not just the raw confluence signal.

Two sources, both read at proposal time:

  - StockNews (zmzhong1/StockNews): the INDEX_META block in
    reports/{TICKER}/tree_v1_en.md — XII scorecard, fatal flags, H-0 confidence,
    archetype. Read from a local checkout (STOCKNEWS_REPO_PATH) when present,
    else fetched from GitHub (raw + contents-API fallback, same auth as
    stocknews_digest.py). This is the "should we own it at all" view.

  - Stock-Portfolio (zmzhong1/stock-portfolio): current holding for the ticker
    via GET /api/portfolio, when STOCK_PORTFOLIO_URL + STOCK_PORTFOLIO_TOKEN are
    set. Degrades to "not checked" otherwise. This is the "do we already hold it
    / does it fit the allocation" view.

The executor turns this context into a gate (skip trades the research contradicts
or that would over-concentrate) AND an acknowledgment line carried on the
proposal, into the Discord card, and into the committed track record.

Stdlib-only.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

USER_AGENT = "AutopilotWatcher-Enrichment/1.0"

STOCKNEWS_REPO = os.environ.get("STOCKNEWS_REPO", "zmzhong1/StockNews")
STOCKNEWS_BRANCH = os.environ.get("STOCKNEWS_BRANCH", "phase-1-scaffold")
STOCKNEWS_REPO_PATH = os.environ.get("STOCKNEWS_REPO_PATH", "").strip()
GH_TOKEN = (os.environ.get("STOCKNEWS_GH_TOKEN") or os.environ.get("GH_TOKEN")
            or os.environ.get("GITHUB_TOKEN"))

STOCK_PORTFOLIO_URL = os.environ.get("STOCK_PORTFOLIO_URL", "").strip().rstrip("/")
STOCK_PORTFOLIO_TOKEN = os.environ.get("STOCK_PORTFOLIO_TOKEN", "").strip()

# XII-score verdict bands (StockNews MANUAL Part K.3.5, mirrored in its CLAUDE.md):
#   >=85 strong · >=65 moderate-buy-with-sizing · >=45 wait/skip · <45 avoid
BAND_STRONG, BAND_MODERATE, BAND_WAIT = 85, 65, 45


def _read_local_tree(ticker):
    if not STOCKNEWS_REPO_PATH:
        return None
    p = Path(STOCKNEWS_REPO_PATH) / "reports" / ticker / "tree_v1_en.md"
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def _fetch_remote_tree(ticker):
    path = f"reports/{ticker}/tree_v1_en.md"
    raw_url = (f"https://raw.githubusercontent.com/{STOCKNEWS_REPO}/"
               f"{STOCKNEWS_BRANCH}/{path}")
    headers = {"User-Agent": USER_AGENT}
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"
    try:
        with urllib.request.urlopen(
                urllib.request.Request(raw_url, headers=headers), timeout=20) as r:
            return r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code != 404 or not GH_TOKEN:
            return None
    except Exception:
        return None
    api_url = (f"https://api.github.com/repos/{STOCKNEWS_REPO}/contents/"
               f"{path}?ref={STOCKNEWS_BRANCH}")
    api_headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github.raw",
                   "Authorization": f"Bearer {GH_TOKEN}"}
    try:
        with urllib.request.urlopen(
                urllib.request.Request(api_url, headers=api_headers), timeout=20) as r:
            return r.read().decode("utf-8")
    except Exception:
        return None


def parse_index_meta(text):
    """Pull the INDEX_META key/value block out of a tree_v1_en.md into a dict.
    Returns {} if no block. Pure."""
    if not text:
        return {}
    m = re.search(r"<!--\s*INDEX_META(.*?)-->", text, re.DOTALL)
    if not m:
        return {}
    meta = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k:
            meta[k] = v
    return meta


def _pct_to_int(s):
    try:
        return int(str(s).replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def verdict_band(xii_score):
    """Map an XII score (int %) to a verdict label. None -> 'unknown'."""
    if xii_score is None:
        return "unknown"
    if xii_score >= BAND_STRONG:
        return "strong-buy"
    if xii_score >= BAND_MODERATE:
        return "moderate-buy"
    if xii_score >= BAND_WAIT:
        return "wait"
    return "avoid"


def _parse_prob(s):
    """'28/52/20' -> (bull, base, bear) ints, or None."""
    try:
        parts = [int(x) for x in str(s).split("/")]
        return tuple(parts[:3]) if len(parts) >= 3 else None
    except (TypeError, ValueError):
        return None


def _parse_durability(s):
    """'21/25' -> 21, or None."""
    try:
        return int(str(s).split("/")[0].strip())
    except (TypeError, ValueError, IndexError):
        return None


def _parse_price(s):
    """'$1,014.53' -> 1014.53, or None."""
    try:
        return float(str(s).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def stocknews_thesis(ticker, tree_text=None):
    """Return the StockNews thesis snapshot for a ticker — the full research
    picture, not just the headline score. `tree_text` lets tests inject the
    markdown; otherwise reads local checkout then GitHub."""
    if tree_text is None:
        tree_text = _read_local_tree(ticker) or _fetch_remote_tree(ticker)
    meta = parse_index_meta(tree_text)
    if not meta:
        return {"found": False, "ticker": ticker}
    xii = _pct_to_int(meta.get("xii_score"))
    try:
        fatal = int(meta.get("fatal_flags", "0"))
    except (TypeError, ValueError):
        fatal = 0
    return {
        "found": True,
        "ticker": ticker,
        "xii_score": xii,
        "h0": _pct_to_int(meta.get("h0")),          # thesis confidence
        "prob": _parse_prob(meta.get("prob")),       # (bull, base, bear)
        "durability": _parse_durability(meta.get("durability")),  # /25
        "fatal_flags": fatal,
        "verdict": verdict_band(xii),
        "archetype": meta.get("archetype"),
        "archetype_category": meta.get("archetype_category"),
        "mispricing_source": meta.get("mispricing_source"),
        "review_due": meta.get("review_due"),
        "updated": meta.get("updated"),
        "anchor_price": _parse_price(meta.get("price")),
    }


def assess_purchase(thesis, today=None):
    """Apply StockNews's research framework to decide whether a name is a good
    *purchase* right now — not just a good company. Pure.

    Combines quality (XII), thesis confidence (H-0), long-term durability (/25),
    and bull/bear asymmetry into an explainable conviction band, then caps it
    when the thesis is stale (past its review_due) or the mispricing/archetype
    flags an unfavorable setup. Returns
    {conviction, score, good_purchase, stale, asymmetry, reasons}.

    This is why XII 91% COST (H-0 60%, bull<bear, 'unfavorable') lands at LOW
    while XII 90% GOOGL (H-0 90%, bull>bear, fresh) lands at HIGH."""
    if not thesis or not thesis.get("found"):
        return {"conviction": "none", "score": 0, "good_purchase": False,
                "stale": False, "asymmetry": None,
                "reasons": ["no StockNews thesis"]}

    xii = thesis.get("xii_score")
    h0 = thesis.get("h0")
    dur = thesis.get("durability")
    prob = thesis.get("prob")
    reasons = []

    if thesis.get("fatal_flags", 0) > 0 or (xii is not None and xii < BAND_WAIT):
        return {"conviction": "avoid", "score": 0, "good_purchase": False,
                "stale": False, "asymmetry": None,
                "reasons": [f"XII {xii}% / {thesis.get('fatal_flags')} fatal flag(s)"]}

    score = 0
    if xii is not None:
        score += 2 if xii >= BAND_STRONG else 1 if xii >= BAND_MODERATE else 0
        reasons.append(f"XII {xii}%")
    if h0 is not None:
        score += 2 if h0 >= 75 else 1 if h0 >= 55 else 0
        reasons.append(f"H-0 {h0}%" + (" (low confidence)" if h0 < 55 else ""))
    if dur is not None:
        score += 1 if dur >= 20 else 0 if dur >= 17 else -1
        reasons.append(f"durability {dur}/25")

    asymmetry = None
    if prob and len(prob) == 3:
        bull, _, bear = prob
        if bull > bear:
            score += 1
            asymmetry = "favorable"
        elif bear > bull:
            score -= 1
            asymmetry = "unfavorable"
            reasons.append(f"asymmetry unfavorable (bull {bull} < bear {bear})")
        else:
            asymmetry = "balanced"

    mis = (thesis.get("mispricing_source") or "")
    cat = (thesis.get("archetype_category") or "")
    if "unfavorable" in mis or "under-review" in cat or "fragile" in cat:
        score -= 1
        reasons.append(f"setup flag ({mis or cat})")

    # Freshness: a thesis past its review_due can't be fully trusted.
    stale = False
    rd = _parse_date(thesis.get("review_due"))
    if rd and today and rd < today:
        stale = True
        reasons.append(f"thesis stale (review due {thesis['review_due']})")

    if score >= 6:
        conviction = "high"
    elif score >= 3:
        conviction = "medium"
    elif score >= 1:
        conviction = "low"
    else:
        conviction = "avoid"

    # A stale thesis caps conviction at medium — re-review before high conviction.
    if stale and conviction == "high":
        conviction = "medium"

    return {"conviction": conviction, "score": score,
            "good_purchase": conviction in ("high", "medium"),
            "stale": stale, "asymmetry": asymmetry, "reasons": reasons}


def _parse_date(s):
    try:
        from datetime import date
        y, m, d = (int(x) for x in str(s).split("-"))
        return date(y, m, d)
    except (TypeError, ValueError):
        return None


def portfolio_context(ticker, account_value=None, _opener=None):
    """Return the current holding for a ticker from the Stock-Portfolio app, or
    {"checked": False} when no credentials are configured. `_opener` is
    injectable for tests; it receives a urllib Request and returns decoded JSON
    text."""
    if not (STOCK_PORTFOLIO_URL and STOCK_PORTFOLIO_TOKEN) and _opener is None:
        return {"checked": False, "reason": "no STOCK_PORTFOLIO_URL/TOKEN"}
    url = f"{STOCK_PORTFOLIO_URL}/api/portfolio"
    headers = {"User-Agent": USER_AGENT,
               "Authorization": f"Bearer {STOCK_PORTFOLIO_TOKEN}"}
    try:
        if _opener is not None:
            data = json.loads(_opener(url))
        else:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"checked": False, "reason": f"fetch failed: {e}"}
    holdings = data.get("holdings", []) if isinstance(data, dict) else []
    held = next((h for h in holdings if (h.get("ticker") or "").upper()
                 == ticker.upper()), None)
    stock_alloc = None
    if isinstance(data, dict):
        stock_alloc = (data.get("allocation", {}) or {}).get("stock_pct")
    return {
        "checked": True,
        "held": bool(held),
        "shares": (held or {}).get("shares"),
        "value_usd": (held or {}).get("market_value") or (held or {}).get("value"),
        "stock_alloc_pct": stock_alloc,
        "target_stock_pct": (data.get("target_stock_pct")
                             if isinstance(data, dict) else None),
    }


def research_note(ticker, root=None):
    """Read the latest daily-research cache (research/{TICKER}.json, written by
    research.py) for a ticker. Returns {flags, researched_at} or {} when absent.
    Lets a trade surface the freshest understanding/flags about the name."""
    from pathlib import Path as _Path
    base = _Path(root) if root else _Path(__file__).parent
    p = base / "research" / f"{ticker}.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    snap = data.get("snapshot", {}) if isinstance(data, dict) else {}
    return {"flags": data.get("flags", []),
            "researched_at": snap.get("researched_at")}


def enrich(ticker, account_value=None):
    """Combined per-trade context: {thesis, portfolio, research}."""
    return {
        "thesis": stocknews_thesis(ticker),
        "portfolio": portfolio_context(ticker, account_value=account_value),
        "research": research_note(ticker),
    }
