#!/usr/bin/env python3
"""Cross-feed confluence — surfaces tickers lit up by multiple independent feeds
at once: politicians, corporate insiders, 8-K events, and crowded 13F holdings.

Any one feed is noise on its own. But when a congressman buys $X, a corporate
insider buys on the open market, AND multiple funds hold it — that overlap is
worth a look. This stitches together signals the project already collects:

  - congress    — recent Capitol Trades from congress_state.json alert_history
  - insider     — recent Form 4 alerts from state.json alert_history
  - corporate   — recent 8-K alerts from state.json alert_history
  - institutional — crowded names from the latest 13F-HRs (via crowding.py)

…all keyed to a ticker (watchlist CIK→ticker via SEC's company_tickers.json, and
best-effort issuer-name matching for 13F holdings).

Stdlib-only. Local run:
    SEC_USER_AGENT='Your Name your@email.com' \
    DISCORD_WEBHOOK='https://discord.com/api/webhooks/...' \
    python3 confluence.py
Dry-run:
    SEC_USER_AGENT='Your Name your@email.com' DRY_RUN=1 python3 confluence.py
"""

import json
import os
import re
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
WATCHLIST_PATH = ROOT / "watchlist.json"
SEC_STATE_PATH = ROOT / "state.json"
CONGRESS_STATE_PATH = ROOT / "congress_state.json"

USER_AGENT = os.environ.get("SEC_USER_AGENT", "").strip()
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
LOOKBACK_DAYS = int(os.environ.get("CONFLUENCE_LOOKBACK_DAYS", "30"))
MIN_FEEDS = int(os.environ.get("CONFLUENCE_MIN_FEEDS", "2"))
TOP_N = int(os.environ.get("CONFLUENCE_TOP_N", "12"))
INCLUDE_13F = os.environ.get("CONFLUENCE_INCLUDE_13F", "1").lower() in ("1", "true", "yes")
DISCORD_RATE_DELAY_SEC = 0.5

COLOR_CONFLUENCE = 0xE67E22  # orange

if not USER_AGENT:
    sys.exit(
        "ERROR: SEC_USER_AGENT not set. SEC requires a contact string.\n"
        "  Example: SEC_USER_AGENT='Your Name your@email.com'"
    )

SEC_HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*"}

FEED_EMOJI = {"congress": "🏛️", "insider": "🧑‍💼", "corporate": "📋",
              "institutional": "🏦"}

_CT_TICKER_RE = re.compile(r"\b([A-Z][A-Z0-9.]{0,5}):(?:US|N|Q|UN|CA|UK)\b")
_NAME_NOISE = {"INC", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "PLC", "LLC",
               "CL", "CLASS", "A", "B", "C", "THE", "HOLDINGS", "HOLDING", "GROUP",
               "COM", "COMMON", "STOCK", "SA", "NV", "AG", "TRUST"}


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00") if s.endswith("Z") else s)
    except ValueError:
        return None


def extract_ticker(issuer_cell):
    """Capitol Trades issuer cells look like 'Apple Inc AAPL:US' -> 'AAPL'."""
    m = _CT_TICKER_RE.search(issuer_cell or "")
    return m.group(1) if m else None


def normalize_name(name):
    s = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper())
    return " ".join(t for t in s.split() if t not in _NAME_NOISE)


def fetch_company_tickers():
    """SEC CIK→ticker map; also returns a normalized-name→ticker index."""
    url = "https://www.sec.gov/files/company_tickers.json"
    req = urllib.request.Request(url, headers=SEC_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[WARN] company_tickers.json fetch failed: {e}", file=sys.stderr)
        return {}, {}
    by_cik, by_name = {}, {}
    for v in raw.values():
        ticker = (v.get("ticker") or "").upper()
        if not ticker:
            continue
        by_cik[str(v["cik_str"]).zfill(10)] = ticker
        norm = normalize_name(v.get("title", ""))
        if norm and norm not in by_name:
            by_name[norm] = ticker
    return by_cik, by_name


# -------------------- Signal collection --------------------

def collect_signals(sec_history, congress_history, name_to_ticker, cik_to_ticker,
                    crowded=None, cutoff=None, today=None):
    """Build {ticker: {issuer, feeds:set, counts:{feed:int}}} from the feeds.

    Pure given its inputs (no network). `crowded` is the optional list from
    crowding.compute_crowding; `name_to_ticker` maps a normalized issuer name to
    a ticker; `cik_to_ticker` is unused here but kept for symmetry with callers.
    """
    sig = defaultdict(lambda: {"issuer": "", "feeds": set(),
                               "counts": defaultdict(int)})

    def add(ticker, feed, issuer):
        if not ticker:
            return
        rec = sig[ticker]
        rec["feeds"].add(feed)
        rec["counts"][feed] += 1
        if issuer and len(issuer) > len(rec["issuer"]):
            rec["issuer"] = issuer

    for h in congress_history or []:
        ts = parse_iso(h.get("ts"))
        if cutoff and ts and ts < cutoff:
            continue
        ticker = extract_ticker(h.get("issuer", ""))
        add(ticker, "congress", h.get("issuer", ""))

    for h in sec_history or []:
        ts = parse_iso(h.get("ts"))
        if cutoff and ts and ts < cutoff:
            continue
        filer = h.get("filer", "")
        ticker = name_to_ticker.get(normalize_name(filer))
        form = h.get("form", "")
        base = form[:-2] if form.endswith("/A") else form
        if base == "4":
            add(ticker, "insider", filer)
        elif base == "8-K":
            add(ticker, "corporate", filer)

    for c in crowded or []:
        ticker = name_to_ticker.get(normalize_name(c.get("issuer", "")))
        add(ticker, "institutional", c.get("issuer", ""))

    return sig


def score_confluence(signals, min_feeds=MIN_FEEDS):
    """Rank tickers active across >= min_feeds distinct feeds.

    Returns [{ticker, issuer, feed_count, total, feeds:[...],
    counts:{feed:int}}] sorted by feed_count then total signal count."""
    out = []
    for ticker, rec in signals.items():
        feeds = rec["feeds"]
        if len(feeds) < min_feeds:
            continue
        counts = dict(rec["counts"])
        out.append({
            "ticker": ticker,
            "issuer": rec["issuer"],
            "feed_count": len(feeds),
            "total": sum(counts.values()),
            "feeds": sorted(feeds),
            "counts": counts,
        })
    out.sort(key=lambda c: (-c["feed_count"], -c["total"]))
    return out


# -------------------- 13F crowding (optional, networked) --------------------

def load_crowded_names(watchlist):
    """Best-effort: reuse crowding.py to get crowded 13F names. Returns [] on any
    failure so confluence degrades to the congress/insider/8-K feeds."""
    try:
        import crowding
    except Exception as e:
        print(f"[WARN] crowding import failed: {e}", file=sys.stderr)
        return []
    funds = []
    for e in watchlist.get("sec_ciks", []):
        if "13F-HR" not in set(e.get("forms", [])):
            continue
        cik = str(e["cik"]).zfill(10)
        fund = crowding.load_fund_holdings(cik, e.get("name", cik))
        if fund:
            funds.append(fund)
        time.sleep(0.15)
    if len(funds) < 2:
        return []
    return crowding.compute_crowding(funds)


# -------------------- Discord --------------------

def post_discord(embed):
    if not DISCORD_WEBHOOK:
        return False
    body = json.dumps({"embeds": [embed]}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK,
        data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "AutopilotWatcher-Confluence/1.0"},
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


def _feed_summary(c):
    parts = []
    for feed in c["feeds"]:
        n = c["counts"].get(feed, 0)
        label = {"congress": "congress", "insider": "insider buys",
                 "corporate": "8-K", "institutional": "13F"}[feed]
        suffix = f"×{n}" if feed != "institutional" and n > 1 else ""
        parts.append(f"{FEED_EMOJI[feed]} {label}{suffix}")
    return " · ".join(parts)


def build_embed(ranked):
    top = ranked[:TOP_N]
    triple = [c for c in top if c["feed_count"] >= 3]
    lines = []
    for c in top:
        star = " ⭐" if c["feed_count"] >= 3 else ""
        issuer = f" _{c['issuer'][:32]}_" if c["issuer"] else ""
        lines.append(f"**{c['ticker']}**{star} — {c['feed_count']} feeds:"
                     f" {_feed_summary(c)}{issuer}")
    fields = [{"name": f"🎯 Confluence — top {len(top)}",
               "value": "\n".join(lines)[:1024], "inline": False}]
    if triple:
        fields.append({
            "name": "⭐ All-feed overlap (≥3)",
            "value": ", ".join(c["ticker"] for c in triple)[:1024],
            "inline": False,
        })
    return {
        "title": "🎯 Cross-feed confluence",
        "description": (f"Tickers active across ≥{MIN_FEEDS} independent feeds in the "
                        f"last {LOOKBACK_DAYS} days (13F = current crowded holdings). "
                        "More feeds = more corroboration."),
        "color": COLOR_CONFLUENCE,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Autopilot Trading — cross-feed confluence"},
    }


def main():
    watchlist = load_json(WATCHLIST_PATH, {})
    sec_state = load_json(SEC_STATE_PATH, {})
    congress_state = load_json(CONGRESS_STATE_PATH, {})

    cik_to_ticker, name_to_ticker = fetch_company_tickers()
    # Map each watched filer's exact name to its ticker via CIK (authoritative,
    # avoids fuzzy matching for the names we control).
    for e in watchlist.get("sec_ciks", []):
        ticker = cik_to_ticker.get(str(e["cik"]).zfill(10))
        if ticker:
            name_to_ticker[normalize_name(e.get("name", ""))] = ticker

    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    crowded = []
    if INCLUDE_13F:
        crowded = load_crowded_names(watchlist)
    print(f"[START] confluence — lookback {LOOKBACK_DAYS}d, "
          f"13F={'on' if INCLUDE_13F else 'off'} ({len(crowded)} crowded names)")
    print(f"        DRY_RUN={DRY_RUN}  Discord={'configured' if DISCORD_WEBHOOK else 'NOT SET'}")

    signals = collect_signals(
        sec_state.get("alert_history", []),
        congress_state.get("alert_history", []),
        name_to_ticker, cik_to_ticker, crowded=crowded, cutoff=cutoff,
    )
    ranked = score_confluence(signals)
    print(f"[CONFLUENCE] {len(ranked)} tickers across >= {MIN_FEEDS} feeds")
    if not ranked:
        print("[INFO] No confluence this run; skipping Discord post.")
        return 0

    embed = build_embed(ranked)
    if DRY_RUN:
        print(json.dumps(embed, indent=2))
        return 0
    ok = post_discord(embed)
    print(f"[DONE] posted={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
