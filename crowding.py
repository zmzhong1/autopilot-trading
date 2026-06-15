#!/usr/bin/env python3
"""13F crowding digest — surfaces names that multiple tracked funds hold.

When several of the institutional managers you follow (Buffett, Burry, Ackman,
Citadel, …) hold the *same* security, that overlap is a "smart-money consensus"
signal. When several of them *newly* bought it in the same quarter, that's the
strongest version. This script reuses the 13F holdings parser the SEC watcher
already relies on (sec_enrich.fetch_13f_holdings) and cross-references each
tracked fund's latest 13F-HR.

13F-HRs are quarterly (filed ~45 days after quarter end), so this runs weekly
alongside the heartbeat rather than per-filing — it just re-reads each fund's
most recent filing and recomputes the overlap.

Funds are the watchlist `sec_ciks` entries whose `forms` include "13F-HR".

Stdlib-only. Local run:
    SEC_USER_AGENT='Your Name your@email.com' \
    DISCORD_WEBHOOK='https://discord.com/api/webhooks/...' \
    python3 crowding.py

Dry-run (prints embed, no Discord):
    SEC_USER_AGENT='Your Name your@email.com' DRY_RUN=1 python3 crowding.py
"""

import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
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

USER_AGENT = os.environ.get("SEC_USER_AGENT", "").strip()
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
MIN_FUNDS = int(os.environ.get("CROWDING_MIN_FUNDS", "2"))
TOP_N = int(os.environ.get("CROWDING_TOP_N", "12"))
SEC_RATE_DELAY_SEC = 0.15
DISCORD_RATE_DELAY_SEC = 0.5
# Wall-clock budget for the 13F fetch loop, so a slow SEC day can't run the
# weekly heartbeat job into its timeout and silently kill the steps that follow.
LOAD_BUDGET_SEC = int(os.environ.get("CROWDING_LOAD_BUDGET_SEC", "120"))
# Cache the computed crowded list so confluence.py (a separate process in the
# same heartbeat job, run right after this one) can reuse it instead of
# re-fetching every tracked fund's 13F.
CROWDED_CACHE_PATH = ROOT / ".crowded_cache.json"
CROWDED_CACHE_MAX_AGE_SEC = int(os.environ.get("CROWDED_CACHE_MAX_AGE_SEC", "10800"))

COLOR_CROWDING = 0x3498DB  # blue, matching the 13F embed family

if not USER_AGENT:
    sys.exit(
        "ERROR: SEC_USER_AGENT not set. SEC requires a contact string.\n"
        "  Example: SEC_USER_AGENT='Your Name your@email.com'"
    )

SEC_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}


# -------------------- SEC fetch helpers --------------------

def http_get_json(url):
    req = urllib.request.Request(url, headers=SEC_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_text(url):
    req = urllib.request.Request(url, headers=SEC_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def latest_13fhr(cik):
    """Return (accession, filing_date) of the fund's most recent 13F-HR, or
    (None, None). EDGAR lists recent filings newest-first."""
    try:
        data = http_get_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    except Exception as e:
        print(f"[WARN] submissions fetch failed for {cik}: {e}", file=sys.stderr)
        return None, None
    recent = data.get("filings", {}).get("recent", {})
    accs = recent.get("accessionNumber", [])
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    for i, acc in enumerate(accs):
        if (forms[i] if i < len(forms) else "") == "13F-HR":
            return acc, (dates[i] if i < len(dates) else "")
    return None, None


def load_fund_holdings(cik, name):
    """Fetch a fund's latest 13F-HR holdings + the set of CUSIPs new vs. the
    prior 13F-HR. Returns a dict or None if no parseable filing."""
    acc, filing_date = latest_13fhr(cik)
    if not acc:
        print(f"[SKIP] {name}: no 13F-HR found", file=sys.stderr)
        return None
    current = sec_enrich.fetch_13f_holdings(cik, acc, http_get_text, http_get_json)
    if not current:
        print(f"[SKIP] {name}: 13F-HR {acc} had no parseable holdings", file=sys.stderr)
        return None

    new_cusips = set()
    prior_acc = sec_enrich.find_prior_13fhr_accession(cik, acc, http_get_json)
    if prior_acc:
        prior = sec_enrich.fetch_13f_holdings(cik, prior_acc, http_get_text, http_get_json)
        if prior:
            prior_cusips = {h["cusip"] for h in prior}
            new_cusips = {h["cusip"] for h in current} - prior_cusips
    return {
        "name": name,
        "filing_date": filing_date,
        "holdings": current,
        "new_cusips": new_cusips,
        "has_prior": bool(prior_acc),
    }


# -------------------- Crowding aggregation (pure, testable) --------------------

def compute_crowding(funds, min_funds=MIN_FUNDS):
    """Cross-reference holdings across funds by CUSIP.

    `funds` is a list of dicts: {name, holdings:[{cusip,issuer,value_usd,...}],
    new_cusips:set}. Returns a list of crowded names sorted by fund count then
    aggregate value:
        {cusip, issuer, fund_count, total_value, new_count,
         funds:[{name, value_usd, is_new}]}
    Only names held by >= min_funds distinct funds are returned.
    """
    by_cusip = defaultdict(lambda: {"issuer": "", "funds": []})
    for fund in funds:
        new_cusips = fund.get("new_cusips") or set()
        for h in fund.get("holdings", []):
            cusip = h.get("cusip")
            if not cusip:
                continue
            rec = by_cusip[cusip]
            issuer = (h.get("issuer") or "").strip()
            # Keep the longest issuer label seen (most descriptive variant).
            if len(issuer) > len(rec["issuer"]):
                rec["issuer"] = issuer
            rec["funds"].append({
                "name": fund["name"],
                "value_usd": h.get("value_usd") or 0.0,
                "is_new": cusip in new_cusips,
            })

    crowded = []
    for cusip, rec in by_cusip.items():
        # One fund can hold a CUSIP across multiple rows (share lots); collapse
        # to distinct funds.
        by_fund = {}
        for f in rec["funds"]:
            agg = by_fund.setdefault(f["name"], {"name": f["name"], "value_usd": 0.0,
                                                 "is_new": False})
            agg["value_usd"] += f["value_usd"]
            agg["is_new"] = agg["is_new"] or f["is_new"]
        fund_list = sorted(by_fund.values(), key=lambda f: -f["value_usd"])
        if len(fund_list) < min_funds:
            continue
        crowded.append({
            "cusip": cusip,
            "issuer": rec["issuer"] or cusip,
            "fund_count": len(fund_list),
            "total_value": sum(f["value_usd"] for f in fund_list),
            "new_count": sum(1 for f in fund_list if f["is_new"]),
            "funds": fund_list,
        })
    crowded.sort(key=lambda c: (-c["fund_count"], -c["total_value"]))
    return crowded


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
            "User-Agent": "AutopilotWatcher-Crowding/1.0",
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


def _fund_line(c):
    parts = []
    for f in c["funds"]:
        tag = " 🆕" if f["is_new"] else ""
        parts.append(f"{f['name'].split(' (')[0]} {sec_enrich.fmt_money(f['value_usd'])}{tag}")
    return ", ".join(parts)


def build_embed(crowded, fund_count, missing):
    most_crowded = crowded[:TOP_N]
    consensus = [c for c in crowded if c["new_count"] >= 2][:TOP_N]

    fields = []
    if most_crowded:
        lines = []
        for c in most_crowded:
            lines.append(
                f"**{c['issuer'][:48]}** — {c['fund_count']} funds · "
                f"{sec_enrich.fmt_money(c['total_value'])}\n   _{_fund_line(c)}_"
            )
        fields.append({
            "name": f"🤝 Most crowded — top {len(most_crowded)}",
            "value": "\n".join(lines)[:1024],
            "inline": False,
        })
    else:
        fields.append({
            "name": "🤝 Most crowded",
            "value": f"_No names held by ≥{MIN_FUNDS} tracked funds._",
            "inline": False,
        })

    if consensus:
        lines = []
        for c in consensus:
            lines.append(
                f"**{c['issuer'][:48]}** — {c['new_count']} funds newly bought "
                f"({c['fund_count']} hold)"
            )
        fields.append({
            "name": "🆕 New consensus this quarter (≥2 funds newly bought)",
            "value": "\n".join(lines)[:1024],
            "inline": False,
        })

    if missing:
        fields.append({
            "name": "⚠️ No 13F parsed",
            "value": ", ".join(m.split(" (")[0] for m in missing)[:1024],
            "inline": False,
        })

    return {
        "title": "🏦 13F crowding — smart-money consensus",
        "description": (f"Overlap across {fund_count} tracked 13F filers' latest "
                        f"quarterly holdings. More funds = stronger consensus; "
                        f"🆕 = newly added this quarter."),
        "color": COLOR_CROWDING,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Autopilot Trading — 13F crowding digest"},
    }


def write_crowded_cache(crowded):
    """Persist the computed crowded list so confluence.py — a separate process
    in the same heartbeat job — can reuse it instead of re-fetching every 13F."""
    try:
        CROWDED_CACHE_PATH.write_text(json.dumps(
            {"ts": datetime.now(timezone.utc).isoformat(), "crowded": crowded}),
            encoding="utf-8")
    except OSError as e:
        print(f"[WARN] could not write crowded cache: {e}", file=sys.stderr)


def read_crowded_cache(max_age_sec=CROWDED_CACHE_MAX_AGE_SEC):
    """Return the cached crowded list if present and fresh, else None."""
    try:
        data = json.loads(CROWDED_CACHE_PATH.read_text(encoding="utf-8"))
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(data["ts"])).total_seconds()
        if 0 <= age <= max_age_sec:
            return data.get("crowded", [])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        pass
    return None


def main():
    if not WATCHLIST_PATH.exists():
        print(f"ERROR: watchlist.json not found at {WATCHLIST_PATH}", file=sys.stderr)
        return 1
    watchlist = json.loads(WATCHLIST_PATH.read_text())
    entries = [e for e in watchlist.get("sec_ciks", [])
               if "13F-HR" in set(e.get("forms", []))]
    if not entries:
        print("[WARN] No 13F-HR filers in watchlist; nothing to do.")
        return 0

    print(f"[START] 13F crowding — {len(entries)} tracked filers")
    print(f"        DRY_RUN={DRY_RUN}  Discord={'configured' if DISCORD_WEBHOOK else 'NOT SET'}")

    funds, missing = [], []
    deadline = time.monotonic() + LOAD_BUDGET_SEC
    for e in entries:
        if time.monotonic() > deadline:
            print(f"[WARN] fetch budget ({LOAD_BUDGET_SEC}s) hit; computing "
                  f"crowding on the {len(funds)} funds fetched so far", file=sys.stderr)
            break
        cik = str(e["cik"]).zfill(10)
        name = e.get("name", cik)
        fund = load_fund_holdings(cik, name)
        if fund:
            funds.append(fund)
            print(f"[OK] {name}: {len(fund['holdings'])} positions "
                  f"({len(fund['new_cusips'])} new) from {fund['filing_date']}")
        else:
            missing.append(name)
        time.sleep(SEC_RATE_DELAY_SEC)

    if len(funds) < MIN_FUNDS:
        print(f"[INFO] Only {len(funds)} funds parsed (need {MIN_FUNDS}); skipping post.")
        return 0

    crowded = compute_crowding(funds)
    write_crowded_cache(crowded)  # let confluence.py reuse this fetch
    print(f"[CROWDING] {len(crowded)} names held by ≥{MIN_FUNDS} funds")
    if not crowded:
        print("[INFO] No crowded names this run; skipping Discord post.")
        return 0

    embed = build_embed(crowded, len(funds), missing)
    if DRY_RUN:
        print(json.dumps(embed, indent=2))
        return 0
    ok = post_discord(embed)
    print(f"[DONE] posted={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    import producer_status
    rc = main()
    producer_status.record("crowding", ok=(rc == 0))
    sys.exit(rc)
