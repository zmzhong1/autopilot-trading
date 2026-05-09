#!/usr/bin/env python3
"""StockNews state digest — fetches dashboards/watchlist_state.md from the
sister StockNews repo, parses it, and posts a Discord embed showing what's
due this week + the top tickers by Section XII score.

Pairs with heartbeat.py: combines two repos' weekly views into one Discord
channel. Both scripts run from .github/workflows/heartbeat.yml on Mondays.

Local run:
    DISCORD_WEBHOOK='https://discord.com/api/webhooks/...' python3 stocknews_digest.py

Dry-run:
    DRY_RUN=1 python3 stocknews_digest.py

Tunables (env vars):
    STOCKNEWS_BRANCH       = StockNews branch to read (default: phase-1-scaffold)
    STOCKNEWS_TOP_N        = ranked-table rows to surface (default: 5)
"""

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone

STOCKNEWS_BRANCH = os.environ.get("STOCKNEWS_BRANCH", "phase-1-scaffold")
WATCHLIST_STATE_URL = (
    f"https://raw.githubusercontent.com/zmzhong1/StockNews/"
    f"{STOCKNEWS_BRANCH}/dashboards/watchlist_state.md"
)
WATCHLIST_STATE_HTML = (
    f"https://github.com/zmzhong1/StockNews/blob/{STOCKNEWS_BRANCH}/dashboards/watchlist_state.md"
)

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
TOP_N = int(os.environ.get("STOCKNEWS_TOP_N", "5"))

COLOR_DIGEST = 0x3498DB
USER_AGENT = "AutopilotWatcher-StockNewsDigest/1.0"


def fetch_watchlist_state():
    req = urllib.request.Request(WATCHLIST_STATE_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8")


def parse_action_items(md):
    """Return dict: '### subsection title' → list of bullet text.

    Only collects bullets under ## Action items — stops at the next ##.
    """
    sections = {}
    in_panel = False
    current = None
    current_lines = []

    for line in md.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = current_lines
                current = None
                current_lines = []
            in_panel = "Action items" in line
            continue
        if not in_panel:
            continue
        if line.startswith("### "):
            if current is not None:
                sections[current] = current_lines
            current = line[4:].strip()
            current_lines = []
            continue
        if current is None:
            continue
        if line.startswith("- "):
            current_lines.append(line[2:].strip())

    if current is not None:
        sections[current] = current_lines
    return sections


def parse_ranked_table(md):
    """Find the | Ticker | XII | ... | table and return list of row dicts."""
    rows = []
    in_table = False
    headers = None
    for line in md.splitlines():
        stripped = line.strip()
        if not in_table:
            if stripped.startswith("|") and "Ticker" in stripped and "XII" in stripped:
                headers = [c.strip() for c in stripped.strip("|").split("|")]
                in_table = True
            continue
        if not stripped.startswith("|"):
            in_table = False
            continue
        # Skip the header underline / separator row (only -, |, :, spaces)
        sep_chars = set(stripped) - {"|", "-", ":", " "}
        if not sep_chars:
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) != len(headers):
            continue
        row = dict(zip(headers, cells))
        ticker_cell = row.get("Ticker", "")
        m = re.search(r"\*\*([^\*]+)\*\*", ticker_cell)
        row["ticker"] = (m.group(1) if m else ticker_cell).strip()
        rows.append(row)
    return rows


def post_discord(embed):
    if not DISCORD_WEBHOOK:
        return False
    body = json.dumps({"embeds": [embed]}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
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


def build_embed(action_sections, ranked):
    fields = []

    today = next(
        (v for k, v in action_sections.items() if "TODAY" in k.upper()), []
    )
    if today:
        fields.append({
            "name": "🔔 Today",
            "value": "\n".join(f"• {t}" for t in today)[:1024],
            "inline": False,
        })

    due = next(
        (v for k, v in action_sections.items() if "Reviews due" in k), []
    )
    if due:
        fields.append({
            "name": "⏳ Reviews due within 7 days",
            "value": "\n".join(f"• {t}" for t in due)[:1024],
            "inline": False,
        })

    stale = next(
        (v for k, v in action_sections.items() if "stale" in k.lower() or "10-K" in k),
        [],
    )
    if stale:
        fields.append({
            "name": "📄 10-K cache stale",
            "value": "\n".join(f"• {t}" for t in stale)[:1024],
            "inline": False,
        })

    if ranked:
        lines = []
        for r in ranked[:TOP_N]:
            t = r.get("ticker", "?")
            xii = r.get("XII", "?")
            h0 = r.get("H-0", "?")
            dur = r.get("Durability", "?")
            ff = r.get("⚑ FF", "0")
            ff_marker = "⚑" if ff and ff.strip() not in ("0", "—", "-") else ""
            action = r.get("Action", "—")
            arch_raw = r.get("Archetype", "")
            am = re.search(r"`([^`]+)`", arch_raw)
            arch = am.group(1) if am else arch_raw
            lines.append(
                f"**{t}** — XII {xii} · H-0 {h0} · Dur {dur} {ff_marker}\n"
                f"   _{arch}_ · {action}"
            )
        fields.append({
            "name": f"🏆 Top {min(TOP_N, len(ranked))} by Section XII",
            "value": "\n".join(lines)[:1024],
            "inline": False,
        })

    if not fields:
        fields.append({
            "name": "—",
            "value": "_(no action items or rankings parsed; check StockNews `dashboards/watchlist_state.md`)_",
            "inline": False,
        })

    fields.append({
        "name": "Source",
        "value": f"[`dashboards/watchlist_state.md`]({WATCHLIST_STATE_HTML})",
        "inline": False,
    })

    return {
        "title": "📊 StockNews — weekly state",
        "description": "Cross-portfolio research view from the StockNews repo, refreshed each session.",
        "color": COLOR_DIGEST,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Autopilot Trading — StockNews digest"},
    }


def main():
    print(f"[START] StockNews digest — fetching {WATCHLIST_STATE_URL}")
    try:
        md = fetch_watchlist_state()
    except Exception as e:
        print(f"[ERROR] Fetch failed: {e}", file=sys.stderr)
        return 1

    action_sections = parse_action_items(md)
    ranked = parse_ranked_table(md)
    print(f"[PARSE] {sum(len(v) for v in action_sections.values())} action items, {len(ranked)} ranked rows")

    embed = build_embed(action_sections, ranked)
    if DRY_RUN:
        print(json.dumps(embed, indent=2))
        return 0
    ok = post_discord(embed)
    print(f"[DONE] posted={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
