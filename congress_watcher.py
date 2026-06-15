#!/usr/bin/env python3
"""Congressional-trades watcher — posts new House/Senate stock disclosures
matching a watchlist to Discord.

Source strategy (stdlib-only, no browser):
  - Primary: kadoa's free congressional-trades mirror, served as a static JSON
    file from GitHub's CDN. The CDN can't IP-block the runner the way Capitol
    Trades and Senate eFD do (that block is what took this watcher dark before).
  - Fallback: Financial Modeling Prep's keyed `house-latest` / `senate-latest`
    API, used when kadoa is unreachable or its data has gone stale. Needs a
    free FMP_API_KEY; without one the watcher simply relies on kadoa alone.

Trades are deduped by a stable, source-agnostic synthetic id, so the same
disclosure is posted once regardless of which source served it.

Local run:
    DISCORD_WEBHOOK='https://discord.com/api/webhooks/...' \
    python3 congress_watcher.py

Dry-run:
    DRY_RUN=1 python3 congress_watcher.py

GitHub Actions: configure DISCORD_WEBHOOK (and optionally FMP_API_KEY) as
repository secrets. See .github/workflows/congress-watcher.yml.
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
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
STATE_PATH = ROOT / "congress_state.json"

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
MAX_ALERTS_PER_RUN = int(os.environ.get("MAX_ALERTS_PER_RUN", "20"))
ALERT_HISTORY_CAP = 200
DISCORD_RATE_DELAY_SEC = 0.5

# Primary source: kadoa's free, GitHub-CDN-hosted congressional-trades mirror
# (House + Senate, refreshed daily). If it goes stale (the pipeline is external
# to us), fall back to Financial Modeling Prep's keyed API.
KADOA_URL = os.environ.get(
    "KADOA_TRADES_URL",
    "https://raw.githubusercontent.com/kadoa-org/"
    "congress-trading-monitor/main/public/data/trades.json")
KADOA_STALE_DAYS = int(os.environ.get("KADOA_STALE_DAYS", "4"))
FMP_API_KEY = os.environ.get("FMP_API_KEY", "").strip()
FMP_BASE = os.environ.get("FMP_BASE", "https://financialmodelingprep.com/stable")
# FMP's free tier rejects limit > 20 (HTTP 402 Payment Required). 20 newest per
# chamber is plenty for a fallback that only runs while kadoa is down; raise it
# (or page) on a paid plan.
FMP_LIMIT = int(os.environ.get("FMP_LIMIT", "20"))
# Bump when the upstream source / id scheme changes to force a one-time reseed,
# so a swap doesn't re-alert the entire backlog under new ids.
SOURCE_VERSION = "kadoa-fmp-v1"

# Transient throttling/network errors are a "try again later" signal, not a
# watcher bug: retry with backoff, and if a fetch still fails skip the run
# instead of failing the workflow (the watcher is idempotent, so a skipped poll
# costs nothing).
FETCH_RETRIES = int(os.environ.get("CONGRESS_FETCH_RETRIES", "3"))
BACKOFF_BASE_SEC = 5
BACKOFF_CAP_SEC = 30
RETRYABLE_HTTP = frozenset({429, 500, 502, 503, 504})
# Escalate to Discord after this many consecutive failed fetches so a genuine
# outage (all sources down) isn't silently swallowed.
ESCALATE_AFTER_FAILURES = int(os.environ.get("CONGRESS_ESCALATE_AFTER", "12"))

COLOR_BUY = 0x2ECC71
COLOR_SELL = 0xE74C3C
COLOR_NEUTRAL = 0x95A5A6

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _retry_after_seconds(http_error, attempt):
    """Honor a numeric Retry-After header (capped); else exponential backoff."""
    backoff = min(BACKOFF_BASE_SEC * (2 ** attempt), BACKOFF_CAP_SEC)
    headers = getattr(http_error, "headers", None)
    raw = headers.get("Retry-After") if headers else None
    if raw:
        try:
            return min(int(raw), BACKOFF_CAP_SEC)
        except ValueError:
            pass  # HTTP-date form — fall back to backoff
    return backoff


def fetch_json(url, retries=FETCH_RETRIES):
    """GET `url` and parse JSON, retrying transient throttling/network errors.

    Raises the last error if all attempts fail; non-retryable HTTP codes
    (e.g. 403/404) raise immediately.
    """
    req = urllib.request.Request(
        url, headers={"User-Agent": BROWSER_UA, "Accept": "application/json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code not in RETRYABLE_HTTP or attempt == retries - 1:
                raise
            wait = _retry_after_seconds(e, attempt)
            print(f"[WARN] HTTP {e.code}; retry {attempt + 1}/{retries} in {wait}s",
                  file=sys.stderr)
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries - 1:
                raise
            wait = min(BACKOFF_BASE_SEC * (2 ** attempt), BACKOFF_CAP_SEC)
            print(f"[WARN] network error ({e}); retry {attempt + 1}/{retries} "
                  f"in {wait}s", file=sys.stderr)
            time.sleep(wait)


# -------------------- normalization + trade assembly --------------------

def _normalize_trade_type(raw):
    """Map a source's transaction label to a buy/sell-detectable string —
    build_alert and the heartbeat key off the 'buy'/'sell' substrings."""
    low = (raw or "").lower()
    if "purchase" in low or "buy" in low:
        return "Buy"
    if "sale" in low or "sell" in low:
        return "Sell"
    return (raw or "").strip() or "—"


def _clean_asset(name):
    """Collapse the multi-line asset/bond descriptions the feeds return."""
    return re.sub(r"\s+", " ", (name or "").strip())[:120]


def _name_key(name):
    """Order/punctuation-insensitive name key, so 'Nancy Pelosi' and
    'Pelosi, Nancy' produce the same synthetic id across sources."""
    return " ".join(sorted(re.findall(r"[a-z]+", (name or "").lower())))


def _synthetic_trade_id(chamber, politician, ticker, tx_date, trade_type, size):
    """Stable, source-agnostic id so the same disclosure dedupes identically
    whether it came from kadoa or the FMP fallback."""
    parts = [chamber or "", _name_key(politician), (ticker or "").upper(),
             tx_date or "", trade_type or "", re.sub(r"\s+", "", size or "")]
    raw = "|".join(parts).lower()
    return "c" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _trade(chamber, politician, ticker, asset, ttype, size, owner,
           tx_date, filing_date, days_lag, doc_url):
    """Assemble the watcher trade dict shared by every source parser."""
    ticker = (ticker or "").upper()
    asset = (asset or "").strip()
    issuer = f"{asset} {ticker}".strip() if ticker else (asset or "—")
    ttype = _normalize_trade_type(ttype)
    size = (size or "").strip() or "—"
    return {
        "trade_id": _synthetic_trade_id(chamber, politician, ticker, tx_date, ttype, size),
        "politician": (politician or "").strip() or "Unknown",
        "ticker": ticker or None,
        "issuer": issuer or "—",
        "trade_type": ttype,
        "size_range": size,
        "owner": (owner or "").strip(),
        "price": "",
        "tx_date": (tx_date or "").strip(),
        "pub_time": (filing_date or "").strip(),
        "days_lag": str(days_lag or "").strip(),
        "doc_url": (doc_url or "").strip(),
    }


def parse_kadoa(data):
    """kadoa trades.json (list of dicts) -> watcher trade dicts; congress only."""
    trades = []
    for r in data or []:
        chamber = (r.get("chamber") or "").lower()
        if chamber not in ("house", "senate"):
            continue  # skip executive-branch / OGE rows (chamber is null)
        trades.append(_trade(
            chamber, r.get("filer_name"), r.get("ticker"),
            _clean_asset(r.get("asset_name")), r.get("transaction_type"),
            r.get("amount_range_label"), r.get("owner"),
            r.get("transaction_date"), r.get("filing_date"),
            r.get("days_to_file"), r.get("doc_url")))
    return trades


def parse_fmp(rows, chamber):
    """FMP house-latest / senate-latest rows -> watcher trade dicts.

    FMP's field spellings vary by endpoint revision (note the historical
    'dateRecieved' misspelling), so several candidates are read defensively.
    """
    trades = []
    for r in rows or []:
        name = (r.get("name")
                or f"{r.get('firstName', '')} {r.get('lastName', '')}").strip()
        filing = (r.get("dateReceived") or r.get("dateRecieved")
                  or r.get("disclosureDate") or "")
        trades.append(_trade(
            chamber, name, r.get("symbol"),
            _clean_asset(r.get("assetDescription")),
            r.get("type") or r.get("transactionType"), r.get("amount"),
            r.get("owner"), r.get("transactionDate"), filing,
            "", r.get("link")))
    return trades


def _newest_filing(trades):
    return max((t.get("pub_time") or "" for t in trades), default="")


def is_stale(trades, today, max_age_days=KADOA_STALE_DAYS):
    """True if the newest filing date is missing or older than max_age_days."""
    newest = _newest_filing(trades)[:10]
    if not newest:
        return True
    try:
        d = datetime.strptime(newest, "%Y-%m-%d").date()
    except ValueError:
        return True
    return (today - d).days > max_age_days


def fetch_kadoa_trades():
    return parse_kadoa(fetch_json(KADOA_URL))


def fetch_fmp_trades():
    if not FMP_API_KEY:
        raise RuntimeError("FMP_API_KEY not set — no fallback source configured")
    house = fetch_json(f"{FMP_BASE}/house-latest?page=0&limit={FMP_LIMIT}&apikey={FMP_API_KEY}")
    senate = fetch_json(f"{FMP_BASE}/senate-latest?page=0&limit={FMP_LIMIT}&apikey={FMP_API_KEY}")
    return parse_fmp(house, "house") + parse_fmp(senate, "senate")


def fetch_congress_trades(today=None):
    """Return (trades, source): kadoa primary, FMP fallback when kadoa is
    unreachable or stale. Raises if no source yields data (caller soft-fails)."""
    today = today or datetime.now(timezone.utc).date()
    try:
        trades = fetch_kadoa_trades()
        if trades and not is_stale(trades, today):
            return trades, "kadoa"
        reason = ("kadoa returned no congress trades" if not trades
                  else f"kadoa stale (newest {_newest_filing(trades)[:10] or 'unknown'})")
    except Exception as e:
        reason = f"kadoa fetch failed: {e}"
    print(f"[WARN] {reason}; trying FMP fallback", file=sys.stderr)
    trades = fetch_fmp_trades()  # raises if no key / fetch fails
    if not trades:
        raise RuntimeError(f"{reason}; FMP returned no trades")
    return trades, "fmp"


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
    # parse:[] makes @everyone/@here/role mentions inert even if scraped trade
    # text reaches `content` (mentions only ping from the content field).
    payload = {"allowed_mentions": {"parse": []}}
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
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
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
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _clip(value, limit):
    """Clip a value to a Discord title/field limit. An over-long value makes
    Discord reject the whole embed, which would wedge that trade forever
    (never marked seen -> retried and rejected every run)."""
    s = value or "—"
    return s if len(s) <= limit else s[:limit - 1] + "…"


def build_alert(trade):
    """Return (embed, headline) for a congressional-trade disclosure."""
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

    url = trade.get("doc_url") or ""
    headline = (f"🏛️ {emoji} {politician} {trade['trade_type'].upper()} {issuer} "
                f"· {trade['size_range']}")

    embed = {
        "title": _clip(f"🏛️ {emoji} {politician}", 256),
        "description": _clip(f"**{trade['trade_type'].upper()}** {issuer}", 4096),
        "color": color,
        "fields": [
            {"name": "Size", "value": _clip(trade["size_range"], 1024), "inline": True},
            {"name": "Owner", "value": _clip(trade["owner"], 1024), "inline": True},
            {"name": "Trade date", "value": _clip(trade["tx_date"], 1024), "inline": True},
            {"name": "Published", "value": _clip(trade["pub_time"], 1024), "inline": True},
            {"name": "Lag (days)", "value": _clip(trade["days_lag"], 1024), "inline": True},
        ],
    }
    if url:
        embed["url"] = url
    return embed, headline


def main():
    if not WATCHLIST_PATH.exists():
        print(f"ERROR: watchlist.json not found at {WATCHLIST_PATH}", file=sys.stderr)
        return 1

    watchlist_data = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    watchlist = watchlist_data.get("congress_members", [])

    state = load_state()
    is_first_run = not state.get("first_run_done", False)
    # A one-time reseed when the upstream source (and thus id scheme) changes,
    # so the swap doesn't re-alert the whole backlog under new ids.
    reseed = is_first_run or state.get("source_version") != SOURCE_VERSION
    seen = set(state.get("seen_trade_ids", []))

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[START] {started}")
    print(f"        DRY_RUN={DRY_RUN}  Discord={'configured' if DISCORD_WEBHOOK else 'NOT SET'}")
    print(f"        first_run={is_first_run}  reseed={reseed}  "
          f"watchlist={watchlist or '(all politicians)'}")

    def soft_fail(reason):
        """Record a transient fetch failure and exit 0 instead of crashing.

        Persists a streak counter to state (runs are stateless) and pings
        Discord every ESCALATE_AFTER_FAILURES failures so a real outage surfaces.
        """
        fails = state.get("consecutive_fetch_failures", 0) + 1
        state["consecutive_fetch_failures"] = fails
        state["last_fetch_error"] = f"{reason} @ {started}"
        save_state(state)
        print(f"[SOFT-FAIL] {reason}; consecutive={fails}. Skipping run.", file=sys.stderr)
        if fails % ESCALATE_AFTER_FAILURES == 0:
            alert(f"⚠️ Congress Watcher: {fails} consecutive failed fetches. "
                  f"Latest: {reason}. All congress sources may be unavailable.")
        return 0

    try:
        trades, source = fetch_congress_trades()
    except Exception as e:
        return soft_fail(f"Congress trade fetch failed: {e}")
    if not trades:
        return soft_fail("No congress trades returned by any source")

    # Usable data in hand — clear any prior failure streak.
    if state.get("consecutive_fetch_failures"):
        print(f"[RECOVERED] fetch ok after {state['consecutive_fetch_failures']} failed run(s)")
        state["consecutive_fetch_failures"] = 0
        state.pop("last_fetch_error", None)

    print(f"[FETCH] {len(trades)} trades from {source}")

    matched = [t for t in trades if matches_watchlist(t["politician"], watchlist)]
    new = [t for t in matched if t["trade_id"] not in seen]
    print(f"[FILTER] {len(matched)} match watchlist, {len(new)} are new")

    if reseed:
        for t in matched:
            seen.add(t["trade_id"])
        state["seen_trade_ids"] = sorted(seen)
        state["first_run_done"] = True
        state["source_version"] = SOURCE_VERSION
        state["last_run"] = started
        save_state(state)
        why = "first run" if is_first_run else "source change"
        print(f"[INIT-DONE] Seeded {len(matched)} trade IDs ({why}); "
              f"subsequent runs alert on truly new only.")
        return 0

    sent = 0
    attempted = 0
    for trade in sorted(new, key=lambda t: (t.get("pub_time", ""), t.get("tx_date", ""), t["trade_id"])):
        if attempted >= MAX_ALERTS_PER_RUN:
            print(f"[INFO] Alert budget hit ({MAX_ALERTS_PER_RUN}); deferring rest to next run")
            break
        attempted += 1
        embed, headline = build_alert(trade)
        url = trade.get("doc_url") or ""
        fallback = f"{headline}\n<{url}>" if url else headline
        if alert(headline, embed=embed, fallback_content=fallback):
            seen.add(trade["trade_id"])
            sent += 1
            state["alert_history"].append({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "kind": "congress",
                "politician": trade["politician"],
                "issuer": trade["issuer"],
                "ticker": trade.get("ticker"),
                "trade_type": trade["trade_type"],
                "size_range": trade["size_range"],
                "tx_date": trade["tx_date"],
                "trade_id": trade["trade_id"],
            })

    state["seen_trade_ids"] = sorted(seen)
    state["source_version"] = SOURCE_VERSION
    state["last_run"] = started
    save_state(state)
    print(f"[DONE] alerts_sent={sent}  attempted={attempted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
