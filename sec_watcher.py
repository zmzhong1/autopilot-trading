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

import sec_enrich

ROOT = Path(__file__).parent
WATCHLIST_PATH = ROOT / "watchlist.json"
STATE_PATH = ROOT / "state.json"

USER_AGENT = os.environ.get("SEC_USER_AGENT", "").strip()
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
MAX_ALERTS_PER_RUN = int(os.environ.get("MAX_ALERTS_PER_RUN", "20"))
# Same-day Form 4 filings from one issuer get batched into a single card once
# this many pile up (set high to disable batching). Lone filings keep their
# richer per-insider card.
FORM4_BATCH_MIN = int(os.environ.get("FORM4_BATCH_MIN", "2"))
ALERT_HISTORY_CAP = 200  # rolling window kept in state.json for the heartbeat
SEC_RATE_DELAY_SEC = 0.15  # SEC limit is 10 req/sec; stay polite at ~6/sec
DISCORD_RATE_DELAY_SEC = 0.5

if not USER_AGENT:
    sys.exit(
        "ERROR: SEC_USER_AGENT not set. SEC requires a contact string.\n"
        "  Example: SEC_USER_AGENT='Your Name your@email.com'"
    )

SEC_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

# Discord embed colours
COLOR_BUY = 0x2ECC71
COLOR_SELL = 0xE74C3C
COLOR_NEUTRAL = 0x95A5A6
COLOR_8K = 0xF1C40F
COLOR_13F = 0x3498DB
COLOR_13DG = 0x9B59B6


def http_get_json(url):
    req = urllib.request.Request(url, headers=SEC_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_text(url):
    req = urllib.request.Request(url, headers=SEC_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def post_discord(content=None, embed=None):
    """Returns True on confirmed delivery, False on any failure."""
    if not DISCORD_WEBHOOK:
        return False
    payload = {}
    if content:
        payload["content"] = content[:1900]
    if embed:
        payload["embeds"] = [embed]
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "AutopilotWatcher/1.0 (+https://github.com/zmzhong1/autopilot-trading)",
        },
        method="POST",
    )
    ok = False
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status in (200, 204):
                ok = True
            else:
                print(f"[WARN] Discord status {resp.status}", file=sys.stderr)
    except Exception as e:
        print(f"[ERROR] Discord post failed: {e}", file=sys.stderr)
    time.sleep(DISCORD_RATE_DELAY_SEC)
    return ok


def alert(headline, embed=None, fallback_content=None):
    """Returns True if the alert is considered delivered (or DRY_RUN)."""
    print(f"[ALERT] {headline}")
    if DRY_RUN:
        return True
    if embed is None:
        return post_discord(content=fallback_content or headline)
    return post_discord(embed=embed)


def load_state():
    if not STATE_PATH.exists():
        return {"sec_seen": {}, "first_run_done": False, "alert_history": []}
    try:
        state = json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        print(f"[WARN] state.json malformed; starting fresh", file=sys.stderr)
        return {"sec_seen": {}, "first_run_done": False, "alert_history": []}
    state.setdefault("alert_history", [])
    return state


def save_state(state):
    # Cap per-CIK history at 2000 (comfortably above EDGAR's ~1000 recent-submissions window).
    # Preserve insertion order — older entries first, newest last — so the trim drops the oldest.
    for cik, accs in list(state.get("sec_seen", {}).items()):
        if len(accs) > 2000:
            state["sec_seen"][cik] = accs[-2000:]
    if len(state.get("alert_history", [])) > ALERT_HISTORY_CAP:
        state["alert_history"] = state["alert_history"][-ALERT_HISTORY_CAP:]
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
    return edgar_index_url(cik, accession)


def edgar_index_url(cik, accession):
    """Human-readable EDGAR filing-index page for an accession.

    Always resolvable from cik+accession alone (no primary_doc needed), so it's
    the reliable deep link to surface even when enrichment / doc lookup fails.
    """
    cik_int = int(cik)
    acc_clean = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{accession}-index.htm"


def build_embed(filer_name, form, filing_date, url, cik, accession, primary_doc, items_str):
    """Returns (embed_dict, headline_str). Falls back gracefully if enrichment fails."""
    base_form = form[:-2] if form.endswith("/A") else form
    timestamp = None
    if filing_date:
        try:
            timestamp = datetime.fromisoformat(filing_date).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass

    if base_form == "4":
        embed, headline = _form4_embed(filer_name, form, filing_date, url, cik,
                                       accession, primary_doc, timestamp)
    elif base_form == "8-K":
        embed, headline = _form8k_embed(filer_name, form, filing_date, url,
                                        items_str, timestamp)
    elif base_form in ("SC 13D", "SC 13G"):
        embed, headline = _sc13_embed(filer_name, form, filing_date, url, cik,
                                      accession, primary_doc, timestamp)
    elif base_form == "13F-HR":
        embed, headline = _form13f_embed(filer_name, form, filing_date, url, cik,
                                         accession, timestamp)
    else:
        # Default — generic embed.
        headline = f"📄 {filer_name} — {form} filed {filing_date}"
        embed = {
            "title": f"{filer_name} — {form}",
            "description": f"Filed {filing_date}",
            "url": url,
            "color": COLOR_NEUTRAL,
        }
        if timestamp:
            embed["timestamp"] = timestamp

    # Stamp every card with its accession number. Multiple same-day filings from
    # one issuer (e.g. several insiders each filing a Form 4) are distinct
    # accessions but otherwise render identically — the footer guarantees each
    # card is visibly self-distinguishing even when enrichment falls back.
    embed.setdefault("footer", {"text": f"📎 {accession}"})
    return embed, headline


def _form4_embed(filer_name, form, filing_date, url, cik, accession, primary_doc, timestamp):
    enriched = None
    try:
        enriched = sec_enrich.enrich_form4(cik, accession, primary_doc,
                                           http_get_text, http_get_json)
    except Exception as e:
        print(f"[WARN] Form 4 enrichment failed: {e}", file=sys.stderr)

    if not enriched:
        # Enrichment failed (no parseable XML/transactions). Keep the card
        # self-distinguishing anyway: the accession + EDGAR link make otherwise
        # identical-looking same-day Form 4s tell-apart-able.
        index_url = edgar_index_url(cik, accession)
        headline = f"📄 {filer_name} — {form} filed {filing_date} ({accession})"
        embed = {
            "title": f"{filer_name} — {form}",
            "description": f"Filed {filing_date}\n[View filing on EDGAR]({index_url})",
            "url": index_url,
            "color": COLOR_NEUTRAL,
            "fields": [
                {"name": "Accession", "value": accession, "inline": True},
                {"name": "Filed", "value": filing_date or "—", "inline": True},
            ],
        }
        if timestamp:
            embed["timestamp"] = timestamp
        return embed, headline

    side = enriched["dominant_side"]
    side_emoji = {"buy": "🟢", "sell": "🔴", "grant": "🎁",
                  "exercise": "🔁", "neutral": "⚪"}.get(side, "⚪")
    color = {"buy": COLOR_BUY, "sell": COLOR_SELL, "grant": COLOR_NEUTRAL,
             "exercise": COLOR_NEUTRAL, "neutral": COLOR_NEUTRAL}.get(side, COLOR_NEUTRAL)

    insider = enriched.get("insider") or "Unknown insider"
    role = enriched.get("role") or "—"
    issuer = enriched.get("issuer_name") or filer_name
    ticker = enriched.get("ticker")
    title_suffix = f" ({ticker})" if ticker else ""

    headline = (f"{side_emoji} {side.upper()} · {insider} ({role}) — "
                f"{issuer}{title_suffix}, {sec_enrich.fmt_money(enriched.get('total_value'))}")

    txs = enriched["transactions"][:5]
    lines = []
    for t in txs:
        shares = sec_enrich.fmt_shares(t.get("shares"))
        price = f"@ ${t['price']:.2f}" if t.get("price") else ""
        value = sec_enrich.fmt_money(t.get("value")) if t.get("value") else ""
        post = (f" → holds {sec_enrich.fmt_shares(t['post_holdings'])}"
                if t.get("post_holdings") is not None else "")
        deriv = " (derivative)" if t.get("derivative") else ""
        lines.append(f"`{t['code']}` {t['label']}{deriv}: {shares} sh {price} {value}{post}".rstrip())
    desc = "\n".join(lines)
    if len(enriched["transactions"]) > 5:
        desc += f"\n…and {len(enriched['transactions']) - 5} more"

    embed = {
        "title": f"{side_emoji} {insider} — {form} on {issuer}{title_suffix}",
        "url": url,
        "color": color,
        "description": desc[:4000],
        "fields": [
            {"name": "Role", "value": role, "inline": True},
            {"name": "Total value", "value": sec_enrich.fmt_money(enriched.get("total_value")),
             "inline": True},
            {"name": "Filed", "value": filing_date or "—", "inline": True},
            {"name": "Filing", "value": f"[{accession}]({edgar_index_url(cik, accession)})",
             "inline": False},
        ],
    }
    if timestamp:
        embed["timestamp"] = timestamp
    return embed, headline


def _form4_batch_embed(filer_name, cik, filings, filing_date):
    """One card summarizing multiple same-day Form 4 filings for an issuer.

    Each line is one insider's net activity with a deep link to that specific
    filing, so the batched card stays self-distinguishing per accession even
    though it's a single Discord post. Returns (embed, headline)."""
    timestamp = None
    if filing_date:
        try:
            timestamp = datetime.fromisoformat(filing_date).replace(
                tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass

    side_emoji = {"buy": "🟢", "sell": "🔴", "grant": "🎁",
                  "exercise": "🔁", "neutral": "⚪"}
    lines = []
    net = {"buy": 0.0, "sell": 0.0}
    ticker = None
    for f in filings:
        acc = f["accession"]
        idx = edgar_index_url(cik, acc)
        enriched = None
        try:
            enriched = sec_enrich.enrich_form4(cik, acc, f["primary_doc"],
                                               http_get_text, http_get_json)
        except Exception as e:
            print(f"[WARN] Form 4 enrichment failed ({acc}): {e}", file=sys.stderr)
        amend = " · 4/A" if f["form"].endswith("/A") else ""
        if enriched:
            ticker = ticker or enriched.get("ticker")
            insider = enriched.get("insider") or "Unknown insider"
            role = enriched.get("role") or "—"
            side = enriched.get("dominant_side", "neutral")
            val = enriched.get("total_value")
            if side in net:
                net[side] += val or 0.0
            lines.append(f"{side_emoji.get(side, '⚪')} [{insider}]({idx}) · "
                         f"{role} · {side.upper()} {sec_enrich.fmt_money(val)}{amend}")
        else:
            # Enrichment unavailable — still link the exact filing so the line is
            # distinguishable.
            lines.append(f"⚪ [{acc}]({idx}){amend} — _details unavailable_")

    if net["sell"] > net["buy"] and net["sell"] > 0:
        color = COLOR_SELL
    elif net["buy"] > net["sell"] and net["buy"] > 0:
        color = COLOR_BUY
    else:
        color = COLOR_NEUTRAL

    title_suffix = f" ({ticker})" if ticker else ""
    headline = (f"📄 {filer_name}{title_suffix} — {len(filings)} Form 4 filings "
                f"on {filing_date}")

    shown = lines[:25]
    desc = "\n".join(shown)
    if len(lines) > len(shown):
        desc += f"\n…and {len(lines) - len(shown)} more"

    all_url = (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
               f"&CIK={cik}&type=4&dateb=&owner=include&count=40")
    embed = {
        "title": f"{filer_name}{title_suffix} — {len(filings)} Form 4 filings",
        "url": all_url,
        "color": color,
        "description": desc[:4000],
        "fields": [
            {"name": "Filings", "value": str(len(filings)), "inline": True},
            {"name": "Net buy / sell",
             "value": f"{sec_enrich.fmt_money(net['buy'])} / {sec_enrich.fmt_money(net['sell'])}",
             "inline": True},
            {"name": "Filed", "value": filing_date or "—", "inline": True},
        ],
        "footer": {"text": f"📎 {len(filings)} accessions on {filing_date}"},
    }
    if timestamp:
        embed["timestamp"] = timestamp
    return embed, headline


def _form8k_embed(filer_name, form, filing_date, url, items_str, timestamp):
    items = sec_enrich.parse_8k_items(items_str or "")
    if items:
        item_lines = "\n".join(f"`{i['code']}` {i['label']}" for i in items)
        item_codes = ", ".join(i["code"] for i in items)
        headline = f"📋 {filer_name} — 8-K [{item_codes}]"
    else:
        item_lines = "_(no item codes reported)_"
        headline = f"📋 {filer_name} — 8-K filed {filing_date}"

    embed = {
        "title": f"{filer_name} — {form}",
        "url": url,
        "color": COLOR_8K,
        "description": item_lines[:4000],
        "fields": [{"name": "Filed", "value": filing_date or "—", "inline": True}],
    }
    if timestamp:
        embed["timestamp"] = timestamp
    return embed, headline


def _sc13_embed(filer_name, form, filing_date, url, cik, accession, primary_doc, timestamp):
    enriched = None
    try:
        enriched = sec_enrich.enrich_sc13(cik, accession, primary_doc,
                                          http_get_text, http_get_json)
    except Exception as e:
        print(f"[WARN] SC 13 enrichment failed: {e}", file=sys.stderr)

    if not enriched:
        headline = f"🎯 {filer_name} — {form} filed {filing_date}"
        embed = {
            "title": f"{filer_name} — {form}",
            "description": f"Filed {filing_date}",
            "url": url,
            "color": COLOR_13DG,
        }
        if timestamp:
            embed["timestamp"] = timestamp
        return embed, headline

    target = enriched.get("issuer_name") or "Unknown target"
    pct = enriched.get("percent_of_class")
    pct_str = f"{pct:.2f}%" if pct is not None else "?"
    shares = sec_enrich.fmt_shares(enriched.get("aggregate_amount"))
    headline = f"🎯 {filer_name} — {form} on {target}: {pct_str} ({shares} sh)"

    fields = [
        {"name": "Target", "value": target, "inline": False},
        {"name": "% of class", "value": pct_str, "inline": True},
        {"name": "Shares", "value": shares, "inline": True},
        {"name": "Filed", "value": filing_date or "—", "inline": True},
    ]
    if enriched.get("issuer_cusip"):
        fields.append({"name": "CUSIP", "value": enriched["issuer_cusip"], "inline": True})

    embed = {
        "title": f"{filer_name} — {form}",
        "url": url,
        "color": COLOR_13DG,
        "fields": fields,
    }
    if timestamp:
        embed["timestamp"] = timestamp
    return embed, headline


def _form13f_embed(filer_name, form, filing_date, url, cik, accession, timestamp):
    enriched = None
    try:
        enriched = sec_enrich.enrich_13fhr(cik, accession, http_get_text, http_get_json)
    except Exception as e:
        print(f"[WARN] 13F enrichment failed: {e}", file=sys.stderr)

    if not enriched:
        headline = f"🏦 {filer_name} — {form} filed {filing_date}"
        embed = {
            "title": f"{filer_name} — {form}",
            "description": f"Filed {filing_date}",
            "url": url,
            "color": COLOR_13F,
        }
        if timestamp:
            embed["timestamp"] = timestamp
        return embed, headline

    pos_count = enriched["position_count"]
    aum = sec_enrich.fmt_money(enriched["total_value_usd"])
    headline = f"🏦 {filer_name} — {form}: {pos_count} positions, {aum} AUM"

    def list_holdings(items, with_pct=False):
        if not items:
            return "_none_"
        out = []
        for h in items:
            line = f"• {h['issuer'][:40]} — {sec_enrich.fmt_money(h['value_usd'])}"
            if with_pct and "pct_change" in h:
                line += f" ({h['pct_change']:+.0%})"
            out.append(line)
        return "\n".join(out)

    fields = [
        {"name": "Positions", "value": str(pos_count), "inline": True},
        {"name": "Total value", "value": aum, "inline": True},
        {"name": "Filed", "value": filing_date or "—", "inline": True},
    ]
    if enriched.get("prior_accession"):
        fields.extend([
            {"name": f"🆕 New ({len(enriched['new_positions'])})",
             "value": list_holdings(enriched["new_positions"])[:1024], "inline": False},
            {"name": f"❌ Exited ({len(enriched['exited'])})",
             "value": list_holdings(enriched["exited"])[:1024], "inline": False},
            {"name": f"📈 Increased ({len(enriched['increased'])})",
             "value": list_holdings(enriched["increased"], with_pct=True)[:1024], "inline": False},
            {"name": f"📉 Decreased ({len(enriched['decreased'])})",
             "value": list_holdings(enriched["decreased"], with_pct=True)[:1024], "inline": False},
        ])
    else:
        fields.append({"name": "Diff", "value": "_(no prior 13F-HR found for comparison)_",
                       "inline": False})

    embed = {
        "title": f"{filer_name} — {form}",
        "url": url,
        "color": COLOR_13F,
        "fields": fields,
    }
    if timestamp:
        embed["timestamp"] = timestamp
    return embed, headline


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
    items_arr = recent.get("items", [])

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
            "items": items_arr[i] if i < len(items_arr) else "",
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

    # Group same-day Form 4 filings (incl. 4/A) per filing date. A large-cap
    # issuer can file many insider Form 4s in one day; one card each is noise and
    # the cards look near-identical. Lone Form 4s and all other forms keep their
    # own (richer) card. Each `unit` is the set of filings sharing one post.
    form4_by_date = {}
    units = []
    for f in new:
        base = f["form"][:-2] if f["form"].endswith("/A") else f["form"]
        if base == "4":
            form4_by_date.setdefault(f["filing_date"], []).append(f)
        else:
            units.append([f])
    for group in form4_by_date.values():
        if len(group) >= FORM4_BATCH_MIN:
            units.append(group)
        else:
            units.extend([f] for f in group)
    # Send oldest-first so alert_history stays chronological and seen_list keeps
    # the newest accessions at its tail (matching the trim in save_state).
    units.sort(key=lambda u: (min(f["filing_date"] for f in u),
                              min(f["accession"] for f in u)))

    def record(filing):
        seen_list.append(filing["accession"])
        seen_set.add(filing["accession"])
        state["alert_history"].append({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": "sec",
            "filer": name,
            "form": filing["form"],
            "filing_date": filing["filing_date"],
            "accession": filing["accession"],
        })

    sent = 0
    for unit in units:
        if alerts_left <= 0:
            break
        # One Discord post per unit (batched or not). Decrement budget regardless
        # of delivery to prevent infinite retry within a run; only mark filings
        # seen on confirmed delivery so transient failures retry next cron tick.
        alerts_left -= 1
        if len(unit) == 1:
            f = unit[0]
            url = filing_url(cik, f["accession"], f["primary_doc"])
            try:
                embed, headline = build_embed(name, f["form"], f["filing_date"], url,
                                              cik, f["accession"], f["primary_doc"],
                                              f["items"])
            except Exception as e:
                print(f"[WARN] embed build failed for {name} {f['accession']}: {e}",
                      file=sys.stderr)
                embed = None
                headline = f"📄 {name} — {f['form']} filed {f['filing_date']}\n<{url}>"
            delivered = alert(headline, embed=embed,
                              fallback_content=f"{headline}\n<{url}>")
        else:
            filing_date = unit[0]["filing_date"]
            try:
                embed, headline = _form4_batch_embed(name, cik, unit, filing_date)
            except Exception as e:
                print(f"[WARN] batch embed build failed for {name} {filing_date}: {e}",
                      file=sys.stderr)
                embed = None
                headline = f"📄 {name} — {len(unit)} Form 4 filings on {filing_date}"
            delivered = alert(headline, embed=embed, fallback_content=headline)

        if delivered:
            sent += 1
            for f in unit:
                record(f)

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

    state["last_run"] = started
    save_state(state)
    print(f"[DONE] alerts_sent={total_sent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
