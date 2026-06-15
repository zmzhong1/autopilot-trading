#!/usr/bin/env python3
"""Ticker discovery — surface tickers worth adding to your stocknews list.

Two signals (selected by config):

  1. **Capitol Trades firehose** — the latest ~96 trades across ALL politicians.
     Tickers traded by multiple distinct politicians in the window are likely
     candidates worth tracking.

  2. **EDGAR 8-K firehose** — the SEC's atom feed of every 8-K filed market-wide.
     CIKs that filed multiple 8-Ks recently may be undergoing material events.
     CIKs are mapped to tickers via SEC's company_tickers.json.

Tickers already in `watchlist.stocknews_tickers` are excluded — only NEW
candidates are surfaced.

Local run:
    SEC_USER_AGENT='Your Name your@email.com' \
    DISCORD_WEBHOOK='https://discord.com/api/webhooks/...' \
    python3 discovery.py

Dry-run:
    SEC_USER_AGENT='Your Name your@email.com' DRY_RUN=1 python3 discovery.py
"""

import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Windows consoles default to a non-UTF-8 codec (e.g. GBK), so printing the
# emoji in alert output raises UnicodeEncodeError. Force UTF-8 — a no-op on the
# UTF-8 CI runner and when stdout isn't reconfigurable (e.g. captured).
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

ROOT = Path(__file__).parent
WATCHLIST_PATH = ROOT / "watchlist.json"

USER_AGENT = os.environ.get("SEC_USER_AGENT", "").strip()
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
TOP_N = int(os.environ.get("DISCOVERY_TOP_N", "10"))
EIGHTK_FEED_COUNT = int(os.environ.get("DISCOVERY_8K_FEED_COUNT", "100"))

COLOR_DISCOVERY = 0x9B59B6  # purple
DISCORD_RATE_DELAY_SEC = 0.5

if not USER_AGENT:
    sys.exit(
        "ERROR: SEC_USER_AGENT not set. SEC requires a contact string.\n"
        "  Example: SEC_USER_AGENT='Your Name your@email.com'"
    )

SEC_HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*"}


# -------------------- Source 1: Congress trades (kadoa / FMP) --------------------

# Reuse the watcher's resilient kadoa-primary + FMP-fallback fetch + parsing.
import congress_watcher  # noqa: E402


def discover_from_congress(stocknews_set):
    """Return list of dicts: {ticker, issuer, politician_count, trade_count, sample_politicians}."""
    try:
        trades, source = congress_watcher.fetch_congress_trades()
    except Exception as e:
        print(f"[ERROR] Congress trade fetch failed: {e}", file=sys.stderr)
        return []
    if not trades:
        print("[WARN] No congress trades returned.", file=sys.stderr)
        return []
    print(f"[INFO] congress discovery via {source}: {len(trades)} trades", file=sys.stderr)

    by_ticker = defaultdict(lambda: {
        "issuer": None,
        "politicians": set(),
        "trade_count": 0,
        "buy_count": 0,
        "sell_count": 0,
    })
    for t in trades:
        ticker = t.get("ticker")
        if not ticker or ticker.upper() in stocknews_set:
            continue
        rec = by_ticker[ticker]
        rec["issuer"] = rec["issuer"] or t.get("issuer", "")
        rec["politicians"].add(t.get("politician", "?"))
        rec["trade_count"] += 1
        ttype = (t.get("trade_type") or "").lower()
        if "buy" in ttype:
            rec["buy_count"] += 1
        elif "sell" in ttype:
            rec["sell_count"] += 1

    candidates = []
    for ticker, rec in by_ticker.items():
        candidates.append({
            "ticker": ticker,
            "issuer": (rec["issuer"] or "").strip(),
            "politician_count": len(rec["politicians"]),
            "trade_count": rec["trade_count"],
            "buy_count": rec["buy_count"],
            "sell_count": rec["sell_count"],
            "sample_politicians": sorted(rec["politicians"])[:3],
        })
    # Rank: distinct politicians first, then trade volume.
    candidates.sort(key=lambda c: (-c["politician_count"], -c["trade_count"]))
    return candidates


# -------------------- Source 2: EDGAR 8-K firehose --------------------

def fetch_8k_atom():
    url = (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
           f"&type=8-K&output=atom&count={EIGHTK_FEED_COUNT}")
    req = urllib.request.Request(url, headers=SEC_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


_TITLE_CIK_RE = re.compile(r"\((\d{10})\)")


def parse_8k_atom(xml_text):
    """Yield {cik, filer, title, link} for each entry in the 8-K atom feed."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"[WARN] 8-K atom parse failed: {e}", file=sys.stderr)
        return []
    # Strip namespace
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]

    out = []
    for entry in root.findall("entry"):
        title_el = entry.find("title")
        title = title_el.text if title_el is not None and title_el.text else ""
        link_el = entry.find("link")
        link = link_el.attrib.get("href") if link_el is not None else None
        m = _TITLE_CIK_RE.search(title)
        if not m:
            continue
        cik = m.group(1)
        # Title format: "8-K - Filer Name (0000123456) (Filer)"
        filer = title.split(" - ", 1)[1].split(" (")[0] if " - " in title else title
        out.append({"cik": cik, "filer": filer, "title": title, "link": link})
    return out


_TICKERS_CACHE = {"data": None}


def fetch_company_tickers():
    """SEC's CIK→ticker map. Cached for the run."""
    if _TICKERS_CACHE["data"] is not None:
        return _TICKERS_CACHE["data"]
    url = "https://www.sec.gov/files/company_tickers.json"
    req = urllib.request.Request(url, headers=SEC_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[WARN] company_tickers.json fetch failed: {e}", file=sys.stderr)
        _TICKERS_CACHE["data"] = {}
        return {}
    by_cik = {}
    for v in raw.values():
        cik_padded = str(v["cik_str"]).zfill(10)
        by_cik[cik_padded] = {"ticker": v["ticker"], "name": v.get("title", "")}
    _TICKERS_CACHE["data"] = by_cik
    return by_cik


def discover_from_8k(stocknews_set):
    """Return ranked candidates from the 8-K atom feed, filtered to tickers
    not in stocknews_set."""
    try:
        atom = fetch_8k_atom()
    except Exception as e:
        print(f"[ERROR] 8-K atom fetch failed: {e}", file=sys.stderr)
        return []
    entries = parse_8k_atom(atom)
    if not entries:
        return []
    cik_to_ticker = fetch_company_tickers()

    counts = Counter()
    sample = {}
    for e in entries:
        info = cik_to_ticker.get(e["cik"])
        if not info:
            continue
        ticker = (info["ticker"] or "").upper()
        if not ticker or ticker in stocknews_set:
            continue
        counts[ticker] += 1
        if ticker not in sample:
            sample[ticker] = {"name": info["name"], "filer": e["filer"], "link": e["link"]}

    candidates = []
    for ticker, count in counts.most_common():
        s = sample.get(ticker, {})
        candidates.append({
            "ticker": ticker,
            "issuer": s.get("name") or s.get("filer") or "",
            "filing_count": count,
            "sample_link": s.get("link"),
        })
    return candidates


# -------------------- Discord --------------------

def post_discord(embed):
    if not DISCORD_WEBHOOK:
        return False
    body = json.dumps({"embeds": [embed]}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "AutopilotWatcher-Discovery/1.0",
        },
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


def build_embed(congress, eightk):
    fields = []
    if congress:
        lines = []
        for c in congress[:TOP_N]:
            sample = ", ".join(c["sample_politicians"]) if c["sample_politicians"] else "?"
            lines.append(
                f"**{c['ticker']}** — {c['politician_count']} politicians · "
                f"{c['trade_count']} trades ({c['buy_count']}🟢 / {c['sell_count']}🔴)\n"
                f"   _{c['issuer'][:60]}_ · e.g. {sample[:80]}"
            )
        value = "\n".join(lines) or "_(none)_"
        fields.append({
            "name": f"🏛️ Congress firehose — top {min(TOP_N, len(congress))}",
            "value": value[:1024],
            "inline": False,
        })
    else:
        fields.append({
            "name": "🏛️ Congress firehose",
            "value": "_(no candidates this week)_",
            "inline": False,
        })

    if eightk:
        lines = []
        for c in eightk[:TOP_N]:
            lines.append(
                f"**{c['ticker']}** — {c['filing_count']} 8-K filing(s) · _{c['issuer'][:60]}_"
            )
        value = "\n".join(lines) or "_(none)_"
        fields.append({
            "name": f"📋 8-K firehose — top {min(TOP_N, len(eightk))}",
            "value": value[:1024],
            "inline": False,
        })
    else:
        fields.append({
            "name": "📋 8-K firehose",
            "value": "_(no candidates this week)_",
            "inline": False,
        })

    fields.append({
        "name": "Next step",
        "value": ("Add tickers you want to track to **`watchlist.json → stocknews_tickers`** "
                  "to suppress them from future digests."),
        "inline": False,
    })

    return {
        "title": "🔎 Ticker discovery — candidates for stocknews",
        "description": ("Ranked tickers _not_ already on your stocknews list. "
                        "Congress: more distinct politicians = stronger signal. "
                        "8-K: more filings = more activity (noisy — read titles)."),
        "color": COLOR_DISCOVERY,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Autopilot Trading — discovery digest"},
    }


def main():
    if not WATCHLIST_PATH.exists():
        print(f"ERROR: watchlist.json not found at {WATCHLIST_PATH}", file=sys.stderr)
        return 1
    watchlist = json.loads(WATCHLIST_PATH.read_text())
    stocknews_list = watchlist.get("stocknews_tickers", []) or []
    stocknews_set = {t.upper() for t in stocknews_list}

    print(f"[START] discovery — excluding {len(stocknews_set)} stocknews tickers")
    print(f"        DRY_RUN={DRY_RUN}  Discord={'configured' if DISCORD_WEBHOOK else 'NOT SET'}")

    congress_candidates = discover_from_congress(stocknews_set)
    print(f"[CONGRESS] {len(congress_candidates)} candidates")

    eightk_candidates = discover_from_8k(stocknews_set)
    print(f"[8-K] {len(eightk_candidates)} candidates")

    if not congress_candidates and not eightk_candidates:
        print("[INFO] No discovery candidates this run; skipping Discord post.")
        return 0

    embed = build_embed(congress_candidates, eightk_candidates)
    if DRY_RUN:
        print(json.dumps(embed, indent=2))
        return 0
    ok = post_discord(embed)
    print(f"[DONE] posted={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    import producer_status
    rc = main()
    producer_status.record("discovery", ok=(rc == 0))
    sys.exit(rc)
