#!/usr/bin/env python3
"""Guardrailed execution layer — turns vetted confluence signals into PROPOSED
orders, and (only when you opt in) places them via the Robinhood Agentic MCP.

This closes the loop the rest of the repo deliberately left open: the watchers
surface high-signal names (insider cluster buys, congressional trades, crowded
13Fs, cross-feed confluence), but README.md > 'What this does NOT cover > Trade
execution' says you place orders by hand. This module proposes them instead —
behind hard risk limits, with live trading off by default.

Safety posture (see guardrails.json + robinhood_mcp.py):
  - SHIPPED DEFAULT = propose-only. Nothing is executed. The executor vets
    signals against guardrails.json and posts a "PROPOSED orders" Discord card.
  - 'paper' mode simulates fills against a virtual cash balance — no real money.
  - 'live' mode requires guardrails.enabled=true AND a wired robinhood_mcp
    (access is still rolling out). Until that's wired, live degrades to propose
    and says so, loudly, rather than pretending it traded.
  - EXECUTOR_KILL=1 force-disables execution regardless of config.

Signal source: reuses confluence.py (the same congress/insider/8-K/13F overlap
the Monday digest computes), keeping one definition of "high-signal".

Stdlib-only. Local run (propose-only, no Discord):
    SEC_USER_AGENT='Your Name you@email.com' DRY_RUN=1 python3 executor.py

GitHub Actions: see .github/workflows/executor.yml (runs propose-only).
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Windows consoles default to a non-UTF-8 codec; force UTF-8 so emoji in output
# don't raise. No-op on the CI runner / when stdout isn't reconfigurable.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

ROOT = Path(__file__).parent
GUARDRAILS_PATH = ROOT / "guardrails.json"
STATE_PATH = ROOT / "executor_state.json"
PROPOSALS_LOG_PATH = ROOT / "proposals_log.json"  # committed track record

# Agentic output (proposals + scorecard) posts to its OWN webhook so a shared
# Discord channel carries only agentic-trade content — not the SEC/Congress/
# heartbeat watcher noise, which stays on DISCORD_WEBHOOK. Falls back to
# DISCORD_WEBHOOK when the dedicated one isn't set (single-channel setups).
DISCORD_WEBHOOK = (os.environ.get("EXECUTOR_DISCORD_WEBHOOK", "").strip()
                   or os.environ.get("DISCORD_WEBHOOK", "").strip())
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
KILL = os.environ.get("EXECUTOR_KILL", "").lower() in ("1", "true", "yes")
DISCORD_RATE_DELAY_SEC = 0.5

COLOR_PROPOSE = 0x3498DB   # blue  — proposals only, nothing executed
COLOR_PAPER = 0x9B59B6     # purple — simulated fills
COLOR_LIVE = 0x2ECC71      # green — real orders placed
COLOR_BLOCKED = 0xE74C3C   # red   — kill switch / nothing approved

# Defaults mirror guardrails.json so a missing key never crashes a run.
DEFAULT_GUARDRAILS = {
    "enabled": False,
    "mode": "propose",
    "account_value_usd": 1000.0,
    "allow_list": [],
    "block_list": [],
    "allowed_sides": ["buy"],
    "min_signal_feeds": 3,
    "max_notional_per_order_usd": 100.0,
    "max_pct_account_per_order": 0.05,
    "max_orders_per_day": 3,
    "max_total_deployed_pct": 0.50,
    "order_type": "market",
    "limit_offset_pct": 0.0,
    "allow_options": False,
    "allow_leverage": False,
    "share_size_display": "pct",  # pct | usd | none — for the shared channel
    # Research/portfolio grounding — every trade must agree with the StockNews
    # thesis and respect the existing book (see enrichment.py).
    "require_stocknews_thesis": True,
    "max_fatal_flags": 0,
    "min_xii_score": 45,
    "max_existing_position_pct": 25.0,
    "min_conviction": "medium",  # research-based purchase conviction floor
    "entry_price_anchor_max_ratio": 5.0,  # drop quotes >5x off the StockNews anchor
}


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _as_float(value, default, lo=None, hi=None):
    """Parse value as float, falling back to default on garbage, then clamp.
    Used to sanitize numeric guardrails: a fat-fingered string can't crash a run
    mid-flight, and a negative cap can't silently disable a limit."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        x = max(lo, x)
    if hi is not None:
        x = min(hi, x)
    return x


def _as_int(value, default, lo=None, hi=None):
    """Integer counterpart of _as_float (caps that must be whole numbers)."""
    try:
        x = int(value)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        x = max(lo, x)
    if hi is not None:
        x = min(hi, x)
    return x


def load_guardrails(path=GUARDRAILS_PATH):
    """Merge on top of defaults so partial/old config files still work, then
    sanitize the numeric limits. Keys starting with '_' (the inline help in
    guardrails.json) are ignored.

    Sanitizing matters because these numbers are the safety net: a negative
    `max_total_deployed_pct` would otherwise make the deploy check always pass,
    and a non-numeric value would crash the run after some orders already went
    out. Percentages clamp to [0,1]; dollars/counts floor at 0; anything
    unparseable reverts to the shipped default."""
    raw = load_json(path, {})
    gr = dict(DEFAULT_GUARDRAILS)
    for k, v in raw.items():
        if not k.startswith("_"):
            gr[k] = v
    d = DEFAULT_GUARDRAILS
    gr["account_value_usd"] = _as_float(gr.get("account_value_usd"),
                                        d["account_value_usd"], lo=0.0)
    gr["max_notional_per_order_usd"] = _as_float(
        gr.get("max_notional_per_order_usd"), d["max_notional_per_order_usd"], lo=0.0)
    gr["max_pct_account_per_order"] = _as_float(
        gr.get("max_pct_account_per_order"), d["max_pct_account_per_order"],
        lo=0.0, hi=1.0)
    gr["max_total_deployed_pct"] = _as_float(
        gr.get("max_total_deployed_pct"), d["max_total_deployed_pct"], lo=0.0, hi=1.0)
    gr["limit_offset_pct"] = _as_float(gr.get("limit_offset_pct"),
                                       d["limit_offset_pct"], lo=0.0, hi=1.0)
    gr["max_orders_per_day"] = _as_int(gr.get("max_orders_per_day"),
                                       d["max_orders_per_day"], lo=0)
    # min_signal_feeds: a non-int or negative value reverts to the default floor
    # (3) rather than 0 — a typo must never *weaken* the corroboration bar.
    mf = _as_int(gr.get("min_signal_feeds"), d["min_signal_feeds"])
    gr["min_signal_feeds"] = mf if mf >= 0 else d["min_signal_feeds"]
    return gr


# -------------------- Pure sizing + guardrail logic (unit-tested) -----------

def size_order(account_value, gr):
    """Dollar size for one order: the smaller of the absolute cap and the
    percent-of-account cap. Never negative."""
    pct_cap = max(0.0, account_value) * float(gr["max_pct_account_per_order"])
    abs_cap = float(gr["max_notional_per_order_usd"])
    return round(max(0.0, min(abs_cap, pct_cap)), 2)


def orders_today(state, now):
    """Count orders recorded in state whose ts is on `now`'s UTC calendar day.
    Counts both executed and proposed orders so propose-mode also respects the
    daily cap (a preview of what live would have done)."""
    today = now.date().isoformat()
    n = 0
    for o in state.get("orders", []):
        ts = o.get("ts") or ""  # tolerate missing/None ts in a corrupt state file
        if isinstance(ts, str) and ts[:10] == today:
            n += 1
    return n


def deployed_notional(state):
    """Sum of notionals already deployed (paper/live filled orders only)."""
    total = 0.0
    for o in state.get("orders", []):
        if o.get("status") in ("paper_filled", "live_filled"):
            try:
                total += float(o.get("notional_usd", 0) or 0)
            except (TypeError, ValueError):
                continue  # skip a corrupt entry rather than crash the run
    return total


def evaluate_proposals(ranked, gr, account_value, state, now):
    """Pure: given ranked confluence signals + guardrails + current state, return
    (approved, rejected). Each element is a proposal dict; rejected ones carry a
    'reason'. No network, no side effects — this is the safety core under test.

    `ranked` items follow confluence.score_confluence shape:
        {ticker, issuer, feed_count, total, feeds:[...], counts:{...}}
    """
    approved, rejected = [], []

    allow = {t.upper() for t in gr.get("allow_list", [])}
    block = {t.upper() for t in gr.get("block_list", [])}
    sides = [s.lower() for s in gr.get("allowed_sides", ["buy"])]
    min_feeds = int(gr.get("min_signal_feeds", 3))
    size = size_order(account_value, gr)
    day_cap = int(gr.get("max_orders_per_day", 0))
    used_today = orders_today(state, now)
    deployed = deployed_notional(state)
    deploy_cap = float(gr.get("max_total_deployed_pct", 0)) * max(0.0, account_value)

    # The executor only ever *buys* on a positive confluence signal. Selling on
    # the absence of a signal is a different (and riskier) policy we don't infer.
    side = "buy"

    slots_left = max(0, day_cap - used_today)

    for sig in ranked:
        ticker = (sig.get("ticker") or "").upper()
        base = {
            "ticker": ticker,
            "side": side,
            "feed_count": sig.get("feed_count", 0),
            "feeds": sig.get("feeds", []),
            "issuer": sig.get("issuer", ""),
            "notional_usd": size,
            "order_type": gr.get("order_type", "market"),
        }

        def reject(reason):
            r = dict(base)
            r["reason"] = reason
            rejected.append(r)

        if not ticker:
            reject("no ticker on signal")
            continue
        if side not in sides:
            reject(f"side '{side}' not in allowed_sides {sides}")
            continue
        if sig.get("feed_count", 0) < min_feeds:
            reject(f"only {sig.get('feed_count', 0)} feeds (< min {min_feeds})")
            continue
        if ticker not in allow:  # empty allow_list => nothing tradable (fail-closed)
            reject("not in allow_list")
            continue
        if ticker in block:
            reject("in block_list")
            continue
        if size <= 0:
            reject("order size resolved to $0 (check caps / account value)")
            continue
        if deployed + size > deploy_cap:
            reject(f"would exceed max deployed ${deploy_cap:,.0f} "
                   f"(already ${deployed:,.0f})")
            continue
        if len(approved) >= slots_left:
            reject(f"daily order cap reached ({day_cap}/day, "
                   f"{used_today} already today)")
            continue

        approved.append(base)
        deployed += size  # reserve against the deploy cap within this run

    return approved, rejected


_CONVICTION_RANK = {"none": 0, "avoid": 0, "low": 1, "medium": 2, "high": 3}


def gate_on_context(approved, gr, account_value, enrich_fn, today=None):
    """Ground each guardrail-approved proposal in the StockNews research and the
    existing portfolio (enrich_fn(ticker, account_value) -> {thesis, portfolio}).

    Returns (kept, rejected). Every kept proposal carries the thesis, portfolio,
    and a research-based purchase `assessment` (conviction from XII + H-0 +
    durability + asymmetry + freshness) so it is *acknowledged* on the card and
    in the track record. A proposal is dropped when the research contradicts the
    buy (no thesis / fatal flag / XII below the floor / conviction below
    min_conviction) or it would over-concentrate an existing holding. Pure given
    enrich_fn (injected in tests)."""
    import enrichment
    require = gr.get("require_stocknews_thesis", True)
    max_fatal = int(gr.get("max_fatal_flags", 0))
    min_xii = int(gr.get("min_xii_score", 45))
    max_pos = float(gr.get("max_existing_position_pct", 100))
    min_conv = gr.get("min_conviction", "medium")
    min_conv_rank = _CONVICTION_RANK.get(min_conv, 2)

    kept, rejected = [], []
    for p in approved:
        ctx = enrich_fn(p["ticker"], account_value) or {}
        th = ctx.get("thesis", {}) or {}
        pf = ctx.get("portfolio", {}) or {}
        rs = ctx.get("research", {}) or {}
        ev = ctx.get("events_8k") or []
        # acknowledge on the proposal
        p = dict(p, thesis=th, portfolio=pf, research=rs, events_8k=ev)

        if require and not th.get("found"):
            rejected.append(dict(p, reason="no StockNews thesis on file"))
            continue
        if th.get("found") and th.get("fatal_flags", 0) > max_fatal:
            rejected.append(dict(p, reason=(
                f"StockNews fatal flag ({th['fatal_flags']} > {max_fatal})")))
            continue
        xii = th.get("xii_score")
        if th.get("found") and xii is not None and xii < min_xii:
            rejected.append(dict(p, reason=(
                f"StockNews XII {xii}% < {min_xii}% (verdict {th.get('verdict')})")))
            continue

        # Research-based conviction: quality + confidence + durability +
        # asymmetry + freshness, not just the headline XII number.
        assessment = enrichment.assess_purchase(th, today=today)
        p = dict(p, assessment=assessment)
        if _CONVICTION_RANK.get(assessment["conviction"], 0) < min_conv_rank:
            reasons = "; ".join(assessment.get("reasons", [])[:3])
            rejected.append(dict(p, reason=(
                f"conviction {assessment['conviction']} < {min_conv} ({reasons})")))
            continue

        if pf.get("checked") and pf.get("held") and account_value:
            val = pf.get("value_usd")
            try:
                pos_pct = float(val) / account_value * 100 if val else 0.0
            except (TypeError, ValueError):
                pos_pct = 0.0
            if pos_pct > max_pos:
                rejected.append(dict(p, reason=(
                    f"already {pos_pct:.0f}% of acct (> {max_pos:.0f}% cap)")))
                continue
        kept.append(p)
    return kept, rejected


def decide_mode(gr):
    """Resolve the effective execution mode after the kill switch + enabled flag.
    Returns one of 'propose', 'paper', 'live'. Anything not explicitly enabled
    collapses to 'propose' — fail-safe."""
    if KILL:
        return "propose"
    if not gr.get("enabled", False):
        return "propose"
    mode = (gr.get("mode") or "propose").lower()
    return mode if mode in ("propose", "paper", "live") else "propose"


# -------------------- Execution (paper sim + live MCP boundary) -------------

def execute(approved, mode, now):
    """Carry out approved proposals per mode. Returns (results, notes) where each
    result is a proposal dict augmented with status + any fill info, and notes is
    a list of human-readable strings for the Discord card."""
    results, notes = [], []
    ts = now.isoformat(timespec="seconds")

    if mode == "propose":
        for p in approved:
            r = dict(p, status="proposed", ts=ts)
            results.append(r)
        return results, notes

    if mode == "paper":
        for p in approved:
            r = dict(p, status="paper_filled", ts=ts)
            results.append(r)
        notes.append("📝 Paper mode — fills are simulated, no real money moved.")
        return results, notes

    # mode == "live"
    try:
        import robinhood_mcp
        wired = robinhood_mcp.is_wired()
    except Exception as e:  # import/attr error must degrade, never trade blindly
        notes.append(f"⚠️ Robinhood MCP unavailable ({e}); proposing, not placing.")
        wired = False
    if not wired:
        notes.append(
            "⚠️ Live mode requested but the Robinhood MCP is not wired — "
            "orders below are PROPOSED, not placed. See robinhood_mcp.py."
        )
        for p in approved:
            r = dict(p, status="proposed_live_unwired", ts=ts)
            results.append(r)
        return results, notes

    for p in approved:
        try:
            resp = robinhood_mcp.place_order(
                ticker=p["ticker"], side=p["side"],
                notional_usd=p["notional_usd"], order_type=p["order_type"],
            )
            r = dict(p, status="live_filled", ts=ts, broker_response=resp)
        except Exception as e:  # never let one bad order abort the batch
            r = dict(p, status="live_error", ts=ts, error=str(e))
            notes.append(f"❌ {p['ticker']} live order failed: {e}")
        results.append(r)
    return results, notes


def record_state(state, results, path=STATE_PATH):
    """Append results to executor_state.json, capping history. Best-effort."""
    orders = state.get("orders", [])
    orders.extend(results)
    state["orders"] = orders[-500:]
    state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"[WARN] could not write {path.name}: {e}", file=sys.stderr)


# Statuses that represent a position the system took (or would have): these go
# into the committed track record so the weekly scorecard can score them.
TRACKED_STATUSES = ("proposed", "proposed_live_unwired", "paper_filled",
                    "live_filled")


def price_is_sane(price, anchor, max_ratio):
    """Reject a quote that's off by more than max_ratio× the StockNews anchor — a
    cheap guard against a fat-fingered / wrong-symbol print polluting the track
    record. Pure. With no price -> not sane; with no usable anchor -> accept
    (can't check). A legit move (even +50%) passes; a 10× error is caught."""
    if price is None:
        return False
    try:
        price = float(price)
    except (TypeError, ValueError):
        return False
    if price <= 0:
        return False
    if not anchor or anchor <= 0:
        return True  # no anchor to compare against; accept the quote
    ratio = max(price / anchor, anchor / price)
    return ratio <= max_ratio


def record_proposals_log(results, account_value, now, path=PROPOSALS_LOG_PATH,
                         price_fn=None, max_anchor_ratio=5.0):
    """Append one shareable track-record row per tracked order, stamped with the
    entry price at proposal time. Unlike executor_state.json this file IS
    committed, so the weekly scorecard can mark each row to market later.
    An entry price wildly off the StockNews anchor (>max_anchor_ratio×) is
    dropped to null rather than recorded. Best-effort; never raises. `price_fn`
    is injectable for tests."""
    if price_fn is None:
        import prices
        price_fn = prices.latest_close
    log = load_json(path, {"proposals": []})
    rows = log.get("proposals", [])
    date = now.date().isoformat()
    for r in results:
        if r.get("status") not in TRACKED_STATUSES:
            continue
        th = r.get("thesis") or {}
        pf = r.get("portfolio") or {}
        entry = price_fn(r["ticker"])
        if not price_is_sane(entry, th.get("anchor_price"), max_anchor_ratio):
            if entry is not None:
                print(f"[WARN] {r['ticker']} quote {entry} implausible vs anchor "
                      f"{th.get('anchor_price')}; dropping entry price", file=sys.stderr)
            entry = None
        rows.append({
            "date": date,
            "ts": r.get("ts"),
            "ticker": r["ticker"],
            "side": r["side"],
            "size_pct": (round(r["notional_usd"] / account_value * 100, 3)
                         if account_value else None),
            "entry_price": entry,
            "feeds": r.get("feeds", []),
            "feed_count": r.get("feed_count"),
            "status": r["status"],
            # Acknowledge the grounding in the committed record too.
            "stocknews": {"xii_score": th.get("xii_score"),
                          "verdict": th.get("verdict"),
                          "h0": th.get("h0"),
                          "durability": th.get("durability"),
                          "conviction": (r.get("assessment") or {}).get("conviction"),
                          "fatal_flags": th.get("fatal_flags"),
                          "found": th.get("found", False)},
            "portfolio": {"held": pf.get("held"), "checked": pf.get("checked")},
        })
    log["proposals"] = rows[-1000:]
    log["last_run"] = now.isoformat(timespec="seconds")
    try:
        path.write_text(json.dumps(log, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"[WARN] could not write {path.name}: {e}", file=sys.stderr)


# -------------------- Discord --------------------

def post_discord(embed):
    if not DISCORD_WEBHOOK:
        return False
    body = json.dumps({"embeds": [embed]}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK, data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "AutopilotWatcher-Executor/1.0"},
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


_STATUS_VERB = {
    "proposed": "PROPOSED",
    "proposed_live_unwired": "PROPOSED (live not wired)",
    "paper_filled": "PAPER FILL",
    "live_filled": "FILLED",
    "live_error": "ERROR",
}


def _size_label(notional, account_value, gr):
    """Render order size per share_size_display. 'pct' keeps a shared channel
    from leaking the account balance; 'usd' shows dollars; 'none' hides size."""
    mode = gr.get("share_size_display", "pct")
    if mode == "none":
        return ""
    if mode == "usd":
        return f"${notional:,.2f}"
    pct = (notional / account_value * 100) if account_value else 0.0
    return f"{pct:.1f}% acct"


def _ack_label(r):
    """One-line acknowledgment of the StockNews thesis + portfolio context that
    cleared this trade — rendered under each order so the grounding is visible."""
    th = r.get("thesis") or {}
    pf = r.get("portfolio") or {}
    a = r.get("assessment") or {}
    parts = []
    if th.get("found"):
        xii = th.get("xii_score")
        seg = f"📚 XII {xii}%" if xii is not None else "📚 thesis"
        if a.get("conviction"):
            seg += f" · conviction {a['conviction'].upper()}"
        if th.get("h0") is not None:
            seg += f" · H-0 {th['h0']}%"
        if th.get("durability") is not None:
            seg += f" · dur {th['durability']}/25"
        if a.get("asymmetry"):
            seg += f" · {a['asymmetry']} asym"
        if a.get("stale"):
            seg += " · ⏳stale"
        if th.get("fatal_flags"):
            seg += f" · {th['fatal_flags']}⚑"
        parts.append(seg)
    elif th:
        parts.append("📚 no StockNews thesis")
    if pf.get("checked"):
        parts.append("💼 " + ("already held" if pf.get("held") else "not held"))
    else:
        parts.append("💼 portfolio not checked")
    rs = r.get("research") or {}
    if rs.get("flags"):
        parts.append(f"🔬 ⚠️ {rs['flags'][0]}")
    # Latest material 8-K from the analyzed events feed — so a buy proposal
    # visibly knows about the earnings release / CFO exit it trades into.
    hot = next((e for e in (r.get("events_8k") or [])
                if e.get("materiality") in ("high", "critical")), None)
    if hot:
        codes = ",".join(c for c in hot.get("codes", []) if c)
        parts.append(f"📋 8-K {hot.get('filing_date')} "
                     f"{(hot.get('materiality') or '').upper()}"
                     + (f" [{codes}]" if codes else ""))
    return " · ".join(parts)


def build_embed(results, rejected, mode, account_value, gr, notes):
    executed = mode in ("paper", "live") and any(
        r["status"] in ("paper_filled", "live_filled") for r in results)
    color = {"propose": COLOR_PROPOSE, "paper": COLOR_PAPER,
             "live": COLOR_LIVE}.get(mode, COLOR_PROPOSE)
    if not results:
        color = COLOR_BLOCKED

    title = {
        "propose": "🧮 Execution proposals (nothing placed)",
        "paper": "📝 Paper-trade run",
        "live": "🟢 Live execution run" if executed else "🧮 Live run (degraded to proposals)",
    }.get(mode, "🧮 Execution proposals")

    lines = []
    for r in results:
        verb = _STATUS_VERB.get(r["status"], r["status"])
        feeds = "+".join(r.get("feeds", []))
        size = _size_label(r["notional_usd"], account_value, gr)
        size = f"{size} " if size else ""
        lines.append(f"**{verb}** {r['side'].upper()} `{r['ticker']}` "
                     f"{size}({r['order_type']}) "
                     f"· {r.get('feed_count', 0)} feeds [{feeds}]")
        ack = _ack_label(r)
        if ack:
            lines.append(f"   ↳ {ack}")
    if not lines:
        lines.append("_No proposals cleared the guardrails this run._")

    fields = [{
        "name": f"Orders ({len(results)})",
        "value": "\n".join(lines)[:1024], "inline": False,
    }]

    if rejected:
        rlines = [f"`{r['ticker'] or '?'}` — {r['reason']}" for r in rejected[:8]]
        if len(rejected) > 8:
            rlines.append(f"…and {len(rejected) - 8} more")
        fields.append({"name": f"Rejected ({len(rejected)})",
                       "value": "\n".join(rlines)[:1024], "inline": False})

    fields.append({
        "name": "Guardrails",
        "value": (f"size ≤ ${gr['max_notional_per_order_usd']:,.0f} / "
                  f"{gr['max_pct_account_per_order']:.0%} acct · "
                  f"≥{gr['min_signal_feeds']} feeds · "
                  f"{gr['max_orders_per_day']}/day · "
                  f"≤{gr['max_total_deployed_pct']:.0%} deployed · "
                  f"sides {gr['allowed_sides']}"),
        "inline": False,
    })

    desc = (f"Mode **{mode}** · account basis ${account_value:,.0f}"
            + ("\n" + "\n".join(notes) if notes else ""))
    if mode == "propose":
        desc += "\n_Propose-only: set `enabled:true` + `mode` in guardrails.json to act._"

    return {
        "title": title, "description": desc, "color": color, "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Autopilot Trading — guardrailed executor"},
    }


# -------------------- Signal gathering (reuses confluence) ------------------

def gather_signals(min_feeds, universe=None):
    """Compute ranked confluence signals the same way the Monday digest does,
    then extend them with the Finnhub market-wide feeds over `universe` (the
    allow-list) so non-SEC-watched industries (MU, CAT, …) can surface. Returns
    [] on any failure so the executor degrades to 'nothing to do' rather than
    crashing. Networked (SEC company_tickers + optional 13F + Finnhub)."""
    if not os.environ.get("SEC_USER_AGENT", "").strip():
        # confluence.py sys.exit()s at import if this is unset; bail with a clear
        # message instead of letting that abort the executor.
        print("[WARN] SEC_USER_AGENT not set; cannot gather signals.",
              file=sys.stderr)
        return []
    try:
        # confluence does a module-level sys.exit() guard; SystemExit is NOT an
        # Exception subclass, so catch it explicitly or it would kill the run.
        import confluence
    except (Exception, SystemExit) as e:
        print(f"[WARN] confluence import failed: {e}", file=sys.stderr)
        return []
    try:
        watchlist = confluence.load_json(confluence.WATCHLIST_PATH, {})
        sec_state = confluence.load_json(confluence.SEC_STATE_PATH, {})
        congress_state = confluence.load_json(confluence.CONGRESS_STATE_PATH, {})
        cik_to_ticker, name_to_ticker = confluence.fetch_company_tickers()
        for e in watchlist.get("sec_ciks", []):
            ticker = cik_to_ticker.get(str(e["cik"]).zfill(10))
            if ticker:
                name_to_ticker[confluence.normalize_name(e.get("name", ""))] = ticker
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=confluence.LOOKBACK_DAYS)
        crowded = confluence.load_crowded_names(watchlist) if confluence.INCLUDE_13F else []
        signals = confluence.collect_signals(
            sec_state.get("alert_history", []),
            congress_state.get("alert_history", []),
            name_to_ticker, cik_to_ticker, crowded=crowded, cutoff=cutoff,
        )
        # Extend with Finnhub's market-wide feeds (insider buys, analyst
        # upgrades, earnings beats) over the tradable universe. Set semantics
        # mean a Finnhub insider hit merges into the same 'insider' feed as an
        # SEC Form-4 hit (no double counting); 'analyst'/'earnings' are new.
        try:
            import finnhub_signals
            fh = finnhub_signals.gather(universe or [], cutoff)
            for tk, feeds in fh.items():
                rec = signals[tk]
                if not rec["issuer"]:
                    rec["issuer"] = tk
                for feed, n in feeds.items():
                    rec["feeds"].add(feed)
                    rec["counts"][feed] += n
        except Exception as e:
            print(f"[WARN] Finnhub feeds failed: {e}", file=sys.stderr)
        # Score with our own floor so the executor's min isn't capped by
        # confluence's display min.
        return confluence.score_confluence(signals, min_feeds=min(min_feeds, 2))
    except Exception as e:
        print(f"[WARN] signal gathering failed: {e}", file=sys.stderr)
        return []


def main():
    gr = load_guardrails()
    mode = decide_mode(gr)
    now = datetime.now(timezone.utc)
    state = load_json(STATE_PATH, {})

    account_value = gr["account_value_usd"]  # already sanitized to a float >= 0
    if mode == "live":
        try:
            import robinhood_mcp
            if robinhood_mcp.is_wired():
                account_value = float(robinhood_mcp.get_account()["account_value_usd"])
        except Exception as e:
            print(f"[WARN] could not read live account, using config: {e}",
                  file=sys.stderr)

    print(f"[START] executor — mode={mode} (config enabled={gr.get('enabled')}, "
          f"kill={KILL})  account=${account_value:,.0f}")
    print(f"        DRY_RUN={DRY_RUN}  Discord={'configured' if DISCORD_WEBHOOK else 'NOT SET'}")

    ranked = gather_signals(int(gr.get("min_signal_feeds", 3)),
                            universe=gr.get("allow_list", []))
    print(f"[SIGNALS] {len(ranked)} confluence candidates")

    approved, rejected = evaluate_proposals(ranked, gr, account_value, state, now)
    print(f"[GUARDRAILS] {len(approved)} approved, {len(rejected)} rejected")

    # Ground each surviving proposal in the StockNews thesis + existing book.
    import enrichment
    approved, ctx_rejected = gate_on_context(approved, gr, account_value,
                                             enrichment.enrich, today=now.date())
    rejected = rejected + ctx_rejected
    print(f"[CONTEXT] {len(approved)} pass thesis/conviction/portfolio, "
          f"{len(ctx_rejected)} dropped")

    results, notes = execute(approved, mode, now)
    for r in results:
        print(f"  -> {r['status']}: {r['side']} {r['ticker']} ${r['notional_usd']:.2f}")

    embed = build_embed(results, rejected, mode, account_value, gr, notes)

    if DRY_RUN:
        print(json.dumps(embed, indent=2))
        return 0

    # Only mutate state when we actually did something stateful (paper/live
    # fills) or recorded proposals worth keeping for the daily-cap count.
    if results:
        record_state(state, results)
        # Stamp the shareable, committed track record with entry prices so the
        # weekly scorecard can mark these to market.
        record_proposals_log(results, account_value, now,
                             max_anchor_ratio=float(
                                 gr.get("entry_price_anchor_max_ratio", 5.0)))

    ok = post_discord(embed) if DISCORD_WEBHOOK else True
    print(f"[DONE] mode={mode} posted={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    import producer_status
    rc = main()
    producer_status.record("executor", ok=(rc == 0))
    sys.exit(rc)
