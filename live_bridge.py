#!/usr/bin/env python3
"""Live-execution bridge — the only path that turns a proposal into a real
Robinhood order, and it can only be driven from a session that holds the
Robinhood Agentic MCP connection (a Claude Code session / routine).

Why a bridge and not an in-process client
-----------------------------------------
robinhood_mcp.py explains the two classic paths: (1) a human places proposals
by hand, (2) a headless OAuth MCP client. This module is path (3): the Python
side stays a *pure, tested vetting engine* and the MCP-holding agent does the
three things only it can do — read the live account, place the order, read the
fill — feeding each result back through this CLI so the committed track record
(`live_orders.json`, `proposals_log.json`) is the single source of truth.

Flow (see routines/live-execution.md for the routine prompt that drives it):

    1. CI executor (Mondays, EXECUTOR_KILL=1) proposes -> proposals_log.json
    2. Agent reads the Agentic account via MCP (portfolio, positions, orders,
       quotes, tradability) and stores it:
           python3 live_bridge.py snapshot --portfolio p.json --positions q.json \\
               --orders o.json [--quotes qq.json] [--tradability t.json]
    3. Agent asks what is placeable *right now* against that snapshot:
           python3 live_bridge.py pending            # human-readable
           python3 live_bridge.py pending --json     # machine-readable orders
       Every guardrail is re-checked against the LIVE book (not the config
       basis): allow/block lists, buying power, per-name concentration on the
       agentic account, total deployed, daily cap, re-buy cooldown, fractional
       tradability, proposal freshness, and the kill switches.
    4. Agent places each order via the MCP (review_equity_order, then
       place_equity_order with the bridge's ref_id) and records it:
           python3 live_bridge.py record --ticker AAPL --notional 50 \\
               --order-id <uuid> --ref-id <uuid> --state placed
    5. After fills are visible:
           python3 live_bridge.py reconcile --orders o2.json
    6. Agent commits live_orders.json + proposals_log.json + the snapshot.

Nothing here talks to the network. Every decision function is pure and unit
tested (test_live_bridge.py). Stdlib-only.
"""

import argparse
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

ROOT = Path(__file__).parent
GUARDRAILS_PATH = ROOT / "guardrails.json"
PROPOSALS_LOG_PATH = ROOT / "proposals_log.json"
LIVE_ORDERS_PATH = ROOT / "live_orders.json"        # committed: real orders
SNAPSHOT_PATH = ROOT / "robinhood_snapshot.json"    # committed: last live read

KILL = os.environ.get("EXECUTOR_KILL", "").lower() in ("1", "true", "yes")

# Deterministic idempotency namespace: the same (day, ticker, slot) always maps
# to the same ref_id, so a retried/re-run routine can never double-place.
REF_NS = uuid.UUID("6f1c2c3e-5b7a-4d1e-9a3b-2c4d5e6f7a8b")

# Robinhood order states that still consume buying power / count as an order.
OPEN_STATES = ("new", "queued", "confirmed", "unconfirmed", "partially_filled")
DEAD_STATES = ("cancelled", "rejected", "failed", "voided")
FILLED_STATES = ("filled",)

# Proposal statuses the bridge is allowed to promote to a live order.
PLACEABLE_STATUSES = ("proposed", "proposed_live_unwired")

_CONVICTION_RANK = {"none": 0, "avoid": 0, "low": 1, "medium": 2, "high": 3}


# -------------------- helpers --------------------

def load_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _parse_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _parse_date(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def ref_id_for(day, ticker, slot=0):
    """Stable per-(day, ticker, slot) idempotency key. Pure."""
    return str(uuid.uuid5(REF_NS, f"{day}:{ticker.upper()}:{slot}"))


# -------------------- snapshot (normalise raw MCP payloads) --------------------

def _unwrap(payload):
    """MCP tool results arrive as {"data": {...}} (sometimes already unwrapped)."""
    if isinstance(payload, dict) and "data" in payload and isinstance(payload["data"], dict):
        return payload["data"]
    return payload if isinstance(payload, dict) else {}


def build_snapshot(portfolio, positions=None, orders=None, quotes=None,
                   tradability=None, account_last4=None, now=None):
    """Normalise raw Robinhood MCP payloads into the compact snapshot the
    vetting logic reads. Pure. Unknown/missing fields degrade to zero/empty."""
    now = now or datetime.now(timezone.utc)
    pf = _unwrap(portfolio)
    bp = pf.get("buying_power") or {}
    if isinstance(bp, dict):
        bp = bp.get("buying_power")
    snap = {
        "ts": now.isoformat(timespec="seconds"),
        "account_last4": account_last4,
        "total_value": _f(pf.get("total_value")),
        "equity_value": _f(pf.get("equity_value")),
        "cash": _f(pf.get("cash")),
        "buying_power": _f(bp, _f(pf.get("cash"))),
        "positions": [],
        "open_orders": [],
        "quotes": {},
        "fractional_ok": {},
    }
    # quotes: {SYM: last}
    q = _unwrap(quotes) if quotes else {}
    for r in q.get("results", []) or []:
        qq = (r or {}).get("quote") or {}
        sym = (qq.get("symbol") or "").upper()
        last = _f(qq.get("last_trade_price"), 0.0)
        if sym and last > 0:
            snap["quotes"][sym] = last
    # positions: symbol, quantity, average_buy_price, market value (quote×qty)
    pz = _unwrap(positions) if positions else {}
    for pos in pz.get("positions", []) or []:
        sym = (pos.get("symbol") or pos.get("instrument_symbol") or "").upper()
        qty = _f(pos.get("quantity"))
        if not sym or qty <= 0:
            continue
        avg = _f(pos.get("average_buy_price"))
        px = snap["quotes"].get(sym) or avg
        snap["positions"].append({
            "symbol": sym, "quantity": qty, "average_buy_price": avg,
            "market_value": round(qty * px, 2),
        })
    # open orders: only live states, only buys matter for deploy/buying power
    oz = _unwrap(orders) if orders else {}
    for o in oz.get("orders", []) or []:
        state = (o.get("state") or "").lower()
        if state not in OPEN_STATES:
            continue
        sym = (o.get("symbol") or o.get("instrument_symbol") or "").upper()
        dba = o.get("dollar_based_amount")
        if isinstance(dba, dict):
            dba = dba.get("amount")
        notional = _f(dba)
        if notional <= 0:
            px = _f(o.get("price")) or snap["quotes"].get(sym, 0.0)
            notional = _f(o.get("quantity")) * px
        snap["open_orders"].append({
            "id": o.get("id"), "symbol": sym, "side": (o.get("side") or "").lower(),
            "state": state, "notional_usd": round(notional, 2),
            "created_at": o.get("created_at"),
        })
    # tradability: fractional / dollar-based eligibility per symbol
    tz = _unwrap(tradability) if tradability else {}
    for r in tz.get("results", []) or []:
        sym = (r.get("symbol") or "").upper()
        if not sym:
            continue
        ok = (r.get("tradeable", True) is not False
              and (r.get("state") or "active") == "active"
              and (r.get("fractional_tradability") or "tradable") == "tradable")
        snap["fractional_ok"][sym] = bool(ok)
    return snap


def snapshot_age_minutes(snap, now=None):
    ts = _parse_ts((snap or {}).get("ts"))
    if not ts:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - ts).total_seconds() / 60.0


# -------------------- the vetting core (pure) --------------------

def latest_proposals(rows, today, max_age_days):
    """Newest placeable proposal per ticker within the freshness window.
    Rows already promoted to a live order (carry a `live` block) are skipped."""
    cutoff = today - timedelta(days=max_age_days)
    best = {}
    for r in rows or []:
        if r.get("status") not in PLACEABLE_STATUSES:
            continue
        if r.get("live"):
            continue
        d = _parse_date(r.get("date"))
        if not d or d < cutoff or d > today:
            continue
        t = (r.get("ticker") or "").upper()
        if not t:
            continue
        if t not in best or d > _parse_date(best[t].get("date")):
            best[t] = r
    return sorted(best.values(), key=lambda r: (-int(r.get("feed_count") or 0),
                                                 r.get("ticker")))


def live_orders_for(live_orders, ticker=None, since=None):
    out = []
    for o in live_orders or []:
        if (o.get("state") or "").lower() in DEAD_STATES:
            continue
        if ticker and (o.get("ticker") or "").upper() != ticker.upper():
            continue
        if since:
            d = _parse_date(o.get("date"))
            if not d or d < since:
                continue
        out.append(o)
    return out


def select_live_orders(proposals, snapshot, live_orders, gr, today, now=None):
    """Pure: (orders_to_place, skipped, notes). Re-vets every fresh proposal
    against the LIVE account snapshot and the committed live order log.

    Each order: {ticker, side, dollar_amount, type, market_hours,
    time_in_force, ref_id, proposal_date, size_pct}. Each skip carries a
    `reason`. `notes` are card-level messages (why nothing was placed)."""
    import executor  # pure sizing helpers; no network at import

    notes = []
    skipped = []
    orders = []

    # ---- kill switches / posture -------------------------------------------
    if KILL:
        return [], [], ["EXECUTOR_KILL=1 — bridge refuses to emit orders"]
    if not gr.get("enabled"):
        return [], [], ["guardrails.enabled is false — propose-only"]
    if (gr.get("mode") or "").lower() != "live":
        return [], [], [f"guardrails.mode is '{gr.get('mode')}' — not live"]
    if not snapshot:
        return [], [], ["no robinhood_snapshot.json — run `snapshot` first"]
    age = snapshot_age_minutes(snapshot, now)
    max_age = float(gr.get("snapshot_max_age_min", 120))
    if age is None or age > max_age:
        return [], [], [f"snapshot is stale ({age and round(age)} min > {max_age:.0f}) — re-run `snapshot`"]
    want4 = str(gr.get("live_account_last4") or "")
    have4 = str(snapshot.get("account_last4") or "")
    if want4 and have4 and want4 != have4:
        return [], [], [f"snapshot account ••••{have4} != guardrails live_account_last4 ••••{want4}"]

    account_value = _f(snapshot.get("total_value"))
    if account_value <= 0:
        return [], [], ["account value is 0 — nothing to size against"]
    size = executor.size_order(account_value, gr)
    if size <= 0:
        return [], [], ["order size resolved to $0"]
    min_live = _f(gr.get("min_order_notional_usd"), 1.0)
    if size < min_live:
        return [], [], [f"order size ${size:.2f} below broker minimum ${min_live:.2f}"]

    allow = {t.upper() for t in gr.get("allow_list", [])}
    block = {t.upper() for t in gr.get("block_list", [])}
    sides = [s.lower() for s in gr.get("allowed_sides", ["buy"])]
    min_conv = _CONVICTION_RANK.get(gr.get("min_conviction", "medium"), 2)
    max_fatal = int(gr.get("max_fatal_flags", 0))
    max_pos_pct = _f(gr.get("max_existing_position_pct"), 100.0)
    deploy_cap = _f(gr.get("max_total_deployed_pct"), 0.0) * account_value
    day_cap = int(gr.get("max_orders_per_day", 0))
    cooldown = int(gr.get("rebuy_cooldown_days", 0))
    require_fractional = bool(gr.get("require_fractional_tradability", True))

    # ---- live book --------------------------------------------------------
    pos_value = {p["symbol"]: _f(p.get("market_value")) for p in snapshot.get("positions", [])}
    open_buys = [o for o in snapshot.get("open_orders", []) if o.get("side") == "buy"]
    pending_by_sym = {}
    for o in open_buys:
        pending_by_sym[o["symbol"]] = pending_by_sym.get(o["symbol"], 0.0) + _f(o.get("notional_usd"))
    deployed = _f(snapshot.get("equity_value")) + sum(pending_by_sym.values())
    buying_power = _f(snapshot.get("buying_power"))
    placed_today = len(live_orders_for(live_orders, since=today))
    slots = max(0, day_cap - placed_today)

    if not proposals:
        notes.append("no fresh placeable proposals")

    for p in proposals:
        t = (p.get("ticker") or "").upper()
        side = (p.get("side") or "buy").lower()
        sn = p.get("stocknews") or {}
        base = {"ticker": t, "side": side, "proposal_date": p.get("date"),
                "feed_count": p.get("feed_count"), "feeds": p.get("feeds", [])}

        def skip(reason):
            skipped.append(dict(base, reason=reason))

        if side not in sides:
            skip(f"side '{side}' not allowed"); continue
        if t not in allow:
            skip("not in allow_list (re-checked live)"); continue
        if t in block:
            skip("in block_list (re-checked live)"); continue
        if not sn.get("found", False):
            skip("proposal has no StockNews thesis attached"); continue
        if int(sn.get("fatal_flags") or 0) > max_fatal:
            skip(f"fatal flags {sn.get('fatal_flags')} > {max_fatal}"); continue
        if _CONVICTION_RANK.get(sn.get("conviction") or "none", 0) < min_conv:
            skip(f"conviction {sn.get('conviction')} < {gr.get('min_conviction')}"); continue
        if require_fractional and snapshot.get("fractional_ok") and \
                snapshot["fractional_ok"].get(t) is False:
            skip("not fractional/dollar tradable on Robinhood (OTC?)"); continue
        # idempotency: never two live orders for one name on one day
        if live_orders_for(live_orders, ticker=t, since=today):
            skip("already ordered today"); continue
        if pending_by_sym.get(t, 0.0) > 0:
            skip(f"open buy order already pending (${pending_by_sym[t]:.2f})"); continue
        if cooldown > 0:
            recent = live_orders_for(live_orders, ticker=t,
                                     since=today - timedelta(days=cooldown))
            if recent:
                skip(f"re-buy cooldown ({cooldown}d) — last {recent[-1].get('date')}"); continue
        held = pos_value.get(t, 0.0) + pending_by_sym.get(t, 0.0)
        if (held + size) / account_value * 100.0 > max_pos_pct:
            skip(f"would be {((held + size) / account_value * 100):.0f}% of acct "
                 f"(> {max_pos_pct:.0f}% cap; held ${held:.2f})"); continue
        if deployed + size > deploy_cap:
            skip(f"would exceed deploy cap ${deploy_cap:.0f} (deployed ${deployed:.2f})"); continue
        if buying_power < size:
            skip(f"buying power ${buying_power:.2f} < ${size:.2f}"); continue
        if len(orders) >= slots:
            skip(f"daily cap ({day_cap}/day, {placed_today} placed today)"); continue

        orders.append({
            "ticker": t, "side": side,
            "dollar_amount": f"{size:.2f}", "notional_usd": size,
            "type": "market", "market_hours": "regular_hours",
            "time_in_force": "gfd",
            "ref_id": ref_id_for(today.isoformat(), t, 0),
            "proposal_date": p.get("date"),
            "size_pct": round(size / account_value * 100.0, 3),
            "feeds": p.get("feeds", []), "feed_count": p.get("feed_count"),
            "stocknews": sn,
        })
        deployed += size
        buying_power -= size
        pending_by_sym[t] = pending_by_sym.get(t, 0.0) + size

    if not orders and not notes:
        notes.append("nothing cleared the live guardrails")
    return orders, skipped, notes


# -------------------- record / reconcile (committed track record) -----------

def record_order(live_orders, proposals_rows, order, order_id, ref_id, state,
                 now, account_last4=None, avg_price=None, quantity=None):
    """Append a live order to the log and stamp the matching proposal row(s).
    Idempotent on order_id. Returns (live_orders, proposals_rows, entry)."""
    for o in live_orders:
        if o.get("order_id") == order_id:
            o.update({"state": state,
                      "avg_price": avg_price if avg_price is not None else o.get("avg_price"),
                      "quantity": quantity if quantity is not None else o.get("quantity"),
                      "updated_ts": now.isoformat(timespec="seconds")})
            return live_orders, proposals_rows, o
    entry = {
        "ts": now.isoformat(timespec="seconds"),
        "date": now.date().isoformat(),
        "ticker": order["ticker"].upper(),
        "side": order.get("side", "buy"),
        "notional_usd": _f(order.get("notional_usd")),
        "order_type": order.get("type", "market"),
        "order_id": order_id,
        "ref_id": ref_id,
        "state": state,
        "avg_price": avg_price,
        "quantity": quantity,
        "account_last4": account_last4,
        "proposal_date": order.get("proposal_date"),
        "feeds": order.get("feeds", []),
        "stocknews": order.get("stocknews", {}),
        "placed_by": "claude-live-bridge",
    }
    live_orders.append(entry)
    for r in proposals_rows or []:
        if (r.get("ticker") or "").upper() == entry["ticker"] and \
                r.get("date") == order.get("proposal_date") and \
                r.get("status") in PLACEABLE_STATUSES:
            r["status"] = "live_placed"
            r["live"] = {"order_id": order_id, "state": state, "ts": entry["ts"]}
            if avg_price:
                r["entry_price"] = avg_price
    return live_orders, proposals_rows, entry


def reconcile(live_orders, proposals_rows, orders_payload, now):
    """Update live orders from a fresh MCP orders payload (by order id).
    Filled -> state 'filled' + avg_price/quantity; dead states propagate;
    proposals rows get status live_filled / live_cancelled. Returns count."""
    oz = _unwrap(orders_payload)
    by_id = {}
    for o in oz.get("orders", []) or []:
        if o.get("id"):
            by_id[o["id"]] = o
    n = 0
    for lo in live_orders:
        o = by_id.get(lo.get("order_id"))
        if not o:
            continue
        state = (o.get("state") or "").lower()
        if not state or state == lo.get("state"):
            continue
        lo["state"] = state
        lo["updated_ts"] = now.isoformat(timespec="seconds")
        if state in FILLED_STATES:
            lo["avg_price"] = _f(o.get("average_price")) or lo.get("avg_price")
            lo["quantity"] = _f(o.get("cumulative_quantity") or o.get("quantity")) or lo.get("quantity")
        n += 1
        for r in proposals_rows or []:
            if (r.get("live") or {}).get("order_id") == lo.get("order_id"):
                r["live"]["state"] = state
                if state in FILLED_STATES:
                    r["status"] = "live_filled"
                    if lo.get("avg_price"):
                        r["entry_price"] = lo["avg_price"]
                elif state in DEAD_STATES:
                    r["status"] = "live_cancelled"
    return n


# -------------------- CLI --------------------

def _load_guardrails():
    import executor
    return executor.load_guardrails(GUARDRAILS_PATH)


def cmd_snapshot(args):
    pf = load_json(args.portfolio, {})
    if not pf:
        print(f"[ERROR] could not read portfolio payload {args.portfolio}", file=sys.stderr)
        return 2
    snap = build_snapshot(
        pf,
        positions=load_json(args.positions, {}) if args.positions else None,
        orders=load_json(args.orders, {}) if args.orders else None,
        quotes=load_json(args.quotes, {}) if args.quotes else None,
        tradability=load_json(args.tradability, {}) if args.tradability else None,
        account_last4=args.account_last4,
    )
    save_json(SNAPSHOT_PATH, snap)
    print(f"[SNAPSHOT] ••••{snap['account_last4']} value ${snap['total_value']:,.2f} "
          f"cash ${snap['cash']:,.2f} bp ${snap['buying_power']:,.2f} "
          f"positions {len(snap['positions'])} open orders {len(snap['open_orders'])}")
    return 0


def cmd_pending(args):
    gr = _load_guardrails()
    now = datetime.now(timezone.utc)
    today = now.date()
    log = load_json(PROPOSALS_LOG_PATH, {"proposals": []})
    live = load_json(LIVE_ORDERS_PATH, {"orders": []}).get("orders", [])
    snap = load_json(SNAPSHOT_PATH, {})
    props = latest_proposals(log.get("proposals", []), today,
                             int(gr.get("max_proposal_age_days", 7)))
    orders, skipped, notes = select_live_orders(props, snap, live, gr, today, now)
    if args.json:
        print(json.dumps({"orders": orders, "skipped": skipped, "notes": notes,
                          "account_number_hint": f"••••{snap.get('account_last4')}"},
                         indent=2))
        return 0
    print(f"[PENDING] {len(orders)} order(s) to place · {len(skipped)} skipped")
    for n in notes:
        print(f"  note: {n}")
    for o in orders:
        print(f"  PLACE {o['side'].upper()} {o['ticker']} ${o['dollar_amount']} market "
              f"regular_hours ref_id={o['ref_id']} (proposal {o['proposal_date']}, "
              f"{o['feed_count']} feeds)")
    for s in skipped:
        print(f"  skip  {s['ticker']}: {s['reason']}")
    return 0


def cmd_record(args):
    now = datetime.now(timezone.utc)
    log = load_json(PROPOSALS_LOG_PATH, {"proposals": []})
    live_doc = load_json(LIVE_ORDERS_PATH, {"orders": []})
    snap = load_json(SNAPSHOT_PATH, {})
    order = {"ticker": args.ticker, "side": args.side, "notional_usd": args.notional,
             "type": "market", "proposal_date": args.proposal_date}
    # Carry the proposal's grounding into the live record when we can find it.
    for r in log.get("proposals", []):
        if (r.get("ticker") or "").upper() == args.ticker.upper() and \
                r.get("date") == args.proposal_date:
            order["feeds"] = r.get("feeds", [])
            order["stocknews"] = r.get("stocknews", {})
    live_doc["orders"], log["proposals"], entry = record_order(
        live_doc.get("orders", []), log.get("proposals", []), order,
        args.order_id, args.ref_id, args.state, now,
        account_last4=snap.get("account_last4"),
        avg_price=args.avg_price, quantity=args.quantity)
    live_doc["last_update"] = now.isoformat(timespec="seconds")
    save_json(LIVE_ORDERS_PATH, live_doc)
    save_json(PROPOSALS_LOG_PATH, log)
    print(f"[RECORD] {entry['side']} {entry['ticker']} ${entry['notional_usd']:.2f} "
          f"order {entry['order_id']} state={entry['state']}")
    return 0


def cmd_reconcile(args):
    now = datetime.now(timezone.utc)
    payload = load_json(args.orders, {})
    log = load_json(PROPOSALS_LOG_PATH, {"proposals": []})
    live_doc = load_json(LIVE_ORDERS_PATH, {"orders": []})
    n = reconcile(live_doc.get("orders", []), log.get("proposals", []), payload, now)
    live_doc["last_update"] = now.isoformat(timespec="seconds")
    save_json(LIVE_ORDERS_PATH, live_doc)
    save_json(PROPOSALS_LOG_PATH, log)
    print(f"[RECONCILE] {n} order(s) updated")
    return 0


def cmd_status(args):
    live = load_json(LIVE_ORDERS_PATH, {"orders": []}).get("orders", [])
    snap = load_json(SNAPSHOT_PATH, {})
    gr = _load_guardrails()
    print(f"posture: enabled={gr.get('enabled')} mode={gr.get('mode')} kill={KILL}")
    if snap:
        print(f"snapshot: {snap.get('ts')} ••••{snap.get('account_last4')} "
              f"value ${_f(snap.get('total_value')):,.2f} cash ${_f(snap.get('cash')):,.2f}")
    print(f"live orders on record: {len(live)}")
    for o in live[-10:]:
        print(f"  {o.get('date')} {o.get('side')} {o.get('ticker')} ${_f(o.get('notional_usd')):.2f} "
              f"{o.get('state')} @ {o.get('avg_price') or '—'} id={o.get('order_id')}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snapshot", help="store a live account read from raw MCP payload files")
    s.add_argument("--portfolio", required=True)
    s.add_argument("--positions")
    s.add_argument("--orders")
    s.add_argument("--quotes")
    s.add_argument("--tradability")
    s.add_argument("--account-last4", required=True)
    s.set_defaults(fn=cmd_snapshot)

    s = sub.add_parser("pending", help="orders placeable now against the snapshot")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_pending)

    s = sub.add_parser("record", help="record an order placed via the MCP")
    s.add_argument("--ticker", required=True)
    s.add_argument("--side", default="buy")
    s.add_argument("--notional", type=float, required=True)
    s.add_argument("--order-id", required=True)
    s.add_argument("--ref-id", required=True)
    s.add_argument("--state", default="placed")
    s.add_argument("--proposal-date")
    s.add_argument("--avg-price", type=float)
    s.add_argument("--quantity", type=float)
    s.set_defaults(fn=cmd_record)

    s = sub.add_parser("reconcile", help="update fills from a fresh MCP orders payload")
    s.add_argument("--orders", required=True)
    s.set_defaults(fn=cmd_reconcile)

    s = sub.add_parser("status", help="print posture + recent live orders")
    s.set_defaults(fn=cmd_status)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
