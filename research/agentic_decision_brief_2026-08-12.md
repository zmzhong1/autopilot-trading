# Agentic Account Decision Brief — 2026-08-12

*Written by the Claude session that completed the StockNews hygiene sweep of 2026-08-12
(StockNews PRs #521/#522, issue backlog 34→5). Everything below was verified against live
tool reads this session — account via Robinhood MCP, pipeline via this repo's state files,
research via StockNews INDEX_META blocks. Framework reads only: this brief recommends
**system** actions and lays out **decision points**; it is not investment advice, and no
trade is executed by any Claude session in any mode.*

---

## 1. Verified state

**Account (Robinhood "Agentic", ••••2732, read 2026-08-12):**
$500.00 cash · $0 equity · zero positions · buying power $500.00 · nothing ever placed.

**Pipeline:** all 9 producers `ok: true` (executor last ran Mon 08-10 14:53 UTC; research
08-12). Posture: `enabled: false` · `mode: "propose"` · `EXECUTOR_KILL=1` in CI ·
buy-only · $50/order (10% × $500) · max 2 orders/day · 48-ticker allow-list.

**Proposal history:** 12 proposals over 6 Mondays (2026-06-29 → 2026-08-10).
MSFT ×5 · NVDA ×5 · MU ×1 · AAPL ×1. None executed.

## 2. The shadow record (what the loop would have done)

$50 at each ticker's **first** proposal, priced at 2026-08-12 session close:

| Ticker | First proposed | Entry | Now | Move | $50 → |
|---|---|---|---|---|---|
| MSFT | 06-29 (4 feeds) | $368.92 | $492.43 | **+33.5%** | +$16.74 |
| NVDA | 06-29 (3 feeds) | $193.70 | $224.15 | **+15.7%** | +$7.86 |
| MU | 06-29 (3 observational feeds) | $1,126.20 | $911.16 | **−19.1%** | −$9.55 |
| AAPL | 08-10 (4 feeds incl. congress) | $306.59 | $302.22 | −1.4% | −$0.71 |

- **As proposed:** +$14.34 on $200 deployed = **+7.2% on deployed capital** in ~6 weeks
  (the actual account: 0.0%).
- **Under current rules:** the MU proposal was the pre-tuning miss — analyst+earnings+13F
  only, zero skin-in-the-game feeds — and it is exactly what `require_conviction_feed: true`
  (added 2026-07-02) now blocks. Excluding it: **+$23.89 on $150 = +15.9% deployed.**
- The one bad call was identified *by the system's own tuning process* before it aged badly.
  That is the loop behaving the way the runbook hoped.

**Standing proposals (08-10):** MSFT (5 feeds, incl. congress — strongest proposal to date)
and AAPL (4 feeds, incl. congress). Both now trade **below** their proposal prices
(MSFT −3.5% vs $510.36; AAPL −1.4% vs $306.59).

## 3. What the research corpus says right now (framework reads, not advice)

| Ticker | h0 | xii | prob (bull/base/bear) | Anchor → now | Cycle band | Note |
|---|---|---|---|---|---|---|
| GOOGL | **87%** | 90% | 30/55/15 | $356.13 → $343.54 (−3.5%) | mid-s-curve | Highest-h0 large cap in the corpus; **never proposed** — its feeds haven't confluenced. The executor is feed-gated, not valuation-gated: working as designed, but worth knowing |
| MSFT | 65% | 90% | 32/45/23 | $499.86 → $492.43 | mid-s-curve | The system's most-proposed name; 08-10 proposal carried congress feed |
| CAT | 68% | 87% | **40/48/12** | $904.07 → $855.65 (−5.4%) | — | Freshly re-anchored 08-05 post-earnings; most favorable prob skew listed here |
| HOOD | 76% | 88% | 38/47/15 | $93.51 → $94.92 | ai-capex-low | Foundation-v2 leaves pass completed today (14/14 tiers, 0 lint) |
| DIS | 66% | 70% | 27/54/19 | $103.53 → $103.22 | uncorrelated | Streaming-margin leaf flipped ⚠️→✅ today on Q3 13% SVOD margin |
| AAPL | 67% | 85% | 30/45/25 | $308.91 → $302.22 | ai-capex-low | The other standing proposal |
| NVDA | 75% | 86% | — | now $224.15 | **ai-capex-high** | h0 was recomputed 82→75 on 08-03; +15.7% above the June proposal zone; see regime caveat |
| AJNMY | 79% | ~90% | — | $33.05 → $34.45 | ai-capex-high | Firmest catalyst path in the corpus (2 of 3 fired, disclosure pending) — but thin OTC ADR: after-hours book showed $33.28 bid / $46.13 ask. At $50 size, **limit orders only**, if ever |

**Regime caveat that cuts across all of it:** `T-Capex-5` fired 2026-07-28 — the market
de-rated GOOGL −7% for *raising* capex guidance. The StockNews macro overlay's read is that
ai-capex-high names carry regime de-rating risk that the trees' own h0s don't fully carry.
The IPS §5 amendment that would let the NVDA/TSM trim review fire is **still awaiting the
owner's decision** (StockNews HANDOFF decision #1).

## 4. Recommendations

**R1 — Flip to paper mode.** `guardrails.json`: `enabled: true`, `mode: "paper"`. This is
the runbook's own step 2, and six weeks of propose-only evidence is enough to justify it:
producers green, rules self-corrected once (MU → `require_conviction_feed`), StockNews
`review_due` discipline now solid (today's sweep: 0 overdue corpus-wide). Paper mode
simulates fills against virtual cash — **no real money in any code path** (CI keeps
`EXECUTOR_KILL=1`; buy-only; $50/order). Watch 2–3 Monday cycles on the scorecard.
*Needs the owner's explicit yes — it is a standing-config change. One-line diff, reversible
by kill switch at any time.*

**R2 — Set the portfolio secrets before any live consideration.** Every proposal to date
logs `portfolio.checked: false` — the 25%-concentration gate has never fired because
`STOCK_PORTFOLIO_URL` / `STOCK_PORTFOLIO_TOKEN` are unset on `executor.yml` (HANDOFF open
item #1, open since 07-03). ~5 minutes in GitHub repo settings; owner-only (secrets).

**R3 — Optional: wire the regime overlay into the executor.** The executor currently reads
h0/xii/review-staleness from StockNews but ignores `cycle_exposure` / `sovereign_exposure`.
A small change: while T-Capex-5 regime holds, require one extra feed (or cap conviction at
medium) for `ai-capex-high` names. Turns the macro overlay from commentary into a gate.
Claude can implement this on request; it only affects proposals.

**R4 — Live mode stays off.** Runbook step 3 conditions aren't met (needs R1 cycles + R2).
Separately and permanently: no Claude session places trades or flips `mode: "live"` — that
switch is the owner's, from the owner's own hands, by this repo's design and by the
assistant's own operating rules.

## 5. Decision points for the owner

| # | Decision | Default if you say nothing |
|---|---|---|
| 1 | Flip `enabled: true` + `mode: "paper"`? (R1) | Stays propose-only |
| 2 | Set the two portfolio secrets on executor.yml (R2 — owner-only) | Concentration gate stays dead |
| 3 | Want the regime gate built? (R3) | Executor stays regime-blind |
| 4 | IPS §5 "T-Capex 1-5" amendment (StockNews decision #1 — it also affects how this account should treat ai-capex-high names) | Trim review can't fire |
| 5 | Any manual use of the $500 is entirely the owner's call, placed by the owner in the app. The trees' current state is in § 3; the two standing system proposals are MSFT and AAPL, both below proposal price. | Cash stays cash |

---

*Sources: Robinhood MCP reads (accounts/portfolio/positions/quotes, 2026-08-12);
`proposals_log.json`; `producer_status.json`; `guardrails.json`; HANDOFF.md; StockNews
INDEX_META per ticker (post-PR-#522 state); `dashboards/macro_regime_overlay_2026-08.md`;
`dashboards/sovereign_regime_overlay.md`.*
