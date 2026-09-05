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
- **Weekly live routine** — `routines/live-execution.md`; intended schedule
  Mondays 14:40 UTC (after the 14:00 CI proposal run), fresh session per fire,
  Robinhood connector attached. **NOT YET CREATED** — the go-live session's
  `create_trigger` call was denied by the session's permission classifier, so
  the owner must create it (claude.ai/code/routines → New routine, paste the
  prompt from `routines/live-execution.md`, attach the Robinhood connector,
  cron `40 14 * * 1`). Record the trigger id here once it exists.
- **No order was placed this session.** The bridge, config and routine prompt
  are complete; the first real placement is the first routine fire (or an
  owner-confirmed interactive run). The two standing 08-31 proposals (AAPL,
  MSFT, $50 each) previewed clean on the MSFT leg via `review_equity_order`
  (no broker alerts); the AAPL preview was classifier-blocked.
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
- Tests: 207 → (this branch) all green, stdlib `unittest`.

### Next session checklist

0. **Owner:** create the routine (above) and merge the integration PR. Until
   both are done nothing is placed — the account stays $500 cash.
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
