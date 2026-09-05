# Local go-live prompt (paste into a local Claude Code session)

Written 2026-09-05 by the remote session that built PR #23. That session was
permission-gated on three things a local session can do: merge the PR, create
the Monday routine, and place the first orders via the Robinhood MCP.

Prerequisites on your machine: `git`, `python3`, the Robinhood Agentic MCP
connected to Claude Code (`claude mcp add robinhood-trading --transport http
https://agent.robinhood.com/mcp/trading`), and push access to
`zmzhong1/autopilot-trading`.

---

```
You are finishing the go-live of zmzhong1/autopilot-trading on the Robinhood Agentic
account. Read HANDOFF.md ("2026-09-05 — LIVE") and research/rules_review_2026-09-05.md
first; routines/live-execution.md is the runbook you will execute in step 4. Work in
this order and stop to ask me only where the steps say so.

STEP 1 — Merge the integration PR
  git fetch origin && git checkout claude/autopilot-robinhood-integration-p7wu75 && git pull
  python3 -m unittest discover -q -p 'test_*.py'      # expect 225 OK
  Merge PR #23 (https://github.com/zmzhong1/autopilot-trading/pull/23) into main with a
  squash merge (gh pr ready 23 && gh pr merge 23 --squash --delete-branch, or the GitHub
  UI). Then: git checkout main && git pull --ff-only origin main.
  Confirm guardrails.json on main has "enabled": true and "mode": "live", and that
  live_bridge.py exists. If either is false, stop and tell me.

STEP 2 — Create the Monday live routine
  Create a Claude Code routine (claude.ai/code/routines → New routine, or `/schedule` in
  this CLI) named "Autopilot · Monday live execution (Robinhood Agentic)":
    repo: zmzhong1/autopilot-trading, branch main, fresh session per fire
    schedule: 40 14 * * 1 (Mondays 14:40 UTC = 10:40 ET, after the 14:00 UTC CI
      proposal run and inside regular hours so dollar-based market orders fill at once)
    connectors: Robinhood Agent only
    prompt: the fenced "Routine prompt" block in routines/live-execution.md, verbatim
    notifications: push + email on completion
  Record the routine's trigger id in HANDOFF.md under "2026-09-05 — LIVE" (replace the
  "NOT YET CREATED" paragraph), commit as
  "docs: record live-execution routine id [skip ci]" and push to main.

STEP 3 — Pre-flight the live account (read-only)
  Via the Robinhood MCP: get_accounts (use the one agentic-allowed account; its number
  ends in 2732 — if it does not, stop and tell me), then get_portfolio,
  get_equity_positions, get_equity_orders (created_at_gte today), get_equity_quotes and
  get_equity_tradability for AAPL and MSFT. Save each raw result verbatim to
  /tmp/rh/{portfolio,positions,orders,quotes,tradability}.json and run:
    python3 live_bridge.py snapshot --portfolio /tmp/rh/portfolio.json \
      --positions /tmp/rh/positions.json --orders /tmp/rh/orders.json \
      --quotes /tmp/rh/quotes.json --tradability /tmp/rh/tradability.json \
      --account-last4 2732
    python3 live_bridge.py pending
  Expected today: $500 cash, no positions, and the two standing 2026-08-31 proposals
  (AAPL, MSFT) each cleared at $50.00 market / regular_hours. If `pending` shows
  something different, show me the full output and stop.

STEP 4 — First placement (needs my explicit yes)
  Show me the `pending --json` order list and, for each order, the review_equity_order
  preview (symbol, side, market, $50.00, estimated shares at the quote, any alerts,
  and the market_data_disclosure line verbatim). Then ASK ME to confirm. Only after I
  say yes, follow routines/live-execution.md steps 3–5 exactly: place_equity_order with
  the bridge's ref_id, `live_bridge.py record` immediately after each placement, wait
  ~2 minutes, get_equity_orders again → `live_bridge.py reconcile`, then commit
  live_orders.json + proposals_log.json + robinhood_snapshot.json to main as
  "chore(live): 2 order(s) placed 2026-09-05 [skip ci]" and push.
  Note: on a weekend the orders queue for Monday 09:30 ET open; they can be cancelled
  in the app until then. Never place anything the bridge did not list, never change a
  size, never place a sell, option or margin order.

STEP 5 — Report
  Five lines: PR merged (commit), routine id + next fire time, account value/cash
  before, orders placed (ticker, $, order id, state, fill or "queued for open"), and
  anything that refused or errored. Then update HANDOFF.md's "Next session checklist"
  so item 0 is done, commit and push.
```
