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
    STOCKNEWS_REPO         = owner/repo of StockNews (default: zmzhong1/StockNews)
    STOCKNEWS_BRANCH       = StockNews branch to read (default: phase-1-scaffold)
    STOCKNEWS_TOP_N        = ranked-table rows to surface (default: 5)
    STOCKNEWS_GH_TOKEN     = PAT with Contents:Read on the StockNews repo;
                             required when StockNews is a private repo. Falls
                             back to GH_TOKEN / GITHUB_TOKEN if unset.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

STOCKNEWS_REPO = os.environ.get("STOCKNEWS_REPO", "zmzhong1/StockNews")
STOCKNEWS_BRANCH = os.environ.get("STOCKNEWS_BRANCH", "phase-1-scaffold")
# Cloudflare Pages production deploy. Override if the project is renamed
# or a custom domain is added.
STOCKNEWS_SITE_BASE_URL = os.environ.get(
    "STOCKNEWS_SITE_BASE_URL", "https://stocknews-87v.pages.dev"
).rstrip("/")
WATCHLIST_STATE_PATH = "dashboards/watchlist_state.md"
WATCHLIST_STATE_URL = (
    f"https://raw.githubusercontent.com/{STOCKNEWS_REPO}/"
    f"{STOCKNEWS_BRANCH}/{WATCHLIST_STATE_PATH}"
)
WATCHLIST_STATE_HTML = (
    f"https://github.com/{STOCKNEWS_REPO}/blob/{STOCKNEWS_BRANCH}/{WATCHLIST_STATE_PATH}"
)
WATCHLIST_STATE_API = (
    f"https://api.github.com/repos/{STOCKNEWS_REPO}/contents/"
    f"{WATCHLIST_STATE_PATH}?ref={STOCKNEWS_BRANCH}"
)
# Cloudflare-rendered equivalents the user can actually click and read.
WATCHLIST_STATE_SITE = f"{STOCKNEWS_SITE_BASE_URL}/dashboards/watchlist_state.html"
SITE_INDEX_URL = f"{STOCKNEWS_SITE_BASE_URL}/"

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
TOP_N = int(os.environ.get("STOCKNEWS_TOP_N", "5"))
# Optional GitHub token for reading a private StockNews repo. Falls back to
# unauthenticated raw.githubusercontent.com when unset.
GH_TOKEN = (
    os.environ.get("STOCKNEWS_GH_TOKEN")
    or os.environ.get("GH_TOKEN")
    or os.environ.get("GITHUB_TOKEN")
    or ""
).strip()

COLOR_DIGEST = 0x3498DB
USER_AGENT = "AutopilotWatcher-StockNewsDigest/1.0"


def fetch_watchlist_state():
    """Fetch dashboards/watchlist_state.md, with auth fallback for private repos.

    Order of attempts:
      1. raw.githubusercontent.com with optional Bearer token (works for both
         public and private repos when token has Contents:Read)
      2. api.github.com contents endpoint with Bearer token (private-repo
         fallback for tokens that aren't accepted on raw.githubusercontent.com)
    """
    headers = {"User-Agent": USER_AGENT}
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"

    req = urllib.request.Request(WATCHLIST_STATE_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code != 404 or not GH_TOKEN:
            raise
        print(
            f"[INFO] raw.githubusercontent.com 404 — falling back to GitHub contents API",
            file=sys.stderr,
        )

    api_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github.raw",
        "Authorization": f"Bearer {GH_TOKEN}",
    }
    api_req = urllib.request.Request(WATCHLIST_STATE_API, headers=api_headers)
    with urllib.request.urlopen(api_req, timeout=20) as resp:
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
            # Hyperlink each ticker to its rendered Cloudflare page so the
            # user can click straight into the full article from Discord.
            ticker_link = f"[**{t}**]({STOCKNEWS_SITE_BASE_URL}/{t}/tree_v1_en.html)" if t and t != "?" else f"**{t}**"
            lines.append(
                f"{ticker_link} — XII {xii} · H-0 {h0} · Dur {dur} {ff_marker}\n"
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
        "name": "📖 Read on site",
        "value": f"[Open dashboard]({WATCHLIST_STATE_SITE}) · [Index]({SITE_INDEX_URL})",
        "inline": True,
    })
    fields.append({
        "name": "Source",
        "value": f"[`dashboards/watchlist_state.md`]({WATCHLIST_STATE_HTML})",
        "inline": True,
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
