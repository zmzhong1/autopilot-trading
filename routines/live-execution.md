# Live execution — Claude cloud routine (Robinhood Agentic MCP)

This is the routine that actually places the orders the pipeline proposes.
It is the **only** path from a proposal to real money, and it runs in a Claude
Code session that holds the Robinhood Agentic MCP connection — never in
GitHub Actions (CI keeps `EXECUTOR_KILL=1`).

Created 2026-09-05 per owner direction ("start running the autopilot").
Routine id and schedule are recorded in `HANDOFF.md`.

## How it fits

| When (UTC, Mondays) | What | Runs where |
|---|---|---|
| 13:00 | heartbeat + feeds refresh | GitHub Actions |
| 14:00 | `executor.py` **proposes** → commits `proposals_log.json` | GitHub Actions (kill switch on) |
| **14:40** | **this routine** re-vets today's proposals against the live account and places what clears | Claude routine + Robinhood MCP |

The Python side stays a pure, tested vetting engine (`live_bridge.py`); the
routine does only what the MCP can do — read the account, place, read fills —
and feeds every result back through the CLI so the committed files
(`live_orders.json`, `proposals_log.json`, `robinhood_snapshot.json`) are the
single source of truth.

## Guardrails that bind at placement (re-checked live, every run)

`guardrails.json`: `enabled` + `mode: live` · allow/block lists · $-size = min(
`max_notional_per_order_usd`, `max_pct_account_per_order` × **live** account
value) · `max_orders_per_day` (from `live_orders.json`) · `max_total_deployed_pct`
(from live equity + open buy orders) · `max_existing_position_pct` per name (live
positions + open orders) · `rebuy_cooldown_days` · fractional tradability ·
`live_account_last4` · snapshot freshness · proposal freshness.

Stops, fastest first: `EXECUTOR_KILL=1` in the env · `enabled: false` · empty
`allow_list` · `block_list` per ticker · disable the routine.

## Routine prompt

```
You are the live-execution operator for zmzhong1/autopilot-trading. You hold the
Robinhood Agentic MCP. You place ONLY what live_bridge.py emits, ONLY into the
Robinhood account whose number ends in the guardrails' live_account_last4, and you
never place options, margin, sells, or anything not on the bridge's list.

0. cd into the autopilot-trading checkout. `git fetch origin main && git checkout main
   && git pull --ff-only origin main`. If live_bridge.py is missing on main, stop and
   report (the integration PR is not merged yet).
   Confirm guardrails.json has enabled=true and mode="live"; if not, stop and report.

1. Read the live account via the Robinhood MCP (the one agentic-allowed account):
   get_accounts -> note the agentic account number; then get_portfolio,
   get_equity_positions, get_equity_orders (created_at_gte = today) for it, and
   get_equity_tradability + get_equity_quotes for the tickers in today's proposals
   (proposals_log.json rows dated today or within max_proposal_age_days).
   Save each raw tool result verbatim as JSON files under /tmp/rh/ and run:
     python3 live_bridge.py snapshot --portfolio /tmp/rh/portfolio.json \
       --positions /tmp/rh/positions.json --orders /tmp/rh/orders.json \
       --quotes /tmp/rh/quotes.json --tradability /tmp/rh/tradability.json \
       --account-last4 <last 4 of the agentic account number>

2. `python3 live_bridge.py pending --json` -> the orders to place. If the list is
   empty, go to step 5.

3. For EACH order, in the listed order:
   a. review_equity_order(account_number=<agentic>, symbol=ticker, side="buy",
      type="market", dollar_amount=<dollar_amount>, market_hours="regular_hours",
      time_in_force="gfd"). If the review reports a blocking alert (insufficient
      buying power, halted instrument, PDT, not tradable), skip this order and note why.
   b. place_equity_order with the SAME parameters plus ref_id=<the bridge's ref_id>.
      Re-send the same ref_id on a transport retry; never invent a new one.
   c. Record immediately:
      python3 live_bridge.py record --ticker <T> --notional <dollar_amount> \
        --order-id <id from the response> --ref-id <ref_id> --state <state from the
        response> --proposal-date <proposal_date>
   Never place an order the bridge did not list, never change the size, never place
   twice for one ticker in a day.

4. Wait ~2 minutes, then get_equity_orders (created_at_gte = today) again, save it to
   /tmp/rh/orders_after.json, and run:
     python3 live_bridge.py reconcile --orders /tmp/rh/orders_after.json

5. Commit + push the record (even when nothing was placed — the snapshot is the
   liveness proof):
     for f in live_orders.json proposals_log.json robinhood_snapshot.json; do [ -f "$f" ] && git add "$f"; done
     git commit -m "chore(live): <N> order(s) placed <date> [skip ci]"
     git pull --rebase --autostash origin main && git push origin main
   Stage with the loop, not a bare `git add a b c`: git FATALS on a missing
   pathspec and stages NOTHING, and live_orders.json does not exist until the
   first placement — so a zero-order run would stage nothing and fail to commit.
   Keep robinhood_snapshot.json LAST in that list; it is always present, so the
   loop always exits 0.
   If a rebase conflicts on proposals_log.json, keep BOTH sides' rows (append) and
   continue.

6. Report in 5 lines: account value / cash before; orders placed (ticker, $, order
   id, state, fill price); orders skipped and why; any MCP or guardrail refusal;
   the commit hash. If anything in steps 1-4 errored, say exactly what, and do not
   retry a placement whose state is unknown — reconcile first.
```

## Setup notes

- **Connector:** the routine must be created with the Robinhood Agent connector
  attached (it is passed explicitly at creation — it is the only connector the
  routine needs).
- **Repository:** `zmzhong1/autopilot-trading`, default branch `main`.
- **Schedule:** `40 14 * * 1` (Mondays 14:40 UTC = 10:40 ET, after the CI proposal
  run at 14:00 UTC and inside regular hours so dollar-based market orders fill
  immediately).
- **Fresh session per fire** — the prompt is self-contained.
- To pause: disable the routine, or set `enabled: false` in `guardrails.json`
  (the bridge refuses to emit orders either way).
