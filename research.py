#!/usr/bin/env python3
"""Daily company-research routine — uses the free Finnhub budget to *understand*
the companies we trade, a rotating slice at a time.

Rather than shallow-poll all ~50 allow-list names every day, this researches a
deterministic slice (~RESEARCH_PER_DAY/day → the whole list weekly), going deep
per name: fundamentals, recent news, earnings, analyst trend. For each it builds
an "understanding" snapshot, cross-checks the StockNews thesis, and:

  1. caches research/{TICKER}.json (committed) so the executor's enrichment can
     read the latest understanding right inside a trade decision;
  2. flags theses that look stale or contradicted (analyst downgrade vs a
     buy-rated thesis, earnings miss vs high conviction, past review_due);
  3. posts a daily digest to the agentic Discord channel.

Pure helpers (slice, metric pick, flag detection) are unit-tested; the Finnhub
fetches reuse finnhub_signals. Degrades to "nothing researched" without
FINNHUB_API_KEY. Stdlib-only.

Local run:
    FINNHUB_API_KEY=... DRY_RUN=1 python3 research.py
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

ROOT = Path(__file__).parent
GUARDRAILS_PATH = ROOT / "guardrails.json"
RESEARCH_DIR = ROOT / "research"

DISCORD_WEBHOOK = (os.environ.get("EXECUTOR_DISCORD_WEBHOOK", "").strip()
                   or os.environ.get("DISCORD_WEBHOOK", "").strip())
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
PER_DAY = int(os.environ.get("RESEARCH_PER_DAY", "7"))
NEWS_LOOKBACK_DAYS = int(os.environ.get("RESEARCH_NEWS_DAYS", "7"))
DISCORD_RATE_DELAY_SEC = 0.5

COLOR_RESEARCH = 0x1ABC9C
COLOR_FLAGGED = 0xE67E22


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


# -------------------- Pure helpers (unit-tested) --------------------

def daily_slice(universe, ordinal, per_day):
    """Deterministic rotating slice of `universe` for a given day `ordinal`
    (e.g. day-of-year). Chunks the sorted universe into groups of `per_day` and
    returns the group for today, so the full list is covered every
    ceil(n/per_day) days. Pure."""
    uni = sorted({t for t in (universe or []) if t})
    if not uni or per_day <= 0:
        return []
    groups = [uni[i:i + per_day] for i in range(0, len(uni), per_day)]
    return groups[ordinal % len(groups)]


_METRIC_FIELDS = [
    ("peTTM", "P/E"),
    ("netProfitMarginTTM", "net margin %"),
    ("revenueGrowthTTMYoy", "rev growth %"),
    ("roeTTM", "ROE %"),
    ("currentRatioQuarterly", "current ratio"),
]


def pick_metrics(metric_payload):
    """Extract a small, human-readable set of fundamentals from a Finnhub
    /stock/metric payload ({'metric': {...}}). Returns {label: value}. Pure."""
    m = (metric_payload or {}).get("metric", {}) if isinstance(metric_payload, dict) else {}
    out = {}
    for key, label in _METRIC_FIELDS:
        v = m.get(key)
        if v is None:
            continue
        try:
            out[label] = round(float(v), 2)
        except (TypeError, ValueError):
            continue
    return out


def compute_flags(thesis, snapshot, today=None):
    """Compare fresh research against the StockNews thesis and return a list of
    one-line flags worth a human's attention. Pure.

    snapshot keys used: analyst_upgrade (bool), earnings_beat (bool)."""
    flags = []
    if not thesis or not thesis.get("found"):
        flags.append("no StockNews thesis on file")
        return flags

    verdict = thesis.get("verdict")
    buy_rated = verdict in ("strong-buy", "moderate-buy")

    # Freshness: thesis past its review_due.
    rd = _parse_date(thesis.get("review_due"))
    if rd and today and rd < today:
        flags.append(f"thesis stale (review due {thesis['review_due']})")

    # Divergence: analysts cooling on a name the thesis rates a buy.
    if buy_rated and snapshot.get("analyst_upgrade") is False:
        flags.append("analyst trend not upgrading vs buy-rated thesis")

    # Earnings miss under high-confidence thesis.
    h0 = thesis.get("h0")
    if snapshot.get("earnings_beat") is False and h0 is not None and h0 >= 75:
        flags.append("recent earnings did not beat, despite high H-0")

    return flags


def _parse_date(s):
    try:
        from datetime import date
        y, m, d = (int(x) for x in str(s).split("-"))
        return date(y, m, d)
    except (TypeError, ValueError):
        return None


# -------------------- Finnhub fetch (reuses finnhub_signals) --------------------

def research_company(ticker, since_date, today_date):
    """Build the per-company understanding snapshot from Finnhub. Returns a dict;
    fields are best-effort (any failed sub-fetch is simply omitted)."""
    import finnhub_signals as fh
    snap = {"ticker": ticker, "researched_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")}

    profile = fh._get("/stock/profile2", {"symbol": ticker})
    if isinstance(profile, dict):
        snap["name"] = profile.get("name")
        snap["industry"] = profile.get("finnhubIndustry")
        snap["market_cap"] = profile.get("marketCapitalization")

    snap["metrics"] = pick_metrics(fh._get("/stock/metric",
                                           {"symbol": ticker, "metric": "all"}))

    news = fh._get("/company-news",
                   {"symbol": ticker, "from": since_date, "to": today_date})
    if isinstance(news, list):
        snap["news"] = [n.get("headline") for n in news[:3]
                        if isinstance(n, dict) and n.get("headline")]

    snap["analyst_upgrade"] = fh.analyst_upgrade(ticker)
    snap["earnings_beat"] = fh.earnings_beat(ticker, since_date)
    return snap


def write_cache(ticker, snapshot, flags, path_dir=RESEARCH_DIR):
    """Persist research/{TICKER}.json (committed) for the executor to read."""
    try:
        path_dir.mkdir(exist_ok=True)
        (path_dir / f"{ticker}.json").write_text(
            json.dumps({"snapshot": snapshot, "flags": flags}, indent=2),
            encoding="utf-8")
    except OSError as e:
        print(f"[WARN] could not cache research for {ticker}: {e}", file=sys.stderr)


# -------------------- Discord --------------------

def post_discord(embed):
    if not DISCORD_WEBHOOK:
        return False
    body = json.dumps({"embeds": [embed]}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK, data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "AutopilotWatcher-Research/1.0"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status in (200, 204)
    except Exception as e:
        print(f"[ERROR] Discord post failed: {e}", file=sys.stderr)
        return False
    finally:
        time.sleep(DISCORD_RATE_DELAY_SEC)


def build_embed(results, today_slice):
    flagged = [r for r in results if r["flags"]]
    color = COLOR_FLAGGED if flagged else COLOR_RESEARCH
    lines = []
    for r in results:
        s = r["snapshot"]
        bits = []
        if s.get("metrics", {}).get("P/E") is not None:
            bits.append(f"P/E {s['metrics']['P/E']}")
        bits.append("📈 analyst↑" if s.get("analyst_upgrade") else "analyst→")
        bits.append("🟢 beat" if s.get("earnings_beat") else "earnings—")
        flag = f" · ⚠️ {r['flags'][0]}" if r["flags"] else ""
        name = s.get("name") or s["ticker"]
        lines.append(f"**{s['ticker']}** ({name[:24]}) — {' · '.join(bits)}{flag}")
    fields = [{"name": f"Researched today ({len(results)})",
               "value": "\n".join(lines)[:1024] or "_none_", "inline": False}]
    if flagged:
        fl = [f"`{r['snapshot']['ticker']}` — {'; '.join(r['flags'])}" for r in flagged]
        fields.append({"name": f"⚠️ Thesis flags ({len(flagged)})",
                       "value": "\n".join(fl)[:1024], "inline": False})
    return {
        "title": "🔬 Daily company research",
        "description": (f"Rotating deep-dive on {len(today_slice)} names "
                        "(full watchlist weekly). Fundamentals + news + earnings "
                        "+ analyst trend, cross-checked against the StockNews thesis."),
        "color": color, "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Autopilot Trading — daily research"},
    }


def main():
    import enrichment
    gr = load_json(GUARDRAILS_PATH, {})
    al = gr.get("allow_list", [])
    universe = [t for t in al if isinstance(t, str) and t] if isinstance(al, list) else []
    now = datetime.now(timezone.utc)
    today = now.date()
    ordinal = today.toordinal()
    today_slice = daily_slice(universe, ordinal, PER_DAY)

    print(f"[START] research — {len(today_slice)} of {len(universe)} names today "
          f"(per_day={PER_DAY})  DRY_RUN={DRY_RUN}")
    import finnhub_signals
    if not finnhub_signals.enabled():
        print("[INFO] FINNHUB_API_KEY not set; nothing to research.")
        return 0

    since = (today - timedelta(days=NEWS_LOOKBACK_DAYS)).isoformat()
    results = []
    for ticker in today_slice:
        snap = research_company(ticker, since, today.isoformat())
        thesis = enrichment.stocknews_thesis(ticker)
        flags = compute_flags(thesis, snap, today=today)
        if not DRY_RUN:
            write_cache(ticker, snap, flags)
        results.append({"snapshot": snap, "flags": flags})
        print(f"  {ticker}: {len(flags)} flag(s)"
              + (f" — {flags[0]}" if flags else ""))

    embed = build_embed(results, today_slice)
    if DRY_RUN:
        print(json.dumps(embed, indent=2))
        return 0
    ok = post_discord(embed) if DISCORD_WEBHOOK else True
    print(f"[DONE] posted={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    import producer_status
    rc = main()
    producer_status.record("research", ok=(rc == 0))
    sys.exit(rc)
