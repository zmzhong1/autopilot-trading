#!/usr/bin/env python3
"""Insider cluster-buying detector — flags when multiple distinct insiders make
open-market PURCHASES (Form 4 transaction code "P") of the same company within a
rolling window.

A lone insider sale is routine (10b5-1 plans, tax withholding, RSU vesting). But
multiple insiders *buying* their own stock with their own cash in a short window
is one of the few insider signals with real predictive history — they don't
coordinate purchases unless they think the stock is cheap.

Scans the watchlist companies that track Form 4 (`sec_ciks` with "4" in `forms`),
reusing the same Form 4 parser the SEC watcher relies on
(sec_enrich.enrich_form4). De-dups via cluster_state.json so a given cluster
alerts once — and again only when a genuinely new purchase joins it.

Stdlib-only. Local run:
    SEC_USER_AGENT='Your Name your@email.com' \
    DISCORD_WEBHOOK='https://discord.com/api/webhooks/...' \
    python3 cluster_buys.py

Dry-run (no Discord, no state write):
    SEC_USER_AGENT='Your Name your@email.com' DRY_RUN=1 python3 cluster_buys.py
"""

import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sec_enrich

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
STATE_PATH = ROOT / "cluster_state.json"

USER_AGENT = os.environ.get("SEC_USER_AGENT", "").strip()
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
LOOKBACK_DAYS = int(os.environ.get("CLUSTER_LOOKBACK_DAYS", "14"))
MIN_INSIDERS = int(os.environ.get("CLUSTER_MIN_INSIDERS", "2"))
MAX_FORM4_PER_CIK = int(os.environ.get("CLUSTER_MAX_FORM4_PER_CIK", "60"))
SEC_RATE_DELAY_SEC = 0.15
DISCORD_RATE_DELAY_SEC = 0.5
ALERTED_CAP = 2000

COLOR_BUY = 0x2ECC71

if not USER_AGENT:
    sys.exit(
        "ERROR: SEC_USER_AGENT not set. SEC requires a contact string.\n"
        "  Example: SEC_USER_AGENT='Your Name your@email.com'"
    )

SEC_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}


def http_get_json(url):
    req = urllib.request.Request(url, headers=SEC_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_text(url):
    req = urllib.request.Request(url, headers=SEC_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


# -------------------- State --------------------

def load_state():
    if not STATE_PATH.exists():
        return {"alerted_accessions": []}
    try:
        state = json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        print("[WARN] cluster_state.json malformed; starting fresh", file=sys.stderr)
        return {"alerted_accessions": []}
    state.setdefault("alerted_accessions", [])
    return state


def save_state(state):
    if len(state.get("alerted_accessions", [])) > ALERTED_CAP:
        state["alerted_accessions"] = state["alerted_accessions"][-ALERTED_CAP:]
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


# -------------------- Purchase scan --------------------

def scan_company_purchases(cik, name, cutoff_date):
    """Return list of open-market purchase records for a company's recent Form 4s:
    {insider, accession, filing_date, shares, value}. Only code "P" transactions
    count (excludes grants, option exercises, tax withholding, routine sales)."""
    try:
        data = http_get_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    except Exception as e:
        print(f"[WARN] submissions fetch failed for {name}: {e}", file=sys.stderr)
        return []
    recent = data.get("filings", {}).get("recent", {})
    accs = recent.get("accessionNumber", [])
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    docs = recent.get("primaryDocument", [])

    purchases = []
    scanned = 0
    for i, acc in enumerate(accs):
        form = forms[i] if i < len(forms) else ""
        base = form[:-2] if form.endswith("/A") else form
        if base != "4":
            continue
        fdate = dates[i] if i < len(dates) else ""
        if fdate and fdate < cutoff_date:
            break  # newest-first; once we pass the window, the rest are older
        scanned += 1
        if scanned > MAX_FORM4_PER_CIK:
            break
        primary_doc = docs[i] if i < len(docs) else ""
        try:
            enriched = sec_enrich.enrich_form4(cik, acc, primary_doc,
                                               http_get_text, http_get_json)
        except Exception as e:
            print(f"[WARN] enrich failed {name} {acc}: {e}", file=sys.stderr)
            continue
        if not enriched:
            continue
        buy_value = sum((t.get("value") or 0.0) for t in enriched["transactions"]
                        if t.get("code") == "P")
        buy_shares = sum((t.get("shares") or 0.0) for t in enriched["transactions"]
                         if t.get("code") == "P")
        if buy_shares <= 0:
            continue  # no open-market purchase in this filing
        purchases.append({
            "insider": enriched.get("insider") or "Unknown insider",
            "role": enriched.get("role") or "—",
            "accession": acc,
            "filing_date": fdate,
            "shares": buy_shares,
            "value": buy_value,
        })
        time.sleep(SEC_RATE_DELAY_SEC)
    return purchases


# -------------------- Cluster detection (pure, testable) --------------------

def detect_clusters(purchases_by_company, min_insiders=MIN_INSIDERS):
    """Given {company: [purchase records]}, return clusters where >= min_insiders
    *distinct* insiders made open-market purchases. Sorted by insider count then
    total $-value. Each cluster:
        {company, insider_count, total_value, total_shares, accessions,
         insiders:[{insider, role, value, shares, accession, filing_date}]}
    """
    clusters = []
    for company, purchases in purchases_by_company.items():
        by_insider = defaultdict(lambda: {"value": 0.0, "shares": 0.0,
                                          "accessions": [], "role": "—",
                                          "filing_date": ""})
        for p in purchases:
            rec = by_insider[p["insider"]]
            rec["value"] += p.get("value") or 0.0
            rec["shares"] += p.get("shares") or 0.0
            rec["accessions"].append(p["accession"])
            if p.get("role") and p["role"] != "—":
                rec["role"] = p["role"]
            rec["filing_date"] = max(rec["filing_date"], p.get("filing_date") or "")
        if len(by_insider) < min_insiders:
            continue
        insiders = [{"insider": name, "role": r["role"], "value": r["value"],
                     "shares": r["shares"], "filing_date": r["filing_date"],
                     "accessions": r["accessions"]}
                    for name, r in by_insider.items()]
        insiders.sort(key=lambda x: -x["value"])
        accessions = [a for ins in insiders for a in ins["accessions"]]
        clusters.append({
            "company": company,
            "insider_count": len(by_insider),
            "total_value": sum(i["value"] for i in insiders),
            "total_shares": sum(i["shares"] for i in insiders),
            "accessions": accessions,
            "insiders": insiders,
        })
    clusters.sort(key=lambda c: (-c["insider_count"], -c["total_value"]))
    return clusters


# -------------------- Discord --------------------

def post_discord(embed):
    if not DISCORD_WEBHOOK:
        return False
    body = json.dumps({"embeds": [embed]}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK,
        data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "AutopilotWatcher-Cluster/1.0"},
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


def build_embed(cluster):
    lines = []
    for ins in cluster["insiders"][:10]:
        lines.append(f"🟢 **{ins['insider']}** ({ins['role']}) — "
                     f"{sec_enrich.fmt_shares(ins['shares'])} sh · "
                     f"{sec_enrich.fmt_money(ins['value'])}")
    if len(cluster["insiders"]) > 10:
        lines.append(f"…and {len(cluster['insiders']) - 10} more insiders")

    return {
        "title": f"🟢 Insider cluster buy — {cluster['company']}",
        "description": (f"**{cluster['insider_count']} insiders** bought on the open "
                        f"market in the last {LOOKBACK_DAYS} days\n\n" + "\n".join(lines)),
        "color": COLOR_BUY,
        "fields": [
            {"name": "Insiders buying", "value": str(cluster["insider_count"]), "inline": True},
            {"name": "Total bought", "value": sec_enrich.fmt_money(cluster["total_value"]),
             "inline": True},
            {"name": "Shares", "value": sec_enrich.fmt_shares(cluster["total_shares"]),
             "inline": True},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"Autopilot Trading — insider cluster buying (code P) · "
                           f"{cluster['insider_count']} insiders"},
    }


def main():
    if not WATCHLIST_PATH.exists():
        print(f"ERROR: watchlist.json not found at {WATCHLIST_PATH}", file=sys.stderr)
        return 1
    watchlist = json.loads(WATCHLIST_PATH.read_text())
    entries = [e for e in watchlist.get("sec_ciks", [])
               if "4" in set(e.get("forms", []))]
    if not entries:
        print("[WARN] No Form 4 filers in watchlist; nothing to do.")
        return 0

    state = load_state()
    alerted = set(state.get("alerted_accessions", []))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).date().isoformat()

    print(f"[START] cluster-buys — {len(entries)} Form 4 filers, lookback {LOOKBACK_DAYS}d")
    print(f"        DRY_RUN={DRY_RUN}  Discord={'configured' if DISCORD_WEBHOOK else 'NOT SET'}")

    purchases_by_company = {}
    for e in entries:
        cik = str(e["cik"]).zfill(10)
        name = e.get("name", cik)
        purchases = scan_company_purchases(cik, name, cutoff)
        if purchases:
            purchases_by_company[name] = purchases
            print(f"[OK] {name}: {len(purchases)} open-market purchase filing(s)")
        time.sleep(SEC_RATE_DELAY_SEC)

    clusters = detect_clusters(purchases_by_company)
    print(f"[CLUSTERS] {len(clusters)} companies with >= {MIN_INSIDERS} insiders buying")

    sent = 0
    for cluster in clusters:
        # Only alert if the cluster contains a purchase we haven't alerted on yet
        # (first time it crosses the threshold, or when a new insider joins).
        if all(a in alerted for a in cluster["accessions"]):
            continue
        embed = build_embed(cluster)
        print(f"[ALERT] cluster buy — {cluster['company']} "
              f"({cluster['insider_count']} insiders)")
        delivered = True if DRY_RUN else post_discord(embed)
        if DRY_RUN:
            print(json.dumps(embed, indent=2))
        if delivered:
            sent += 1
            alerted.update(cluster["accessions"])

    state["alerted_accessions"] = sorted(alerted)
    state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not DRY_RUN:
        save_state(state)
    print(f"[DONE] clusters_alerted={sent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
