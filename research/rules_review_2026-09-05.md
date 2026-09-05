# Autopilot rules review — 2026-09-05 (go-live session)

*Written by the Claude session that took the Agentic account live on the owner's
direction ("review the rules for autopilot, utilize what's really given from
StockNews, and start running the autopilot of Robinhood"). Every claim below was
checked against live tool reads this session (Robinhood MCP, both repos' state
files, the StockNews INDEX_META / decisions.jsonl corpus). Framework reads only —
this is a record of what the system does and why, not investment advice.*

---

## 1. Verified state before changes

| Item | Reading |
|---|---|
| Agentic account ••••2732 | $500.00 cash · $0 equity · 0 positions · 0 orders ever · buying power $500 |
| Pipeline | all 9 producers `ok: true`; executor last ran Mon 2026-08-31 |
| Proposal history | 18 rows over 9 Mondays (06-29 → 08-31): MSFT ×8 · NVDA ×5 · AAPL ×4 · MU ×1 |
| Standing proposals (08-31) | AAPL (4 feeds: analyst+congress+insider+institutional, XII 85, conv medium) · MSFT (same 4 feeds, XII 90, conv medium) |
| Shadow record (brief of 08-12) | +7.2% on deployed as proposed; +15.9% excluding the pre-tuning MU miss |
| Posture | `enabled: false` · `mode: propose` · `EXECUTOR_KILL=1` in CI · buy-only · $50/order · 2/day · 70% deploy cap |
| Tests | 181 passing |

## 2. Rule-by-rule review

### Kept as-is (working as designed)

- **`allow_list` fail-closed + `allowed_sides: ["buy"]`** — the two blast-radius limits. Accumulate-only stays: selling on thesis falsification is a different policy (see §5).
- **`min_signal_feeds: 3` + `require_conviction_feed: true`** — the 2026-07-02 tuning that would have blocked the only losing shadow trade (MU, −19%, three observational feeds). Both standing proposals carry congress + insider.
- **`require_stocknews_thesis` / `max_fatal_flags: 0` / `min_xii_score: 45` / `min_conviction: medium`** — the research gate. Conviction is computed from the full thesis (XII + H-0 + durability + asymmetry + freshness), which is why COST (XII 91, H-0 60, bear>bull) never proposes.
- **Stale-thesis cap** (`STALE_HARD_CAP_DAYS = 30`) — 8 trees are past `review_due` today (ASML, BYD, CRWD, GOOGL, HOOD, NVDA, PATH, SIVE), all < 30 days, so they cap high→medium rather than block. StockNews Routine A self-heals this.
- **`max_orders_per_day: 2` / `max_total_deployed_pct: 0.70` / 10%-per-order sizing** — $50/order, $350 max deployed, ≥4 Mondays to deploy. Right scale for a $500 pilot.
- **CI hard stop** (`EXECUTOR_KILL=1` in `executor.yml`) — unchanged, deliberately. CI proposes; it can never place.

### Gaps found and fixed this session

| # | Gap | Fix |
|---|---|---|
| G1 | **Deployed-capital and daily caps read `executor_state.json`, which is gitignored** — in CI the file never exists, so in any non-propose mode both caps would silently reset every run. | `live_bridge.py` computes deployed = live equity + open buy orders, and the daily cap from the committed `live_orders.json`. The real book is the state. |
| G2 | **The 25% per-name concentration gate has never fired** — it reads the Stock-Portfolio app, whose secrets were never set (HANDOFF item #1, open since 07-03). | The bridge applies `max_existing_position_pct` against the *Agentic account's own* positions + open orders (the book actually being traded). No secrets needed. The Stock-Portfolio read stays optional. |
| G3 | **Same name re-proposed every Monday** (AAPL/MSFT: 4 consecutive weeks) → in live mode the account would concentrate into 2 names within a month. | `rebuy_cooldown_days: 14` + the 25% cap ($125/name at $500). |
| G4 | **Executor is regime-blind** — it reads h0/xii/staleness but ignores `cycle_exposure`; StockNews' T-Capex-5 (*market de-rates capex increases*) fired 2026-07-28 and is still lit. This was R3 in the 08-12 brief. | `regime_gate` in guardrails: `ai-capex-high` names (13 trees: NVDA, TSM, AMD, AVGO, MU …) need 4 feeds instead of 3 and are capped at medium conviction while active. Set `active: false` when StockNews retires the trigger. |
| G5 | **Decision journal unused** — StockNews keeps `reports/{T}/decisions.jsonl` with the research's own latest call (`buy/watch/hold/wait/skip`), and the executor ignored it. | `block_decision_actions`: `skip` / `wait` / `avoid` / `sell` / `trim` / `exit` block a buy (TOTDY `wait`, MP `skip` today). `watch` — 47 of 52 journals, the corpus default for "0% position under observation" — does **not** block; acting on confluence for watch-rated ownable names is the executor's whole job. |
| G6 | **Sovereign band unused** — schema v6 `sovereign_exposure` exists (4 trees stamped so far). | `block_sovereign_bands: ["sovereign-impaired"]` (BABA / TCEHY bucket per StockNews). Other bands pass — they are sizing inputs, not gates. |
| G7 | **OTC ADRs are un-executable as dollar orders** — Robinhood `get_equity_tradability` this session: AJNMY, TOTDY, SFTBY, TCEHY `fractional_tradability: untradable`. A $50 market order for AJNMY (~$33) would fail or, worse, be re-sized. | The four go on `block_list`; the bridge also skips any symbol whose live tradability read says not fractional. Re-enable per name once a limit-order path exists. |
| G8 | **Live path unspecified** — `robinhood_mcp.py` was a stub; the README's "going live" section said "place proposals by hand". | Path 3: `live_bridge.py` (pure, 26 tests) + `routines/live-execution.md` (Monday 14:40 UTC routine holding the Robinhood MCP). Snapshot freshness (≤120 min), `live_account_last4` check, deterministic `ref_id` per (day, ticker) so a retried run can never double-place. |

### Rules I chose NOT to add (and why)

- **Earnings blackout** (skip buys within N days of `_pipeline_state.next_known_event`): the 8-K feed already surfaces earnings on the card, and $50 into a name the research rates ownable is not an earnings bet. Cheap to add later as `earnings_blackout_days`.
- **Selling on fatal flag / thesis falsification**: the owner's stated posture is accumulate-only until the loop is trusted. When wanted, the trigger already exists in the trees (`fatal_flags ≥ 1`, or an `✗` verdict flip on a load-bearing leaf) — it would be a separate `sell_rules` block, never inferred from "signal absent".
- **Valuation gate** (e.g. price vs anchor): the executor is feed-gated by design; the StockNews anchor is not a target. `entry_price_anchor_max_ratio` stays a fat-finger check only.
- **Lowering `min_signal_feeds` to 2** to trade more: the shadow record is good *because* the bar is high. Trade count is not the objective.

## 3. What StockNews actually feeds the executor now

| StockNews artefact | Field(s) read | Used for |
|---|---|---|
| `reports/{T}/tree_v1_en.md` INDEX_META | `xii_score`, `fatal_flags`, `h0`, `prob`, `durability`, `review_due`, `price`, `archetype_category`, `mispricing_source` | thesis gate + conviction (unchanged) |
| same, **new** | `cycle_exposure` | regime gate (G4) |
| same, **new** | `sovereign_exposure` | sovereign block (G6) |
| `reports/{T}/decisions.jsonl` **new** | latest `action`, `date`, `size_reason` | decision-journal block (G5); acknowledged on the card (`📓 journal watch (2026-05-04)`) |
| `events/8k_events.jsonl` (autopilot's own feed, consumed by StockNews) | materiality | card context (unchanged) |

Contract for StockNews authors (mirrored in StockNews `CLAUDE.md`): keep the four
field names and the band vocabularies stable; free text after a band is tolerated
(the parser takes the leading token), a renamed field silently becomes "absent".

## 4. What was placed at go-live

**Nothing, yet.** The session that built this was denied by its permission
classifier on the two calls that would have started the loop: `create_trigger`
(the Monday routine) and one of the two `review_equity_order` previews. The MSFT
preview that did run came back with no broker alerts (`order_checks: {}`) at
$499.41 last, i.e. ~0.1 share for $50. Per the bridge's own rules the two standing
08-31 proposals (AAPL, MSFT) would both clear today: allow-listed, `watch` journal,
no cycle band (AAPL) / mid-s-curve (MSFT), conviction medium, $50 = 10% of the
$500 live read, 0 positions, 0 open orders, both fractional-tradable.

First real placement = first routine fire after the owner creates the routine
(`routines/live-execution.md`) and merges the integration PR, or an
owner-confirmed interactive run. `live_orders.json` is created by that first run.

## 5. Open items after this session

1. **Watch the first routine fire** — Monday 2026-09-07 14:40 UTC. Expected: AAPL/MSFT
   skipped on cooldown; anything new the CI run proposes is vetted live.
2. Stock-Portfolio secrets (HANDOFF #1) — now optional: the concentration gate binds
   on the Agentic book. Still useful for the *household* correlated-exposure view.
3. Limit-order path for OTC ADRs if AJNMY ever needs to be buyable here.
4. Sell rules — owner decision, not before a few Monday cycles of live fills.
5. StockNews decision #1/#2 (IPS §5 "T-Capex 1-5", ratify T-Capex-5) — the regime
   gate here assumes T-Capex-5 stands; flip `regime_gate.active` if it is rejected.
