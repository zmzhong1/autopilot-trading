#!/usr/bin/env python3
"""Finnhub signal layer — the broad, market-wide net that the SEC-by-CIK watcher
can't be. Adds three independent confluence feeds, keyed (FINNHUB_API_KEY,
free tier 60/min), reliable on cloud IPs:

  - insider  : open-market insider *purchases* (Form 3/4/5), MARKET-WIDE — this
               is what lets MU, CAT, and every other industry surface without
               maintaining a CIK list.
  - analyst  : a net upgrade in analyst recommendation trend month-over-month.
  - earnings : a recent positive earnings surprise (actual > estimate).

Each is fetched per-ticker over a bounded universe (the executor passes the
allow-list), so the call budget is ~3 x |allow_list|. Everything degrades to
"no signal" on any error (missing key, 403 premium, throttle) — a feed never
crashes the run. The pure scoring helpers (count/detect) are unit-tested; the
network wrappers are thin.

These plug straight into confluence's {ticker: {feeds:set, counts}} structure,
so a Finnhub insider hit and an SEC Form-4 hit collapse to the same 'insider'
feed (set semantics — no double counting), while 'analyst' and 'earnings' are
genuinely new corroboration dimensions.

Stdlib-only.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
BASE = "https://finnhub.io/api/v1"
USER_AGENT = "AutopilotWatcher-Finnhub/1.0"
LOAD_BUDGET_SEC = int(os.environ.get("FINNHUB_LOAD_BUDGET_SEC", "240"))
CALL_DELAY_SEC = float(os.environ.get("FINNHUB_CALL_DELAY_SEC", "0.25"))


def enabled():
    return bool(API_KEY)


def _get(path, params):
    """GET BASE/path?params&token=KEY -> parsed JSON, or None on any failure."""
    if not API_KEY:
        return None
    q = dict(params)
    q["token"] = API_KEY
    url = f"{BASE}{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[WARN] finnhub {path} failed: {e}", file=sys.stderr)
        return None
    finally:
        time.sleep(CALL_DELAY_SEC)


# -------------------- Pure scoring helpers (unit-tested) --------------------

def count_insider_buys(rows, since):
    """Count open-market insider PURCHASES (transactionCode 'P', positive share
    change) on/after `since` (YYYY-MM-DD). rows = Finnhub insider 'data' list."""
    n = 0
    for r in rows or []:
        code = (r.get("transactionCode") or "").upper()
        date = r.get("transactionDate") or ""
        try:
            change = float(r.get("change") or 0)
        except (TypeError, ValueError):
            change = 0
        if code == "P" and change > 0 and date >= since:
            n += 1
    return n


def is_net_upgrade(rec_rows):
    """True when the latest analyst recommendation month is more bullish than the
    prior month: (strongBuy+buy) rose, or (sell+strongSell) fell. rec_rows =
    Finnhub /stock/recommendation list (newest-first, as the API returns)."""
    rows = [r for r in (rec_rows or []) if r.get("period")]
    if len(rows) < 2:
        return False
    rows = sorted(rows, key=lambda r: r["period"])  # oldest -> newest
    prev, cur = rows[-2], rows[-1]

    def bull(r):
        return (r.get("strongBuy", 0) or 0) + (r.get("buy", 0) or 0)

    def bear(r):
        return (r.get("sell", 0) or 0) + (r.get("strongSell", 0) or 0)

    return bull(cur) > bull(prev) or bear(cur) < bear(prev)


def recent_beat(earnings_rows, since):
    """True when a reported quarter on/after `since` beat estimate. earnings_rows
    = Finnhub /stock/earnings list ({period, actual, estimate, surprisePercent})."""
    for r in earnings_rows or []:
        period = r.get("period") or ""
        if period < since:
            continue
        sp = r.get("surprisePercent")
        actual, est = r.get("actual"), r.get("estimate")
        if sp is not None:
            try:
                if float(sp) > 0:
                    return True
            except (TypeError, ValueError):
                pass
        elif actual is not None and est is not None:
            try:
                if float(actual) > float(est):
                    return True
            except (TypeError, ValueError):
                pass
    return False


# -------------------- Network wrappers (thin) --------------------

def insider_buy_count(ticker, since):
    data = _get("/stock/insider-transactions",
                {"symbol": ticker, "from": since, "to": _today()})
    rows = (data or {}).get("data", []) if isinstance(data, dict) else []
    return count_insider_buys(rows, since)


def analyst_upgrade(ticker):
    data = _get("/stock/recommendation", {"symbol": ticker})
    return is_net_upgrade(data if isinstance(data, list) else [])


def earnings_beat(ticker, since):
    data = _get("/stock/earnings", {"symbol": ticker})
    return recent_beat(data if isinstance(data, list) else [], since)


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def gather(universe, cutoff, budget_sec=None):
    """Scan `universe` (list of tickers) and return {ticker: {feed: count}} for
    the Finnhub feeds. `cutoff` is a datetime; we look back to its date. Returns
    {} if no key. Bounded by budget_sec so a slow run can't hang the workflow."""
    if not API_KEY:
        print("[INFO] FINNHUB_API_KEY not set; skipping Finnhub feeds.",
              file=sys.stderr)
        return {}
    since = cutoff.date().isoformat() if cutoff else _today()
    budget = budget_sec or LOAD_BUDGET_SEC
    deadline = time.monotonic() + budget
    out = {}
    scanned = 0
    for ticker in universe or []:
        if time.monotonic() > deadline:
            print(f"[WARN] Finnhub budget hit after {scanned} tickers; "
                  f"{len(universe) - scanned} unscanned", file=sys.stderr)
            break
        scanned += 1
        feeds = {}
        buys = insider_buy_count(ticker, since)
        if buys:
            feeds["insider"] = buys
        if analyst_upgrade(ticker):
            feeds["analyst"] = 1
        if earnings_beat(ticker, since):
            feeds["earnings"] = 1
        if feeds:
            out[ticker] = feeds
    print(f"[FINNHUB] scanned {scanned} tickers, {len(out)} with signals")
    return out
