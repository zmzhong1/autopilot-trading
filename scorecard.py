#!/usr/bin/env python3
"""Weekly agentic-proposal scorecard — the track record that makes the system
worth sharing.

executor.py logs every proposal it makes to the committed proposals_log.json,
stamped with the entry price at proposal time. This marks each open proposal to
the current market price, computes per-signal return + an aggregate hit-rate and
average return, and posts a card to the SHARED agentic Discord channel
(EXECUTOR_DISCORD_WEBHOOK, falling back to DISCORD_WEBHOOK).

It is read-only: it never trades, never touches Robinhood — just scores what the
proposal engine called. Sizing is shown as % (never $) so a channel with other
people in it can't infer the account balance.

Stdlib-only. Local run:
    DRY_RUN=1 python3 scorecard.py
GitHub Actions: runs in .github/workflows/executor.yml after the executor.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

ROOT = Path(__file__).parent
PROPOSALS_LOG_PATH = ROOT / "proposals_log.json"

DISCORD_WEBHOOK = (os.environ.get("EXECUTOR_DISCORD_WEBHOOK", "").strip()
                   or os.environ.get("DISCORD_WEBHOOK", "").strip())
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
TOP_N = int(os.environ.get("SCORECARD_TOP_N", "15"))
DISCORD_RATE_DELAY_SEC = 0.5

COLOR_UP = 0x2ECC71
COLOR_DOWN = 0xE74C3C
COLOR_FLAT = 0x95A5A6


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _parse_date(s):
    try:
        return datetime.fromisoformat(s).date()
    except (TypeError, ValueError):
        return None


# -------------------- Pure scoring logic (unit-tested) --------------------

def compute_scorecard(rows, price_map, today=None):
    """Mark each tracked proposal to market. Pure: no network.

    rows: proposals_log 'proposals' entries ({ticker, side, date, entry_price,
    feeds, ...}). price_map: {ticker: current_price}. Returns
    {scored: [...], n, n_scored, hit_rate, avg_return, best, worst} where each
    scored row adds current_price, return_pct, hold_days. A row is skipped (but
    counted in n) when it lacks an entry price or has no current price — we can't
    score what we can't mark.
    """
    scored = []
    for r in rows:
        entry = r.get("entry_price")
        cur = price_map.get(r.get("ticker"))
        if not entry or not cur:
            continue
        try:
            entry = float(entry)
            cur = float(cur)
        except (TypeError, ValueError):
            continue
        if entry <= 0:
            continue
        sign = -1.0 if r.get("side") == "sell" else 1.0
        ret = sign * (cur - entry) / entry * 100.0
        d = _parse_date(r.get("date"))
        hold = (today - d).days if (today and d) else None
        scored.append({
            "ticker": r.get("ticker"), "side": r.get("side", "buy"),
            "date": r.get("date"), "entry_price": round(entry, 2),
            "current_price": round(cur, 2), "return_pct": round(ret, 2),
            "hold_days": hold, "feeds": r.get("feeds", []),
        })
    n_scored = len(scored)
    wins = sum(1 for s in scored if s["return_pct"] > 0)
    avg = round(sum(s["return_pct"] for s in scored) / n_scored, 2) if n_scored else 0.0
    return {
        "scored": scored,
        "n": len(rows),
        "n_scored": n_scored,
        "hit_rate": round(wins / n_scored * 100, 1) if n_scored else 0.0,
        "avg_return": avg,
        "best": max(scored, key=lambda s: s["return_pct"]) if scored else None,
        "worst": min(scored, key=lambda s: s["return_pct"]) if scored else None,
    }


# -------------------- Discord --------------------

def post_discord(embed):
    if not DISCORD_WEBHOOK:
        return False
    body = json.dumps({"embeds": [embed]}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK, data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "AutopilotWatcher-Scorecard/1.0"},
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


def build_embed(card):
    avg = card["avg_return"]
    color = COLOR_UP if avg > 0 else COLOR_DOWN if avg < 0 else COLOR_FLAT
    rows = sorted(card["scored"], key=lambda s: s["return_pct"], reverse=True)
    lines = []
    for s in rows[:TOP_N]:
        emoji = "🟢" if s["return_pct"] > 0 else "🔴" if s["return_pct"] < 0 else "⚪"
        age = f", {s['hold_days']}d" if s["hold_days"] is not None else ""
        feeds = "+".join(s.get("feeds", []))
        lines.append(f"{emoji} `{s['ticker']}` **{s['return_pct']:+.1f}%** "
                     f"(since {s['date']}{age}) [{feeds}]")
    if not lines:
        lines.append("_No scored signals yet — proposals need an entry price and "
                     "a current quote to mark to market._")

    fields = [{"name": f"Signals ({card['n_scored']} scored / {card['n']} logged)",
               "value": "\n".join(lines)[:1024], "inline": False}]
    if card["n_scored"]:
        b, w = card["best"], card["worst"]
        fields.append({
            "name": "Aggregate",
            "value": (f"Hit-rate **{card['hit_rate']:.0f}%** · "
                      f"avg **{card['avg_return']:+.1f}%**\n"
                      f"best `{b['ticker']}` {b['return_pct']:+.1f}% · "
                      f"worst `{w['ticker']}` {w['return_pct']:+.1f}%"),
            "inline": False,
        })
    return {
        "title": "📈 Agentic proposal scorecard",
        "description": ("Hypothetical equal-weight performance of every proposal "
                        "the agent has made, marked to the latest close. "
                        "Propose-only — these are signals, not executed trades."),
        "color": color, "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Autopilot Trading — proposal track record"},
    }


def main():
    log = load_json(PROPOSALS_LOG_PATH, {"proposals": []})
    rows = log.get("proposals", [])
    print(f"[START] scorecard — {len(rows)} logged proposals  "
          f"DRY_RUN={DRY_RUN}  Discord={'configured' if DISCORD_WEBHOOK else 'NOT SET'}")
    if not rows:
        print("[INFO] no proposals logged yet; nothing to score.")
        return 0

    import prices
    tickers = sorted({r.get("ticker") for r in rows if r.get("ticker")})
    price_map = prices.latest_closes(tickers)
    card = compute_scorecard(rows, price_map, today=datetime.now(timezone.utc).date())
    print(f"[SCORECARD] {card['n_scored']}/{card['n']} scored · "
          f"hit-rate {card['hit_rate']}% · avg {card['avg_return']}%")

    embed = build_embed(card)
    if DRY_RUN:
        print(json.dumps(embed, indent=2))
        return 0
    ok = post_discord(embed) if DISCORD_WEBHOOK else True
    print(f"[DONE] posted={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    import producer_status
    rc = main()
    producer_status.record("scorecard", ok=(rc == 0))
    sys.exit(rc)
