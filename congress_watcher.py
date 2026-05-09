#!/usr/bin/env python3
"""Capitol Trades watcher — scrapes www.capitoltrades.com/trades for new
congressional trades matching a watchlist, posts new ones to Discord.

Server-rendered HTML scraping (no browser at runtime, stdlib-only).
Capitol Trades' BFF API blocks the /trades endpoint, but the SSR HTML
contains the latest trades via ?pageSize= up to ~96 per request.

Local run:
    DISCORD_WEBHOOK='https://discord.com/api/webhooks/...' \
    python3 congress_watcher.py

Dry-run:
    DRY_RUN=1 python3 congress_watcher.py

GitHub Actions: configure DISCORD_WEBHOOK as a repository secret.
See .github/workflows/congress-watcher.yml.
"""

import html as html_lib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
WATCHLIST_PATH = ROOT / "watchlist.json"
STATE_PATH = ROOT / "congress_state.json"

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
MAX_ALERTS_PER_RUN = int(os.environ.get("MAX_ALERTS_PER_RUN", "20"))
PAGE_SIZE = int(os.environ.get("CAPITOL_TRADES_PAGE_SIZE", "96"))
ALERT_HISTORY_CAP = 200
DISCORD_RATE_DELAY_SEC = 0.5

COLOR_BUY = 0x2ECC71
COLOR_SELL = 0xE74C3C
COLOR_NEUTRAL = 0x95A5A6

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def fetch_trades_html(page_size):
    url = f"https://www.capitoltrades.com/trades?pageSize={page_size}"
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def strip_tags(s):
    s = re.sub(r"<[^>]+>", " ", s)
    s = html_lib.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_trades(html):
    """Return list of trade dicts from server-rendered HTML."""
    trades = []
    chunks = re.split(r'<tr[^>]*data-state="false"[^>]*>', html)
    for chunk in chunks[1:]:
        end = chunk.find("</tr>")
        if end < 0:
            continue
        row = chunk[:end]
        trade_id = re.search(r"/trades/(\d+)", row)
        if not trade_id:
            continue
        pol_id = re.search(r"/politicians/([A-Z]\d+)", row)
        issuer_id = re.search(r"/issuers/(\d+)", row)
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        cells = [strip_tags(td) for td in tds]
        if len(cells) < 9:
            continue
        trades.append({
            "trade_id": trade_id.group(1),
            "politician_id": pol_id.group(1) if pol_id else None,
            "issuer_id": issuer_id.group(1) if issuer_id else None,
            "politician": cells[0],   # "Name Party Chamber State"
            "issuer": cells[1],       # "Name Ticker"
            "pub_time": cells[2],
            "tx_date": cells[3],
            "days_lag": cells[4],
            "owner": cells[5],
            "trade_type": cells[6],
            "size_range": cells[7],
            "price": cells[8],
        })
    return trades


def matches_watchlist(politician_str, watchlist):
    """True if any watchlist substring (case-insensitive) is in politician_str."""
    if not watchlist:
        return True
    pol_lower = politician_str.lower()
    return any(w.lower() in pol_lower for w in watchlist)


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
        return {"seen_trade_ids": [], "first_run_done": False, "alert_history": []}
    try:
        state = json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        print("[WARN] congress_state.json malformed; starting fresh", file=sys.stderr)
        return {"seen_trade_ids": [], "first_run_done": False, "alert_history": []}
    state.setdefault("alert_history", [])
    return state


def save_state(state):
    if len(state.get("seen_trade_ids", [])) > 5000:
        state["seen_trade_ids"] = state["seen_trade_ids"][-5000:]
    if len(state.get("alert_history", [])) > ALERT_HISTORY_CAP:
        state["alert_history"] = state["alert_history"][-ALERT_HISTORY_CAP:]
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def build_alert(trade):
    """Return (embed, headline) for a Capitol Trades trade row."""
    politician = trade["politician"]
    issuer = trade["issuer"]
    trade_type = (trade["trade_type"] or "").lower()
    is_buy = "buy" in trade_type
    is_sell = "sell" in trade_type
    if is_buy:
        emoji, color = "🟢", COLOR_BUY
    elif is_sell:
        emoji, color = "🔴", COLOR_SELL
    else:
        emoji, color = "⚪", COLOR_NEUTRAL

    url = f"https://www.capitoltrades.com/trades/{trade['trade_id']}"
    headline = (f"🏛️ {emoji} {politician} {trade['trade_type'].upper()} {issuer} "
                f"· {trade['size_range']}")

    embed = {
        "title": f"🏛️ {emoji} {politician}",
        "description": f"**{trade['trade_type'].upper()}** {issuer}",
        "url": url,
        "color": color,
        "fields": [
            {"name": "Size", "value": trade["size_range"] or "—", "inline": True},
            {"name": "Owner", "value": trade["owner"] or "—", "inline": True},
            {"name": "Price", "value": trade["price"] or "—", "inline": True},
            {"name": "Trade date", "value": trade["tx_date"] or "—", "inline": True},
            {"name": "Published", "value": trade["pub_time"] or "—", "inline": True},
            {"name": "Lag", "value": trade["days_lag"] or "—", "inline": True},
        ],
    }
    return embed, headline


def main():
    if not WATCHLIST_PATH.exists():
        print(f"ERROR: watchlist.json not found at {WATCHLIST_PATH}", file=sys.stderr)
        return 1

    watchlist_data = json.loads(WATCHLIST_PATH.read_text())
    watchlist = watchlist_data.get("congress_members", [])

    state = load_state()
    is_first_run = not state.get("first_run_done", False)
    seen = set(state.get("seen_trade_ids", []))

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[START] {started}")
    print(f"        DRY_RUN={DRY_RUN}  Discord={'configured' if DISCORD_WEBHOOK else 'NOT SET'}")
    print(f"        first_run={is_first_run}  watchlist={watchlist or '(all politicians)'}  page_size={PAGE_SIZE}")

    try:
        html = fetch_trades_html(PAGE_SIZE)
    except urllib.error.HTTPError as e:
        print(f"[ERROR] Capitol Trades HTTP {e.code} {e.reason}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[ERROR] Capitol Trades fetch failed: {e}", file=sys.stderr)
        return 1

    trades = parse_trades(html)
    if not trades:
        print("[WARN] No trades parsed — Capitol Trades HTML may have changed.", file=sys.stderr)
        return 1

    print(f"[FETCH] {len(trades)} trades on page")

    matched = [t for t in trades if matches_watchlist(t["politician"], watchlist)]
    new = [t for t in matched if t["trade_id"] not in seen]
    print(f"[FILTER] {len(matched)} match watchlist, {len(new)} are new")

    if is_first_run:
        for t in matched:
            seen.add(t["trade_id"])
        state["seen_trade_ids"] = sorted(seen)
        state["first_run_done"] = True
        state["last_run"] = started
        save_state(state)
        print(f"[INIT-DONE] Seeded {len(matched)} trade IDs; subsequent runs alert on truly new only.")
        return 0

    sent = 0
    attempted = 0
    for trade in sorted(new, key=lambda t: t["trade_id"]):
        if attempted >= MAX_ALERTS_PER_RUN:
            print(f"[INFO] Alert budget hit ({MAX_ALERTS_PER_RUN}); deferring rest to next run")
            break
        attempted += 1
        embed, headline = build_alert(trade)
        url = f"https://www.capitoltrades.com/trades/{trade['trade_id']}"
        if alert(headline, embed=embed, fallback_content=f"{headline}\n<{url}>"):
            seen.add(trade["trade_id"])
            sent += 1
            state["alert_history"].append({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "kind": "congress",
                "politician": trade["politician"],
                "issuer": trade["issuer"],
                "trade_type": trade["trade_type"],
                "size_range": trade["size_range"],
                "tx_date": trade["tx_date"],
                "trade_id": trade["trade_id"],
            })

    state["seen_trade_ids"] = sorted(seen)
    state["last_run"] = started
    save_state(state)
    print(f"[DONE] alerts_sent={sent}  attempted={attempted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
