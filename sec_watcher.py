#!/usr/bin/env python3
"""SEC EDGAR watcher — polls submissions API and posts new filings to Discord.

Uses only the Python standard library. No pip install needed.

Local run:
    SEC_USER_AGENT='Your Name your@email.com' \
    DISCORD_WEBHOOK='https://discord.com/api/webhooks/...' \
    python3 sec_watcher.py

Dry-run (no Discord posts, no state changes from notify side):
    SEC_USER_AGENT='Your Name your@email.com' DRY_RUN=1 python3 sec_watcher.py

GitHub Actions: configure SEC_USER_AGENT and DISCORD_WEBHOOK as repository secrets.
See .github/workflows/sec-watcher.yml.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
WATCHLIST_PATH = ROOT / "watchlist.json"
STATE_PATH = ROOT / "state.json"

USER_AGENT = os.environ.get("SEC_USER_AGENT", "").strip()
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
MAX_ALERTS_PER_RUN = int(os.environ.get("MAX_ALERTS_PER_RUN", "20"))
SEC_RATE_DELAY_SEC = 0.15  # SEC limit is 10 req/sec; stay polite at ~6/sec
DISCORD_RATE_DELAY_SEC = 0.5

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


def post_discord(content):
    if not DISCORD_WEBHOOK:
        return
    body = json.dumps({"content": content[:1900]}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 204):
                print(f"[WARN] Discord status {resp.status}", file=sys.stderr)
    except Exception as e:
        print(f"[ERROR] Discord post failed: {e}", file=sys.stderr)
    time.sleep(DISCORD_RATE_DELAY_SEC)


def alert(message):
    print(f"[ALERT] {message.splitlines()[0]}")
    if DRY_RUN:
        return
    post_discord(message)


def load_state():
    if not STATE_PATH.exists():
        return {"sec_seen": {}, "first_run_done": False}
    try:
        return json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        print(f"[WARN] state.json malformed; starting fresh", file=sys.stderr)
        return {"sec_seen": {}, "first_run_done": False}


def save_state(state):
    # Cap per-CIK history at 2000 (comfortably above EDGAR's ~1000 recent-submissions window).
    # Preserve insertion order — older entries first, newest last — so the trim drops the oldest.
    for cik, accs in list(state.get("sec_seen", {}).items()):
        if len(accs) > 2000:
            state["sec_seen"][cik] = accs[-2000:]
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def matches_form(filter_set, form):
    if not filter_set:
        return True
    if form in filter_set:
        return True
    # Match amendments: "SC 13D/A" matches if "SC 13D" is in filter
    if form.endswith("/A") and form[:-2] in filter_set:
        return True
    return False


def filing_url(cik, accession, primary_doc):
    cik_int = int(cik)
    acc_clean = accession.replace("-", "")
    if primary_doc:
        return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{primary_doc}"
    return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&owner=include&count=40"


def check_entry(entry, state, is_first_run, alerts_left):
    cik = str(entry["cik"]).zfill(10)
    name = entry.get("name", cik)
    forms_filter = set(entry.get("forms", []))

    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        data = http_get_json(url)
    except urllib.error.HTTPError as e:
        print(f"[ERROR] {name} ({cik}): HTTP {e.code} {e.reason}", file=sys.stderr)
        return 0
    except Exception as e:
        print(f"[ERROR] {name} ({cik}): {e}", file=sys.stderr)
        return 0

    recent = data.get("filings", {}).get("recent", {})
    accessions = recent.get("accessionNumber", [])
    forms_arr = recent.get("form", [])
    dates_arr = recent.get("filingDate", [])
    docs_arr = recent.get("primaryDocument", [])

    relevant = []
    for i, acc in enumerate(accessions):
        form = forms_arr[i] if i < len(forms_arr) else ""
        if not matches_form(forms_filter, form):
            continue
        relevant.append({
            "accession": acc,
            "form": form,
            "filing_date": dates_arr[i] if i < len(dates_arr) else "",
            "primary_doc": docs_arr[i] if i < len(docs_arr) else "",
        })

    is_new_cik = cik not in state["sec_seen"]
    seen_list = list(state["sec_seen"].get(cik, []))
    seen_set = set(seen_list)

    if is_first_run or is_new_cik:
        # EDGAR returns recent filings in reverse-chronological order (newest first).
        # Reverse so we append oldest-first, leaving the newest at the end of seen_list.
        # The trim in save_state keeps newest entries.
        for f in reversed(relevant):
            if f["accession"] not in seen_set:
                seen_list.append(f["accession"])
                seen_set.add(f["accession"])
        state["sec_seen"][cik] = seen_list
        label = "INIT" if is_first_run else "NEW-CIK"
        print(f"[{label}] {name}: seeded {len(relevant)} relevant filings (no alerts)")
        return 0

    new = [f for f in relevant if f["accession"] not in seen_set]
    if not new:
        return 0

    sent = 0
    # `relevant` is newest-first; reverse so we send/store oldest-first.
    for f in reversed(new):
        if alerts_left <= 0:
            break
        url = filing_url(cik, f["accession"], f["primary_doc"])
        msg = (
            f"📄 **{name}** — **{f['form']}** filed {f['filing_date']}\n"
            f"<{url}>"
        )
        alert(msg)
        seen_list.append(f["accession"])
        seen_set.add(f["accession"])
        sent += 1
        alerts_left -= 1

    state["sec_seen"][cik] = seen_list
    return sent


def main():
    if not WATCHLIST_PATH.exists():
        print(f"ERROR: watchlist.json not found at {WATCHLIST_PATH}", file=sys.stderr)
        return 1

    watchlist = json.loads(WATCHLIST_PATH.read_text())
    entries = watchlist.get("sec_ciks", [])
    if not entries:
        print("[WARN] No sec_ciks in watchlist.json; nothing to check.")
        return 0

    state = load_state()
    is_first_run = not state.get("first_run_done", False)

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[START] {started}")
    print(f"        DRY_RUN={DRY_RUN}  Discord={'configured' if DISCORD_WEBHOOK else 'NOT SET'}")
    print(f"        first_run={is_first_run}  entries={len(entries)}  alert_budget={MAX_ALERTS_PER_RUN}")

    alerts_left = MAX_ALERTS_PER_RUN
    total_sent = 0

    for entry in entries:
        if alerts_left <= 0 and not is_first_run:
            print("[INFO] Alert budget exhausted; remaining entries deferred to next run.")
            break
        sent = check_entry(entry, state, is_first_run, alerts_left)
        total_sent += sent
        alerts_left -= sent
        time.sleep(SEC_RATE_DELAY_SEC)

    if is_first_run:
        state["first_run_done"] = True
        print(f"[INIT-DONE] State seeded; subsequent runs alert on truly new filings only.")

    save_state(state)
    print(f"[DONE] alerts_sent={total_sent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
