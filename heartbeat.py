#!/usr/bin/env python3
"""Weekly heartbeat — confirms the watcher is alive and posts a digest of the
last 7 days of alerts to Discord.

Reads alert_history from state.json and congress_state.json (both are rolling
windows kept by the watchers themselves). No EDGAR or Capitol Trades calls
needed — just summarise what we already saw and posted.

Local run:
    DISCORD_WEBHOOK='https://discord.com/api/webhooks/...' python3 heartbeat.py

Dry-run:
    DRY_RUN=1 python3 heartbeat.py

GitHub Actions: see .github/workflows/heartbeat.yml.
"""

import json
import os
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import producer_status

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
SEC_STATE_PATH = ROOT / "state.json"
CONGRESS_STATE_PATH = ROOT / "congress_state.json"

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
WINDOW_DAYS = int(os.environ.get("HEARTBEAT_WINDOW_DAYS", "7"))
# The weekly digest producers that keep no state of their own — tracked via
# producer_status.json so a silent stop (a crash, a dead data source) surfaces
# here instead of the watcher still looking healthy.
DIGEST_PRODUCERS = ("discovery", "crowding", "regime", "confluence", "stocknews")

COLOR_HEALTHY = 0x2ECC71
COLOR_STALE = 0xE67E22


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
        # tolerate trailing Z
        s = s.replace("Z", "+00:00") if s.endswith("Z") else s
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def filter_recent(history, cutoff):
    out = []
    for h in history or []:
        ts = parse_iso(h.get("ts"))
        if ts and ts >= cutoff:
            out.append(h)
    return out


def post_discord(embed):
    if not DISCORD_WEBHOOK:
        return False
    body = json.dumps({"embeds": [embed]}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "AutopilotWatcher-Heartbeat/1.0",
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
        time.sleep(0.5)


def main():
    watchlist = load_json(WATCHLIST_PATH, {})
    sec_state = load_json(SEC_STATE_PATH, {})
    congress_state = load_json(CONGRESS_STATE_PATH, {})

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=WINDOW_DAYS)

    sec_recent = filter_recent(sec_state.get("alert_history", []), cutoff)
    congress_recent = filter_recent(congress_state.get("alert_history", []), cutoff)

    sec_form_counts = Counter(h.get("form", "?") for h in sec_recent)
    sec_top_filers = Counter(h.get("filer", "?") for h in sec_recent).most_common(5)
    congress_top_pols = Counter(h.get("politician", "?") for h in congress_recent).most_common(5)
    congress_buys = sum(1 for h in congress_recent if "buy" in (h.get("trade_type") or "").lower())
    congress_sells = sum(1 for h in congress_recent if "sell" in (h.get("trade_type") or "").lower())

    sec_last = parse_iso(sec_state.get("last_run"))
    congress_last = parse_iso(congress_state.get("last_run"))
    healthy = True
    stale_notes = []
    if not sec_last or (now - sec_last) > timedelta(days=1):
        healthy = False
        stale_notes.append(
            f"SEC watcher last ran {sec_last.isoformat(timespec='minutes') if sec_last else 'never'}"
        )
    if not congress_last or (now - congress_last) > timedelta(days=2):
        healthy = False
        stale_notes.append(
            f"Congress watcher last ran "
            f"{congress_last.isoformat(timespec='minutes') if congress_last else 'never'}"
        )
    for name, note in producer_status.stale(producer_status.load(), DIGEST_PRODUCERS, now):
        healthy = False
        stale_notes.append(f"{name} digest — {note}")

    cik_count = len(watchlist.get("sec_ciks", []))
    pol_count = len(watchlist.get("congress_members", []))
    pol_label = f"{pol_count}" if pol_count else "all"

    title = "💓 Watcher heartbeat — healthy" if healthy else "⚠️ Watcher heartbeat — stale"
    color = COLOR_HEALTHY if healthy else COLOR_STALE

    fields = [
        {"name": "Window", "value": f"last {WINDOW_DAYS} days", "inline": True},
        {"name": "CIKs watched", "value": str(cik_count), "inline": True},
        {"name": "Politicians", "value": pol_label, "inline": True},
        {"name": "SEC alerts", "value": str(len(sec_recent)), "inline": True},
        {"name": "Congress alerts", "value": str(len(congress_recent)), "inline": True},
        {"name": "Buys / Sells (Congress)",
         "value": f"{congress_buys} 🟢 / {congress_sells} 🔴", "inline": True},
    ]
    if sec_form_counts:
        fields.append({
            "name": "SEC by form",
            "value": "\n".join(f"`{f}` × {n}" for f, n in sec_form_counts.most_common()),
            "inline": False,
        })
    if sec_top_filers:
        fields.append({
            "name": "Top SEC filers",
            "value": "\n".join(f"• {name} — {n}" for name, n in sec_top_filers),
            "inline": True,
        })
    if congress_top_pols:
        fields.append({
            "name": "Top politicians",
            "value": "\n".join(f"• {name} — {n}" for name, n in congress_top_pols),
            "inline": True,
        })
    if stale_notes:
        fields.append({"name": "⚠️ Stale", "value": "\n".join(stale_notes), "inline": False})

    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "timestamp": now.isoformat(),
        "footer": {"text": "Autopilot Trading — weekly digest"},
    }

    print(f"[HEARTBEAT] {title}  sec={len(sec_recent)}  congress={len(congress_recent)}")
    if DRY_RUN:
        print(json.dumps(embed, indent=2))
        return 0
    ok = post_discord(embed)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
