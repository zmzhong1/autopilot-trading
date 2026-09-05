# HANDOFF.md — autopilot-trading

System-state handoff for future Claude sessions and the owner. The repo
itself is the persistence layer — everything below is either committed here,
committed in the sister StockNews repo, or listed as an explicit owner action.
No work exists only in a chat session.

## 2026-09-05 — LIVE (read this first)

Owner direction this session: *"review the rules for autopilot, utilize what's
really given from StockNews, and start running the autopilot of Robinhood."*
Full review: `research/rules_review_2026-09-05.md`.

- **Posture is now `enabled: true`, `mode: "live"`.** CI still runs with
  `EXECUTOR_KILL=1` and only proposes. Real orders go out ONLY through
  `live_bridge.py` from a Claude session holding the Robinhood Agentic MCP
  (path 3 in `robinhood_mcp.py`), into account ••••2732 (`live_account_last4`).
- **Weekly live routine — CREATED 2026-09-05.**
  `trig_011pfWZKjL6SUGVkPjUCN8gf` · enabled · cron `40 14 * * 1` (UTC) ·
  **first fire 2026-09-07T14:40:00Z** · fresh session per fire
  (`persist_session: false`) · env `env_012TCX4zvbiGkQhaELFmC4is` · source
  `github.com/zmzhong1/autopilot-trading` · prompt = the fenced block in
  `routines/live-execution.md`. Console: https://claude.ai/code/routines
  Historical note: two earlier attempts failed — the remote go-live session's
  `create_trigger` was denied by its permission classifier, and the 2026-09-05
  local session could not reach `job_config.ccr.environment_id` (only obtainable
  from `/v1/environments` or the claude.ai UI; the RemoteTrigger tool exposes no
  environments action and the `claude` CLI has no environments subcommand). The
  owner created it in the UI.
- **Routine config audit (2026-09-05) — 3 findings, all FIXED.** As first
  created the routine was scheduled correctly but would have placed nothing.
  Fixed via `POST /v1/code/triggers/{id}` (owner-approved), verified in the
  response; `next_run_at`, `enabled`, `persist_session` and the prompt
  (event uuid `9fd37924-…`) were unchanged by the edit:
    1. `session_context.allowed_tools` had **no MCP tools**, so the
       `mcp__Robinhood-Agent__*` calls had no permission path in an unattended
       run. Added exactly the 8 the prompt uses — `get_accounts`,
       `get_portfolio`, `get_equity_positions`, `get_equity_orders`,
       `get_equity_quotes`, `get_equity_tradability`, `review_equity_order`,
       `place_equity_order`. Deliberately NOT added: any cancel, option, crypto
       or watchlist tool. **If you widen this list, widen it here and nowhere
       else** — it is the routine's real capability boundary, narrower than the
       connector's.
    2. `outcomes` pinned branch `claude/eloquent-feynman` with
       `autofix_on_pr_create: true`, while step 5 of the prompt pushes to `main`.
       A run that landed `live_orders.json` on a side branch would leave the next
       fire's daily-cap and cooldown reads (which read `main`) stale, so the
       cumulative caps would stop binding — same class as G1 in the rules review.
       Cleared to `outcomes: []`, `autofix_on_pr_create: false`.
    3. A second connector (`Context7`) was attached beyond the Robinhood-only
       spec. Removed; `mcp_connections` is now Robinhood-Agent alone.
  Notifications confirmed `push: true, email: true`.
- **Unattended smoke test PASSED 2026-09-05 14:15 UTC** (owner-approved,
  on-demand fire of the routine; session `cse_01USRVkhRAZNriCtRJpLTG2x`,
  `success`, 24 turns, 80s). AAPL+MSFT were temporarily block-listed
  (`90f84ca`) so `pending` emitted 0 orders and **no order could be placed by
  construction**; block_list reverted immediately after and verified
  byte-identical to its pre-test state. What the run proved end to end:
  sandbox allocation → clone of `main` → `live_bridge.py` + guardrails read →
  **Robinhood MCP reached with no permission prompt** (the fix for finding #1;
  the cloud session loaded the deferred tools via `ToolSearch` then called
  `get_accounts`, `get_portfolio`, `get_equity_positions`, `get_equity_orders`,
  `get_equity_tradability`, `get_equity_quotes`) → account ••••2732 matched
  `live_account_last4` → `$500.00 / 0 positions / 0 open orders` →
  `snapshot` → `pending` correctly skipped both names with
  `"in block_list (re-checked live)"` → committed and **pushed to `main`**
  (`4070470`, the fix for finding #2). Only `review_equity_order` /
  `place_equity_order` remain unexercised in an unattended run.
- **Latent bug found by the smoke test (step 5 of the routine prompt).**
  `git add live_orders.json proposals_log.json robinhood_snapshot.json` **fatals
  (exit 1) and stages nothing** when `live_orders.json` does not yet exist —
  which is exactly the state of any run that places 0 orders before the first
  ever placement. This is the same `git add` / `git status --porcelain`
  mismatch that "killed every run in PR #21" (see the comments in
  `.github/workflows/executor.yml`). The run self-recovered by retrying with the
  one file that existed, so it cost 2 turns, not the commit. **Fix when
  convenient** — in `routines/live-execution.md` *and* in the routine's own
  prompt (they must stay identical), use the guarded form already used in the
  workflows: `for f in live_orders.json proposals_log.json
  robinhood_snapshot.json; do [ -f "$f" ] && git add "$f"; done`.
  **FIXED 2026-09-05** in `routines/live-execution.md` and pushed to the live
  routine prompt in the same edit, so the two remain byte-identical. Verified in
  a scratch repo: the old form exits 128 and stages nothing when
  `live_orders.json` is absent; the loop exits 0 and stages what exists, in both
  the zero-order and all-three-present cases. `robinhood_snapshot.json` is kept
  LAST in the list because it always exists, so the loop's exit status is 0.
- **No order has been placed yet.** The bridge, config and routine prompt are
  complete; the first real placement is the first routine fire (or an
  owner-placed order). The two standing 08-31 proposals (AAPL, MSFT, $50 each)
  have now **both** previewed clean via `review_equity_order` — `order_checks: {}`,
  no broker alerts, verified 2026-09-05 06:2x UTC from the local session (the
  earlier AAPL classifier block did not recur).
- **2026-09-05 local session — what it did.** Merged the integration PR (#23,
  squash → `0242749`); 225 tests green on the branch and again on `main`;
  confirmed `enabled: true` / `mode: "live"` / `live_account_last4: "2732"` and
  `live_bridge.py` present on `main`. Read-only pre-flight of ••••2732 (the one
  `agentic_allowed` account, nickname "Agentic"): **$500.00 cash, $0 equity,
  0 positions, 0 orders**. `live_bridge.py snapshot` + `pending` emit exactly the
  two expected orders, 0 skipped:
    - AAPL $50.00 market/regular_hours `ref_id=10a1aea4-7821-51a0-948d-58cec06601c6`
    - MSFT $50.00 market/regular_hours `ref_id=0e55d0e1-9d9e-5dac-854a-a1c1ee0832a6`
  Both proposal 2026-08-31, 4 feeds, conviction medium, fatal_flags 0.
  **It did not place them:** placing a securities order is outside what that
  session's operating rules let it do, regardless of owner authorization. The
  placement is an owner action (Robinhood app / own session) or the routine's
  first fire. `ref_id`s above are deterministic per (day, ticker), so a later
  routine fire cannot double-place what the owner places by hand the same day.
- **New gates from StockNews** (executor.py `gate_on_context`): decision-journal
  action block (`skip/wait/avoid/sell/trim/exit`), `sovereign-impaired` block,
  and the T-Capex-5 regime gate (`ai-capex-high` → 4 feeds + medium cap).
- **New live-only guardrails**: per-name 25% cap on the *Agentic* book, 14-day
  re-buy cooldown, deploy cap from live equity, OTC ADRs blocked (no fractional
  orders), snapshot freshness, deterministic `ref_id`.
- **Committed live record**: `live_orders.json` (every real order + fill),
  `robinhood_snapshot.json` (last live read). `proposals_log.json` rows carry
  `live: {order_id, state}` and status `live_placed` → `live_filled`.
- **Stops, fastest first**: env `EXECUTOR_KILL=1` · `enabled: false` · empty
  `allow_list` · `block_list` · disable the routine.
- Tests: 225 passing on `main`, stdlib `unittest` (was 207 pre-branch).

### Next session checklist

0. ~~Merge the integration PR~~ ~~and create the routine~~ — **both done
   2026-09-05** (PR #23 squash-merged as `0242749`, branch deleted; routine
   `trig_011pfWZKjL6SUGVkPjUCN8gf`, its 3 config findings fixed and verified).
   Nothing is owed before the first fire.
1. Confirm the Monday routine fired and what it did (`live_orders.json`, the
   routine's report, `git log main`). First possible fire: 2026-09-07 14:40 UTC.
2. If the integration PR is still unmerged, the routine prompt checks out `main`
   and stops when `live_bridge.py` is absent — merge it, or point the routine at
   the branch.
3. `regime_gate.active` tracks StockNews T-Capex-5; flip it if the owner rejects
   the trigger (StockNews HANDOFF decision #2).

---

## 2026-07-03 handoff (historical, still accurate for the watchers)

## What this system is (30-second refresher)

A $0/month alert + propose-only trading pipeline on GitHub Actions cron:
SEC EDGAR + Congress watchers → Discord alerts → weekly confluence signals →
`executor.py` vets them against `guardrails.json` + the StockNews research
trees → PROPOSED orders (never live unless the owner flips `enabled`).
Sister repos: **StockNews** (research trees this repo reads),
**stock-portfolio** (holdings app, optional concentration check).

## State as of 2026-07-03

- **All 6 crons green.** SEC watcher (15-min weekdays), Congress watcher
  (hourly weekdays), daily research, cluster buys (weekday evenings),
  heartbeat + executor + digests (Mondays). Verified via Actions history and
  `producer_status.json` (all `ok: true`; heartbeat now also tracks
  `cluster_buys`).
- **8-K deep analysis is live** (PR #20, hotfix #21). Every 8-K alert now
  fetches the filing + EX-99 press-release exhibit and produces per-item
  summaries, financial highlights, personnel changes, and a materiality band
  (`critical/high/medium/low`). The analysis:
  - renders as a materiality-coloured Discord card;
  - is committed to `events/8k_events.jsonl` (rolling 500, deduped by
    accession) — **the bridge StockNews reads** via its
    `orchestration/autopilot_events.py`;
  - feeds `state.json` alert_history with `materiality`, so `confluence.py`
    ignores `low` (Reg FD decks) when counting the corporate feed;
  - surfaces on executor proposal cards (`📋 8-K <date> HIGH [2.02]`).
  The feed populates as 8-Ks arrive; an empty file until then is normal.
- **Trading rules tuned 2026-07-02** (owner-approved, see `guardrails.json`
  inline `_help` text for full rationale):
  - `require_conviction_feed: true` — ≥1 skin-in-the-game feed
    (congress / insider / material 8-K) required; analyst+earnings+13F alone
    no longer clears the bar.
  - Stale-thesis cap (`enrichment.STALE_HARD_CAP_DAYS = 30`) — a thesis
    >30 days past `review_due` caps conviction at LOW → blocked by
    `min_conviction: medium`. Mildly stale still caps high→medium.
  - `max_orders_per_day: 2` (was 5) — time diversification at $50/order.
  - `BYD` removed from the allow-list — on US exchanges that symbol is
    **Boyd Gaming**, not BYD Company (1211.HK) which the StockNews thesis
    covers. Re-add as BYDDY only with a `reports/BYDDY/` thesis.
- **Execution posture unchanged:** `enabled: false`, `mode: "propose"`,
  `EXECUTOR_KILL=1` in CI. The Robinhood Agentic account (••••2732) holds
  $500 cash, zero positions, nothing ever placed.

## What happens without any Claude session (the system self-runs)

| When | What | Where to see it |
|---|---|---|
| Every 15 min (weekdays) | SEC watcher alerts + 8-K analyses | Discord + `events/8k_events.jsonl` |
| Hourly (weekdays) | Congress trades | Discord |
| Weekday mornings | Deterministic company research | `research/*.json` + Discord |
| Weekday evenings | Insider cluster buys | Discord |
| Monday 13:00 UTC | Heartbeat + discovery + crowding + regime + confluence + StockNews digest | Discord |
| Monday 14:00 UTC | Executor proposes (never places) + scorecard | Discord + `proposals_log.json` |

The heartbeat flags any producer that silently stops. A failed workflow shows
red in the Actions tab; the 2026-07-02 lesson is that the *commit step* can
fail while the watcher succeeded — read the failing step, not just the badge.

## Owner runbook — going live (when/if you choose)

1. Fund/verify the Agentic account; confirm `account_value_usd` matches.
2. Set `"enabled": true` and `"mode": "paper"` in `guardrails.json` first;
   watch 2–3 Monday cycles of simulated fills on the scorecard.
3. Only then `"mode": "live"` — and only from an interactive session with the
   Robinhood MCP wired (`robinhood_mcp.py`); CI can never trade
   (`EXECUTOR_KILL=1` stays in `executor.yml`).
4. Kill switches, fastest first: env `EXECUTOR_KILL=1` · `enabled: false` ·
   empty `allow_list` (fail-closed) · `block_list` per ticker.

## Open items (owner or next session)

1. **Set `STOCK_PORTFOLIO_URL` + `STOCK_PORTFOLIO_TOKEN` secrets** on
   `executor.yml` — until then every proposal logs
   `portfolio.checked: false` and the 25%-concentration gate never fires.
2. **Optional LLM research layer is not running** — 0/29 `research/*.json`
   files have a `claude_note`. Enable per `routines/daily-research.md`
   (claude.ai routines UI, owner-gated) or accept the script-only digest.
3. **Refresh stale StockNews trees** — NVDA/HOOD (review_due 2026-06-09),
   AAPL/PLTR (06-15), TCEHY (06-30). NVDA/HOOD cross the 30-day hard block
   around **2026-07-09** and become unbuyable until refreshed. StockNews
   Routine A now bumps `review_due` on every review (prompt re-pasted by
   owner 2026-07-02), so this should self-heal — verify after Monday.
4. **14 allow-list names have no StockNews thesis yet** (GS, MS, TM, HMC,
   SONY, FCX, NEM, SHEL, STLD, + JPM in progress, …) — harmless (thesis gate
   fail-closes) and self-healing: they're queued in StockNews `_watchlist.json`
   at ~2 trees/week.
5. **Verify the first analyzed 8-K end-to-end** when one lands: Discord card
   shows materiality + key figures → row appears in `events/8k_events.jsonl`
   with the right ticker → `python3 -m orchestration.autopilot_events {T}`
   in StockNews returns it.

## Session log (2026-07-02 → 03)

- PR #20 (merged): 8-K deep analysis + events feed + confluence materiality
  filter + proposal-card 8-K context + BYD fix + cluster_buys heartbeat
  coverage + rules tuning. 181 tests passing.
- PR #21 (merged, hotfix): sec-watcher commit step died on the not-yet-
  existing `events/` dir (`git add` fatals on a missing pathspec while
  `git status --porcelain` tolerates it; `state.json` is dirty every run, so
  every 15-min run failed). Fixed with `events/.gitkeep` + independent
  staging behind a `[ -d events ]` guard. Verified green (run 28636107016)
  and state commits are flowing again.
- StockNews side: PRs #400/#402/#403 (merged) — events-feed reader wired
  into stage-6 thesis-update, dynamic CIK resolution for Stage 0, routine
  tuning (pre-IPO weekly + backlog drained 190→3, review_due staleness
  clock, Routine A prompt fix).
