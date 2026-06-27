#!/usr/bin/env python3
"""Tiny keyless equity-price helper (Stooq daily close).

Shared by executor.py (capture an entry price when a proposal fires) and
scorecard.py (mark open proposals to market). Stooq is the same keyless source
regime.py already uses for the S&P/VIX legs — no new dependency, no API key.

US equities use the `{ticker}.us` Stooq symbol. Returns None on any failure so
callers degrade (skip the entry price / skip that row) rather than crash.

Stdlib-only.
"""

import sys
import urllib.request

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def stooq_symbol(ticker):
    """Map a US ticker to its Stooq symbol. Class shares use a dash on Stooq
    (BRK.B -> brk-b.us); we lower-case and swap '.' for '-'."""
    return f"{ticker.strip().lower().replace('.', '-')}.us"


def latest_close(ticker):
    """Most recent daily close for a US equity ticker as a float, or None."""
    symbol = stooq_symbol(ticker)
    url = f"https://stooq.com/q/d/l/?s={symbol}&i=d"
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[WARN] price fetch failed for {ticker}: {e}", file=sys.stderr)
        return None
    lines = text.strip().splitlines()
    if len(lines) < 2 or not lines[0].lower().startswith("date"):
        return None
    # Walk from the end for the last parseable Close (Date,Open,High,Low,Close,Vol).
    for row in reversed(lines[1:]):
        cols = row.split(",")
        if len(cols) < 5:
            continue
        try:
            return float(cols[4])
        except ValueError:
            continue
    return None


def latest_closes(tickers):
    """Best-effort {ticker: close} for a list of tickers; omits any that fail."""
    out = {}
    for t in tickers:
        c = latest_close(t)
        if c is not None:
            out[t] = c
    return out
