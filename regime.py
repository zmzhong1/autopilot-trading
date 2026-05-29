#!/usr/bin/env python3
"""Market regime gauge — a weekly "risk weather" report.

This does NOT predict crashes (nobody can). It *describes* current conditions
from objective, free, public data and rolls them into a calm / mixed / stressed
read so you have context, not a forecast.

Signals (all free):
  - Trend       — S&P 500 vs its 200-day moving average
  - Volatility  — VIX level
  - Yield curve — 10y minus 2y Treasury spread
  - Credit      — high-yield OAS credit spread

FRED (free key from https://fred.stlouisfed.org/docs/api/api_key.html) is the
preferred source — one reliable API covers all four signals. Without a key the
gauge falls back to Stooq for the S&P/VIX legs only (Stooq is keyless but rate-
limits / 403s more often), and skips the macro legs. Set FRED_API_KEY as a repo
secret for the full, dependable gauge.

Stdlib-only. Local run:
    DISCORD_WEBHOOK='https://discord.com/api/webhooks/...' python3 regime.py
Dry-run:
    DRY_RUN=1 python3 regime.py
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()
DISCORD_RATE_DELAY_SEC = 0.5

COLOR_CALM = 0x2ECC71      # green
COLOR_MIXED = 0xF1C40F     # amber
COLOR_STRESSED = 0xE74C3C  # red

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# -------------------- Stooq (price / VIX) --------------------

def fetch_stooq_closes(symbol):
    """Return chronological list of daily closes for a Stooq symbol, or []."""
    url = f"https://stooq.com/q/d/l/?s={symbol}&i=d"
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[WARN] Stooq fetch failed for {symbol}: {e}", file=sys.stderr)
        return []
    lines = text.strip().splitlines()
    if len(lines) < 2 or not lines[0].lower().startswith("date"):
        print(f"[WARN] Stooq returned unexpected data for {symbol}: {lines[:1]}",
              file=sys.stderr)
        return []
    closes = []
    for row in lines[1:]:
        cols = row.split(",")
        if len(cols) < 5:
            continue
        try:
            closes.append(float(cols[4]))  # Date,Open,High,Low,Close,Volume
        except ValueError:
            continue
    return closes


def sma(values, window):
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


# -------------------- FRED (macro, optional) --------------------

def fetch_fred_series(series_id, limit=300):
    """Chronological list of recent non-missing values for a FRED series, or [].
    Needs a key. `limit` is the number of most-recent observations to pull."""
    if not FRED_API_KEY:
        return []
    url = (f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}"
           f"&api_key={FRED_API_KEY}&file_type=json&sort_order=desc&limit={limit}")
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[WARN] FRED fetch failed for {series_id}: {e}", file=sys.stderr)
        return []
    vals = []
    for obs in data.get("observations", []):  # newest-first
        val = obs.get("value")
        if val and val != ".":
            try:
                vals.append(float(val))
            except ValueError:
                continue
    vals.reverse()  # chronological
    return vals


def fetch_fred_latest(series_id):
    """Latest non-missing observation for a FRED series, or None."""
    vals = fetch_fred_series(series_id, limit=10)
    return vals[-1] if vals else None


# -------------------- Scoring (pure, testable) --------------------

def assess_regime(spx_close, spx_sma200, vix, curve_spread=None, hy_spread=None):
    """Combine the available signals into a risk score + per-component read.

    Returns {score, level, color, components:[{name, value, read, risk}]}.
    `score` sums per-component risk points; level is calm/mixed/stressed. Missing
    inputs are simply skipped (the gauge degrades gracefully).
    """
    components = []
    score = 0

    if spx_close is not None and spx_sma200:
        pct = (spx_close / spx_sma200 - 1) * 100
        if spx_close >= spx_sma200:
            read, risk = f"above 200dma (+{pct:.1f}%)", 0
        else:
            read, risk = f"below 200dma ({pct:.1f}%)", 1
        components.append({"name": "📈 Trend (S&P 500)", "value": f"{spx_close:,.0f}",
                           "read": read, "risk": risk})
        score += risk

    if vix is not None:
        if vix < 15:
            read, risk = "calm", 0
        elif vix < 20:
            read, risk = "normal", 0
        elif vix < 25:
            read, risk = "slightly elevated", 1
        elif vix < 35:
            read, risk = "elevated", 2
        else:
            read, risk = "stressed", 3
        components.append({"name": "🌪️ Volatility (VIX)", "value": f"{vix:.1f}",
                           "read": read, "risk": risk})
        score += risk

    if curve_spread is not None:
        if curve_spread < 0:
            read, risk = "inverted (recession watch)", 1
        elif curve_spread < 0.5:
            read, risk = "flat", 0
        else:
            read, risk = "normal", 0
        components.append({"name": "📉 Yield curve (10y−2y)", "value": f"{curve_spread:+.2f}%",
                           "read": read, "risk": risk})
        score += risk

    if hy_spread is not None:
        if hy_spread < 3.5:
            read, risk = "tight", 0
        elif hy_spread < 5:
            read, risk = "normal", 0
        elif hy_spread < 7:
            read, risk = "widening", 1
        else:
            read, risk = "stressed", 2
        components.append({"name": "💳 Credit (HY OAS)", "value": f"{hy_spread:.2f}%",
                           "read": read, "risk": risk})
        score += risk

    if score <= 1:
        level, color = "🟢 Calm", COLOR_CALM
    elif score <= 3:
        level, color = "🟡 Mixed — stay alert", COLOR_MIXED
    else:
        level, color = "🔴 Stressed", COLOR_STRESSED
    return {"score": score, "level": level, "color": color, "components": components}


# -------------------- Discord --------------------

def post_discord(embed):
    if not DISCORD_WEBHOOK:
        return False
    body = json.dumps({"embeds": [embed]}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK,
        data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "AutopilotWatcher-Regime/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status in (200, 204)
    except Exception as e:
        print(f"[ERROR] Discord post failed: {e}", file=sys.stderr)
        return False
    finally:
        time.sleep(DISCORD_RATE_DELAY_SEC)


def build_embed(regime):
    fields = [{"name": c["name"], "value": f"{c['value']} — {c['read']}", "inline": True}
              for c in regime["components"]]
    return {
        "title": f"🌡️ Market regime — {regime['level']}",
        "description": ("A snapshot of current market *conditions* (not a forecast). "
                        "Risk points sum across the signals below — more points = "
                        "more fragile backdrop."),
        "color": regime["color"],
        "fields": fields or [{"name": "No data", "value": "_no signals available_",
                              "inline": False}],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"Autopilot Trading — regime gauge · risk score {regime['score']}"},
    }


def main():
    print(f"[START] regime gauge — DRY_RUN={DRY_RUN}  "
          f"FRED={'on' if FRED_API_KEY else 'off (Stooq fallback)'}")

    # Prefer FRED (one reliable source for everything); fall back to Stooq for
    # the price/vol legs if no key is configured.
    spx = fetch_fred_series("SP500", limit=300) or fetch_stooq_closes("^spx")
    spx_close = spx[-1] if spx else None
    spx_sma200 = sma(spx, 200)

    vix = fetch_fred_latest("VIXCLS")
    if vix is None:
        vix_series = fetch_stooq_closes("^vix")
        vix = vix_series[-1] if vix_series else None

    curve = fetch_fred_latest("T10Y2Y")      # 10y-2y spread, %
    hy = fetch_fred_latest("BAMLH0A0HYM2")    # HY OAS, %

    if spx_close is None and vix is None and curve is None and hy is None:
        print("[ERROR] No signals could be fetched; skipping post.", file=sys.stderr)
        return 1

    regime = assess_regime(spx_close, spx_sma200, vix, curve, hy)
    print(f"[REGIME] {regime['level']} (score {regime['score']}, "
          f"{len(regime['components'])} signals)")

    embed = build_embed(regime)
    if DRY_RUN:
        print(json.dumps(embed, indent=2))
        return 0
    ok = post_discord(embed)
    print(f"[DONE] posted={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
