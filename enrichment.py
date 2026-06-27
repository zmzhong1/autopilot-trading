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


def stocknews_thesis(ticker, tree_text=None):
    """Return the StockNews thesis snapshot for a ticker. `tree_text` lets tests
    inject the markdown; otherwise reads local checkout then GitHub."""
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
        "h0": meta.get("h0"),
        "fatal_flags": fatal,
        "verdict": verdict_band(xii),
        "archetype": meta.get("archetype"),
        "review_due": meta.get("review_due"),
        "price": meta.get("price"),
    }


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


def enrich(ticker, account_value=None):
    """Combined per-trade context: {thesis, portfolio}."""
    return {
        "thesis": stocknews_thesis(ticker),
        "portfolio": portfolio_context(ticker, account_value=account_value),
    }
