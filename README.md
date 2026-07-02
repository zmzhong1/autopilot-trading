# Autopilot Trading — Free SEC + Congress Watcher

A $0/month alert pipeline for the data Autopilot, Quiver Quant, and Unusual Whales charge for:

- **SEC insider / institutional filings** (Form 4, 13D, 13G, 13F, 8-K) — sub-15-minute alerts via [sec_watcher.py](sec_watcher.py), enriched with parsed transaction details (insider name, buy/sell, shares, $-value, 8-K deep analysis with materiality + key figures, 13D stake size, 13F quarter-over-quarter diff).
- **Congressional trades** (STOCK Act PTRs, House + Senate) — hourly alerts via [congress_watcher.py](congress_watcher.py), colour-coded buy/sell with structured fields. Sourced from a CDN-hosted mirror (kadoa) with a Financial Modeling Prep fallback — no fragile site-scraping that datacenter IP-blocks can kill.
- **Weekly heartbeat** — every Monday morning, a digest of the past 7 days of alerts plus a watcher-health check via [heartbeat.py](heartbeat.py).
- **Ticker discovery** — also Mondays, a digest of NEW tickers worth adding to your news/research list, surfaced from the congressional-trades + EDGAR 8-K firehoses, via [discovery.py](discovery.py).
- **13F crowding** — also Mondays, a digest of names multiple tracked funds hold (and newly bought) the same quarter — smart-money consensus from the 13F filings you already parse, via [crowding.py](crowding.py).
- **Insider cluster buys** — every weekday evening, an alert when multiple insiders make open-market *purchases* of the same company in a rolling window — the high-signal opposite of routine sales, via [cluster_buys.py](cluster_buys.py).
- **Market regime gauge** — also Mondays, a "risk weather" snapshot (S&P trend, VIX, yield curve, credit spreads) that describes conditions — *not* a crash predictor — via [regime.py](regime.py).
- **Cross-feed confluence** — also Mondays, tickers lit up by multiple feeds at once (politician + insider + 8-K + crowded 13F) via [confluence.py](confluence.py).
- **StockNews state digest** — also Mondays, a cross-portfolio research view pulled from the sister [StockNews](https://github.com/zmzhong1/StockNews) repo via [stocknews_digest.py](stocknews_digest.py): action items due this week + top tickers by Section XII score.

Both watchers run on GitHub Actions cron and post Discord rich embeds (no plain-text spam). Built because the upstream data is 100% public, and the paid apps just sell the automation layer.

## What it watches

By default, [watchlist.json](watchlist.json) tracks:

**SEC EDGAR (`sec_watcher.py`)**

| Group | What | Default form filter |
|---|---|---|
| Big tech (Apple, Microsoft, Amazon, Alphabet, Tesla, NVIDIA) | Insider trades + material events | Form 4, 8-K |
| Berkshire Hathaway (Buffett) | Quarterly holdings + activist stakes + insiders | 13F-HR, SC 13D, SC 13G, 4, 8-K |
| Scion Asset Management (Burry) | Quarterly holdings + activist stakes | 13F-HR, SC 13D, SC 13G |
| Pershing Square (Ackman) | Quarterly holdings + activist stakes | 13F-HR, SC 13D, SC 13G |
| Bridgewater (Dalio) | Quarterly holdings | 13F-HR |
| Renaissance Technologies (Simons) | Quarterly holdings | 13F-HR |
| Citadel Advisors (Griffin) | Quarterly holdings + activist stakes | 13F-HR, SC 13D, SC 13G |
| Soros Fund Management | Quarterly holdings + activist stakes | 13F-HR, SC 13D |

All CIKs verified against SEC EDGAR.

**Congress trades (`congress_watcher.py`)**

Default `congress_members` watchlist matches by name substring (case-insensitive):
- Pelosi
- Crenshaw
- Tuberville
- Greene

Empty list = match all politicians. Add/remove names in [watchlist.json](watchlist.json).

## Disclosure timing — what's actually achievable

| Filing | Statutory deadline | Total lag from trade |
|---|---|---|
| Form 4 (insiders) | 2 business days | **~2 BD — near real-time** |
| SC 13D (activist 5%+) | 5 BD (tightened Feb 2024) | ~5 BD |
| SC 13D/A (amendment) | 2 BD | ~2 BD |
| SC 13G (passive 5%+) | 5 BD passive, up to 45 days for QIIs | 5 BD–45 days |
| 8-K (material events) | 4 BD | ~4 BD |
| 13F-HR (hedge funds) | 45 days post-quarter | **45–135 days** — no legal way to beat this |
| Form N-PORT (mutual funds) | 60 days uniform | 60 days |

This watcher gives you sub-15-minute alerts from when a filing is accepted by EDGAR. **The wall you can't break for free is 13F's 45-day post-quarter window** — but watching the same fund's 13D filings (5 BD) catches large stake changes weeks earlier.

## Quick start (5 minutes)

### 1. Create a Discord webhook

In your Discord server: `Server Settings → Integrations → Webhooks → New Webhook`. Copy the webhook URL — looks like `https://discord.com/api/webhooks/123.../abc...`.

If you don't have a Discord server, create one for yourself in 10 seconds — it's free.

### 2. Test locally

```bash
cd "$(pwd)"

# Replace with your real contact (SEC requires this) and webhook
export SEC_USER_AGENT="Your Name your@email.com"
export DISCORD_WEBHOOK="https://discord.com/api/webhooks/..."

# SEC watcher — first run seeds state, posts ZERO alerts
python3 sec_watcher.py

# Congress watcher — same first-run silent-seed behavior
python3 congress_watcher.py

# Subsequent runs: alerts on truly new filings only
python3 sec_watcher.py
python3 congress_watcher.py
```

Flags (apply to both watchers):
- `DRY_RUN=1` — log alerts to stdout instead of Discord (useful when editing watchlist)
- `MAX_ALERTS_PER_RUN=20` — cap per-run Discord posts (default 20). A batched same-day Form 4 card counts as one post.
- `FORM4_BATCH_MIN=2` — SEC watcher only; batch same-day Form 4 filings from one issuer into a single card once this many pile up (default 2). Lone Form 4s keep their richer per-insider card. Set very high to disable batching.
- `FMP_API_KEY` — Congress watcher only; a free [Financial Modeling Prep](https://site.financialmodelingprep.com/) key enabling the fallback source when the kadoa mirror is down/stale. Unset = kadoa only.
- `KADOA_STALE_DAYS=4` — Congress watcher only; if the mirror's newest filing is older than this many days, fall back to FMP.

### 3. Push to GitHub for free 24/7 monitoring

```bash
gh repo create autopilot-trading --private --source=. --remote=origin
git add .
git commit -m "feat: initial SEC EDGAR watcher"
git push -u origin main
```

Then add two secrets in `Repo Settings → Secrets and variables → Actions → New repository secret`:
- `SEC_USER_AGENT` = `Your Name your@email.com`
- `DISCORD_WEBHOOK` = your full webhook URL

Three workflows run on cron:
- [sec-watcher.yml](.github/workflows/sec-watcher.yml) — every 15 min, weekdays 12:00–23:00 UTC (8 AM – 7 PM ET). ~30s per run.
- [congress-watcher.yml](.github/workflows/congress-watcher.yml) — hourly at :07 past, weekdays 12:00–23:00 UTC. ~10s per run.
- [cluster-buys.yml](.github/workflows/cluster-buys.yml) — weekdays at 22:30 UTC (after the Form 4 acceptance window). Runs [cluster_buys.py](cluster_buys.py) and commits `cluster_state.json`.
- [heartbeat.yml](.github/workflows/heartbeat.yml) — Mondays at 13:00 UTC. Posts the 7-day digest + health check, then runs [discovery.py](discovery.py) (ticker candidates), [crowding.py](crowding.py) (13F consensus), [regime.py](regime.py) (risk weather), [confluence.py](confluence.py) (cross-feed overlap), and [stocknews_digest.py](stocknews_digest.py) (StockNews portfolio view). ~60s per run.

Total cost: ~150 min/mo, well within GitHub's free 2,000 min/mo. Edit cron schedules to taste.

### 4. Trigger the first run manually

In GitHub: `Actions → SEC Watcher → Run workflow`. The first run is silent (seeds state). The second run alerts on anything filed since.

## Editing the watchlist

[watchlist.json](watchlist.json) has two top-level arrays plus reference comments (any `_*` keys are ignored).

### `sec_ciks` — for SEC watcher

```json
{ "cik": "0001067983", "name": "Berkshire Hathaway", "forms": ["13F-HR", "SC 13D"] }
```

- **`cik`** — 10-digit zero-padded SEC CIK. Look up at https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany.
- **`name`** — Display name in Discord alerts.
- **`forms`** — Form types to watch. Empty/omitted = all forms.

The form filter matches exactly OR with `/A` amendment suffix. So `["SC 13D"]` catches both `SC 13D` and `SC 13D/A`.

Adding a new CIK after first run is safe — the script silently seeds new CIKs without spamming.

### `congress_members` — for Congress watcher

```json
"congress_members": ["Pelosi", "Crenshaw", "Tuberville", "Greene"]
```

Each entry is a case-insensitive substring matched against the filer's name (e.g. "Nancy Pelosi"). Use last names for unambiguous folks, full names if needed.

**Empty list `[]` = match ALL politicians** (firehose mode — the full recent-disclosure window, House + Senate).

### Re-seeding

To wipe state and re-seed silently:

```bash
rm state.json congress_state.json
python3 sec_watcher.py
python3 congress_watcher.py
```

## Common CIKs to add

| Filer | CIK |
|---|---|
| Berkshire Hathaway (Buffett) | 0001067983 |
| Scion Asset Management (Burry) | 0001649339 |
| Pershing Square (Ackman) | 0001336528 |
| Bridgewater (Dalio) | 0001350694 |
| Renaissance Technologies (Simons) | 0001037389 |
| Citadel Advisors (Griffin) | 0001423053 |
| Soros Fund Management | 0001029160 |
| Apple Inc | 0000320193 |
| Microsoft Corp | 0000789019 |
| Amazon.com | 0001018724 |
| Alphabet Inc | 0001652044 |
| Tesla Inc | 0001318605 |
| NVIDIA Corp | 0001045810 |

To find a fund's CIK from name, run:

```bash
curl -s -H "User-Agent: $SEC_USER_AGENT" \
  "https://efts.sec.gov/LATEST/search-index?q=%22FUND+NAME+HERE%22&forms=13F-HR" \
  | python3 -c "import json,sys; [print(h['_source']['ciks'][0],'-',h['_source']['display_names'][0]) for h in json.load(sys.stdin).get('hits',{}).get('hits',[])[:5]]"
```

## How the Congress watcher works

Disclosures come from two CDN/API sources, so no single blocked site can take the watcher dark (Capitol Trades and Senate eFD both IP-block datacenter runners — this watcher previously went silent for ~10 days when Capitol Trades started 429ing CI). The script:

1. **Primary — kadoa**: pulls a static JSON mirror of recent House + Senate PTRs from GitHub's CDN (`raw.githubusercontent.com`), which can't IP-block CI. Executive-branch / OGE rows (no chamber) are filtered out.
2. **Fallback — Financial Modeling Prep**: if kadoa is unreachable or its newest filing is older than `KADOA_STALE_DAYS` (default 4), it calls FMP's `house-latest` / `senate-latest` endpoints. Needs a free `FMP_API_KEY` secret; without one the watcher runs on kadoa alone.
3. Normalizes both sources into one trade shape and filters by the configured `congress_members` substrings.
4. Dedupes against `congress_state.json` using a **stable, source-agnostic synthetic id** (name + ticker + date + type + amount), so a disclosure posts once regardless of which source served it.
5. Posts each new trade to Discord with type emoji, size range, owner, dates, and a link to the filing.

A `SOURCE_VERSION` constant guards the id scheme — bumping it triggers a one-time silent reseed, so a source/format change never re-alerts the whole backlog. Transient fetch errors retry with backoff, then soft-fail (exit 0) so a throttle never fails the workflow; after `CONGRESS_ESCALATE_AFTER` consecutive failures it pings Discord so a real outage surfaces.

## What this does NOT cover

### Real-time congressional alerts (minute-latency)
This script polls hourly off a daily-refreshed mirror. If you want **minute-latency** Congress alerts, you still need:

- **[Unusual Whales free Discord](https://discord.com/invite/unusualwhales)** — auto-posts within minutes of EDGAR/PTR acceptance.
- Twitter follows: [@PelosiTracker_](https://twitter.com/PelosiTracker_), [@unusualwhales](https://twitter.com/unusualwhales), [@capitol2iq](https://twitter.com/capitol2iq).

The upstream mirror has its own ingestion lag (hours) from when a PTR hits house.gov/senate.gov, plus the underlying STOCK Act 30-day median filing lag. Best case end-to-end: ~30 days from trade to alert — a research feed, not a front-running tool.

### Real-time options flow / dark pool / unusual activity
Out of scope. This watches official SEC disclosures only. For options flow, that's what Unusual Whales / Cheddar Flow paid tiers actually sell — there's no free equivalent because the data feeds are licensed by exchanges.

### Trade execution
This started as a data-alerting watcher, not a copy-trading platform. The optional **execution layer** below now closes that loop — behind hard risk limits, propose-only by default. You can still just place orders by hand in any broker when an alert fires, or use a paid copy-trading service like [Dub](https://www.dubapp.com) ($9.99/mo unlimited) or [Autopilot](https://www.joinautopilot.com) ($100/yr per portfolio).

## Agentic execution (Robinhood MCP)

> **Status: propose-only by default. Nothing trades unless you explicitly opt in. Read this whole section before flipping a switch.**

Robinhood shipped an official **Agentic Trading** product (announced 2026-05-27): an [MCP](https://modelcontextprotocol.io) server your AI agent connects to, which can read your accounts and place orders inside a dedicated, **isolated "Agentic account"**. It's the sanctioned replacement for the deprecated, reverse-engineered `robin-stocks` path. Access is still rolling out — Robinhood emails you when you're in.

This repo adds the layer that turns the signals it already computes (insider cluster buys + congressional trades + crowded 13Fs + cross-feed [confluence](confluence.py)) into **vetted order proposals**, with live trading off by default.

### The three modes (`mode` in [guardrails.json](guardrails.json))

| Mode | What happens | Real money? |
|---|---|---|
| `propose` *(default)* | Vets signals against the guardrails, logs + posts a "PROPOSED orders" Discord card. **Never executes.** | No |
| `paper` | Simulates fills against a virtual cash balance to watch the loop behave. | No |
| `live` | Places real orders via the Robinhood Agentic MCP. Requires `enabled: true` **and** a wired [robinhood_mcp.py](robinhood_mcp.py) (not shipped — see below). | Yes |

Two independent stops guard execution: `enabled: false` (master switch, shipped off) collapses any mode to propose-only, and `EXECUTOR_KILL=1` (set in the CI workflow) force-disables execution even if `enabled` is true. **Live trading is impossible by config alone** — it also needs the MCP adapter wired, which this repo deliberately leaves as a stub.

### Guardrails ([guardrails.json](guardrails.json))

Every limit is enforced in the pure, unit-tested core ([test_executor.py](test_executor.py)):

- **`allow_list`** — only these tickers can ever be traded. **Empty = nothing tradable (fail-closed).** Primary blast-radius limit; seed from what you research in StockNews.
- **`block_list`** — hard deny, checked after the allow-list.
- **`allowed_sides`** — `["buy"]` by default (accumulate-only; the agent can never sell your position).
- **`min_signal_feeds`** — only act on confluence corroborated by ≥N independent feeds (default 3 = all-feed overlap).
- **`max_notional_per_order_usd`** / **`max_pct_account_per_order`** — order size is the *smaller* of the two caps.
- **`max_orders_per_day`** — daily order cap, counted from `executor_state.json`.
- **`max_total_deployed_pct`** — stop opening new positions past this fraction of the account.
- **`allow_options` / `allow_leverage`** — both forced false; the executor never constructs derivatives or margin orders.

### How to run it

```bash
# Propose-only, no Discord, prints the card it would post:
SEC_USER_AGENT='Your Name you@email.com' DRY_RUN=1 python3 executor.py

# Propose-only to Discord (safe to schedule; CI does exactly this):
SEC_USER_AGENT='Your Name you@email.com' DISCORD_WEBHOOK='https://...' python3 executor.py

# Paper-trade once you trust the proposals (edit guardrails.json: enabled=true, mode=paper):
SEC_USER_AGENT='...' python3 executor.py
```

CI runs [executor.yml](.github/workflows/executor.yml) every Monday at 14:00 UTC, **propose-only** (with `EXECUTOR_KILL=1` as a hard stop), so you get the proposal card weekly without any execution risk.

### Market-wide signal feeds (Finnhub)

The SEC watcher only follows insider/8-K filings for a handful of watched CIKs, so non-tech names (MU, CAT, …) could never reach the ≥3-feed bar. [finnhub_signals.py](finnhub_signals.py) closes that gap with a keyed, cloud-reliable, **market-wide** layer (free tier, `FINNHUB_API_KEY`) that scans the tradable universe (the allow-list) and adds three confluence feeds:

- **insider** — open-market insider *purchases* (Form 3/4/5), market-wide. Merges with the SEC Form-4 feed (set semantics — no double count), extending it to every industry.
- **analyst** — a net upgrade in the analyst recommendation trend month-over-month.
- **earnings** — a recent positive earnings surprise.

`analyst` and `earnings` are genuinely new, independent corroboration dimensions. Every feed degrades to "no signal" on any error (missing key, premium-gated endpoint, throttle) — it never fails a run, and SEC/congress/13F keep working without it. This is what lets MU/WMT/CAT and other industries surface, still gated by ≥3-feed confluence **and** the StockNews conviction check below.

### Daily company research (rotating, Finnhub budget-aware)

To use the free Finnhub quota productively, [research.py](research.py) deep-dives a **rotating slice** of the allow-list each weekday (~`RESEARCH_PER_DAY`/day → the full list weekly) instead of shallow-polling everything daily. Per name it pulls fundamentals + recent news + earnings + analyst trend, builds an "understanding" snapshot, **cross-checks the StockNews thesis**, and:

1. caches `research/{TICKER}.json` (committed) — the executor's `enrichment.research_note` reads the `flags`, so any open proposal for that name shows a `🔬 ⚠️ …` line;
2. flags stale/contradicted theses (analyst cooling on a buy-rated name, earnings miss under high H-0, past `review_due`);
3. posts a daily digest to the agentic channel.

Runs two ways (build both): the deterministic cron ([research.yml](.github/workflows/research.yml), weekday mornings) and an LLM-synthesis **Claude cloud routine** you enable on the web — see [routines/daily-research.md](routines/daily-research.md) for the prompt + setup. Both are read-only; neither ever trades.

### Research + portfolio grounding (every trade)

A confluence signal alone never books a trade. Each guardrail-approved proposal is then **grounded in** ([enrichment.py](enrichment.py)):

- **StockNews thesis** — the `INDEX_META` in `reports/{TICKER}/tree_v1_en.md` (read from a local checkout or the private repo via `STOCKNEWS_GH_TOKEN`). The trade is **skipped** if there's no thesis on file, if the durability test fired a fatal flag (`max_fatal_flags`), or if the Section XII score is below `min_xii_score` (the "avoid" band).
- **Research conviction** — beyond the headline XII number, a purchase conviction is computed from the *full* thesis: quality (XII) **and** confidence (H-0), durability (/25), bull/bear asymmetry, and thesis freshness (`review_due`). The trade is skipped below `min_conviction`. This is why a high-XII / low-confidence / unfavorable-asymmetry name is correctly rejected: COST (XII 91%, H-0 60%, bull/bear 18/30) → **low → skip**, while GOOGL (XII 90%, H-0 90%, 30/15) → **high**. The agent buys on the research, not the number.
- **Portfolio position** — the current holding from the Stock-Portfolio app (`GET /api/portfolio`, when `STOCK_PORTFOLIO_URL` + `STOCK_PORTFOLIO_TOKEN` are set). The trade is skipped if it would push an existing holding past `max_existing_position_pct`. Without creds the check is **noted, not failed**.

Both are **acknowledged on the proposal** — each order on the card carries a `↳ 📚 StockNews XII 90% strong-buy · 💼 not held` line, and the same context is written into the committed track record. Example decision trace:

```
### MSFT — signal: 2 feeds [corporate, insider]
  [2] guardrails : ✅ in allow-list, $50, within caps
  [3] StockNews  : XII 90% -> strong-buy | fatal_flags 0 | H-0 63%
  [4] portfolio  : not checked (no creds) — noted, not failed
  [5] DECISION   : ✅ PROPOSE BUY MSFT 10% acct (market)
### SIVE — XII 22% / 3 fatal flags
  [5] DECISION   : ❌ SKIP — StockNews fatal flag (3 > 0)
```

### Track record + scorecard (shareable)

Every proposal is appended to the committed [proposals_log.json](proposals_log.json), stamped with its **entry price** at proposal time ([prices.py](prices.py), keyless Stooq). Each Monday [scorecard.py](scorecard.py) marks every open proposal to the latest close and posts an 📈 **Agentic proposal scorecard** — per-signal return + aggregate **hit-rate** and **average return**. That's the track record that lets you (and anyone you share with) judge *how good* the signals are. It's read-only and never touches Robinhood.

### Sharing it (separate channel for a shared audience)

The agentic output (proposal cards + scorecard) posts to its **own** webhook, `EXECUTOR_DISCORD_WEBHOOK`, falling back to `DISCORD_WEBHOOK` only when that isn't set. Point `EXECUTOR_DISCORD_WEBHOOK` at a channel you're happy to share, and the SEC/Congress/heartbeat watcher noise stays on your private `DISCORD_WEBHOOK` — so the shared channel is **agentic trades and nothing else**. Sizing on those cards is shown as **% of account** (`share_size_display: "pct"` in guardrails.json), so a channel with other people in it never leaks your balance — switch to `"usd"` or `"none"` if you prefer.

### Going live (when you have access)

Live order placement is intentionally **not wired** — [robinhood_mcp.py](robinhood_mcp.py) is a documented stub, and `mode: live` degrades to clearly-labelled proposals until you implement it. The recommended path isn't an unattended cron: connect the MCP to **Claude Code** and review/place orders interactively from the proposal list —

```bash
claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading
```

(Claude Desktop, ChatGPT, Cursor, and Codex connect the same endpoint a different way — see the [Agentic Trading overview](https://robinhood.com/us/en/support/articles/agentic-trading-overview/).) Headless programmatic trading would additionally require Agentic access, an OAuth token, and an MCP client — built only once you've watched the loop behave. Until then, the safe default holds: the pipeline proposes, a human disposes.

## How the SEC watcher works

1. Reads [watchlist.json](watchlist.json) → list of CIK + form-type filters.
2. For each entry, hits `https://data.sec.gov/submissions/CIK{cik}.json` (free, no key, just `User-Agent`).
3. Filters returned filings by form types in watchlist.
4. Compares against [state.json](state.json) to find new ones.
5. Posts each new filing to Discord webhook (oldest first, so newest is most-recent in chat).
6. Updates state.json with the new accession numbers.
7. GitHub Actions commits state.json so it persists between cron runs.

State.json caps per-CIK history at 2,000 accessions (well above EDGAR's ~1,000-entry recent-submissions window) to prevent unbounded growth without losing entries that could be re-flagged as "new."

## Troubleshooting

**"HTTP 403"** — SEC is rejecting your User-Agent. Make it look like a real contact: `Your Name your@email.com`.

**"No alerts firing"** — Check `state.json` exists and `first_run_done: true`. If first run hasn't completed, it seeds silently.

**"Too many alerts on first deploy"** — Don't worry about it; first run is silent by design (`first_run_done: false` → seeds without notifying).

**"GitHub Actions cron not firing"** — GitHub Actions cron is unreliable for free-tier repos that haven't been pushed-to recently. Push any change to wake it up, or trigger manually via `Actions → Run workflow`.

**"State.json conflicts in git"** — The Actions workflow uses `[skip ci]` in commit messages and `concurrency` to avoid running over itself. If you push a manual change while a cron run is in flight, you may get a merge conflict on state.json — resolve by accepting the cron's version.

**"Discord rate limit"** — Default is 0.5s between posts. If you have many alerts at once, raise `MAX_ALERTS_PER_RUN` in the workflow but stay under Discord's webhook rate limit (~30/min).

**"I want a different CIK"** — Add it to [watchlist.json](watchlist.json). The SEC watcher silent-seeds new CIKs automatically on the next run.

**"Congress watcher: `No congress trades returned` / soft-failing"** — the kadoa mirror is unreachable or stale and there's no FMP fallback configured. Check the source directly (`curl -s "$KADOA_TRADES_URL" | head -c 200`); if kadoa is down, add a free `FMP_API_KEY` secret to enable the fallback. The watcher soft-fails (exit 0) and pings Discord after `CONGRESS_ESCALATE_AFTER` consecutive failures.

**"Congress watcher missing my favorite politician"** — confirm the name substring matches the filer name the mirror uses, and that they've filed a PTR recently (the mirror is a rolling ~6-month, ~5,000-record window). Empty `congress_members` = match everyone.

**"StockNews digest empty / fetch failed"** — The digest reads `dashboards/watchlist_state.md` from the StockNews repo's `phase-1-scaffold` branch. If StockNews moved its default branch, set `STOCKNEWS_BRANCH=<new-branch>` in the heartbeat workflow env. If the markdown structure changed, update `parse_action_items` / `parse_ranked_table` in [stocknews_digest.py](stocknews_digest.py).

## Hard truths

- **You cannot beat 13F's 45-day window for free, period.** Statutory.
- **Pro algos trade Form 4 within seconds.** Free retail can move from "weeks behind" to "minutes behind," not "ahead."
- **All free copy-trading carries lag risk** — Autopilot, Dub, eToro all wait for the public filing. No time machines.

## What the Discord alerts look like

Alerts are Discord rich embeds (colour-coded, structured fields, hyperlinked). The watchers fetch each filing's structured data (XML where available) and surface the parts that matter:

| Form | Embed surfaces |
|---|---|
| **Form 4** (insider) | 🟢/🔴 buy or sell · insider name + role · transaction code (P/S/A/M/F…) · shares × price = $-value · post-transaction holdings · per-leg breakdown for multi-leg filings · accession + EDGAR deep link |
| **Form 4 (same-day batch)** | 📄 one card when an issuer files ≥`FORM4_BATCH_MIN` insider Form 4s on the same day · one line per insider (side · role · $-value · deep link) · net buy/sell totals — collapses high-volume large-cap insider noise into a single notification |
| **8-K** | 📋 embed colour-coded by **materiality** (🚨 red critical — bankruptcy/restatement/delisting · 🔴 orange high — earnings/M&A/officer change · 🟡 yellow medium · ⚪ grey low — Reg FD decks) · the watcher fetches the actual filing + press-release exhibit and surfaces: per-item summaries in the filing's own words, 💰 key figures (revenue/EPS/margin/🔭 guidance) pulled from the earnings exhibit, 👤 leadership changes for `5.02`, and the press-release headline + link. Falls back to the plain item-code list when the document can't be parsed |
| **SC 13D / 13G** | 🎯 purple embed · target issuer name + CUSIP · % of class · aggregate shares (parsed from post-2024 mandated XML schema) |
| **13F-HR** | 🏦 blue embed · position count · total $-value · diff vs. prior quarter: 🆕 new positions, ❌ exits, 📈 increases, 📉 decreases (top 5 each, ranked by $) |
| **Congress trade** | 🏛️ green/red embed · politician · buy/sell · issuer · size range · owner · trade date / pub date / lag |

Where structured XML is unavailable (older filings, malformed XML), the watcher falls back gracefully to the original "Filer — form filed date + link" format.

## 8-K deep analysis → events feed → StockNews

Every 8-K the watcher alerts on is also **analyzed and persisted**, not just labeled:

1. `sec_enrich.enrich_8k` fetches the filing's primary document and its press-release exhibit (EX-99), extracts per-item summaries, financial highlights, personnel changes, and assigns a **materiality band** (`critical` / `high` / `medium` / `low` per item code — see `ITEM_8K_MATERIALITY`).
2. The full breakdown is appended to **[events/8k_events.jsonl](events/8k_events.jsonl)** (committed, rolling 500 events, deduped by accession) with the ticker resolved via SEC `company_tickers.json`.
3. Three consumers read it:
   - **StockNews** (`zmzhong1/StockNews`): its `orchestration/autopilot_events.py` pulls a ticker's recent analyzed 8-Ks straight into the stage-6 `thesis-update` evidence pass — material events arrive pre-summarized with materiality attached.
   - **Confluence / executor signals**: the materiality lands in `state.json → alert_history`, and `confluence.py` now **skips `low`-materiality 8-Ks** (Reg FD decks, exhibit-only 9.01s) when counting the 📋 corporate feed — a routine investor-deck upload can no longer help push a name over `min_signal_feeds`.
   - **Proposal cards**: `enrichment.recent_8k_events` surfaces the latest high/critical 8-K on each executor proposal (`📋 8-K 2026-07-02 HIGH [2.02]`), so a buy visibly knows about the earnings release or CFO exit it trades into.

## Ticker discovery

`discovery.py` runs every Monday alongside the heartbeat and posts a 🔎 digest of tickers worth adding to your news / research list (e.g. a sister project like StockNews). Two signals:

- **Congress firehose** — across ALL politicians (not just your watchlist), tickers traded by the most distinct politicians in the recent-disclosure window (same kadoa/FMP source as the watcher). More distinct politicians ≈ stronger signal.
- **EDGAR 8-K firehose** — across the entire market, CIKs filing the most 8-Ks in the latest atom snapshot, mapped to tickers via SEC's `company_tickers.json`. Noisy on its own — the embed includes the issuer name so you can eyeball.

Tickers already in `watchlist.json → stocknews_tickers` are excluded — populate that list with what your news project already covers, and the digest will only surface NEW candidates. To suppress a suggestion permanently, just add its ticker.

## 13F crowding digest

`crowding.py` runs every Monday alongside the heartbeat and posts a 🏦 digest cross-referencing the latest 13F-HR holdings of every tracked fund (the `watchlist.json → sec_ciks` entries whose `forms` include `13F-HR`). When several smart-money managers hold the *same* security, that overlap is a consensus signal; when several *newly* bought it the same quarter, that's the strongest version.

It reuses the same `sec_enrich.fetch_13f_holdings` parser the SEC watcher relies on — no new data source. Two sections:

- **🤝 Most crowded** — names held by the most distinct funds, ranked by fund count then aggregate $-value, with each holder's position size (🆕 marks a newly added position).
- **🆕 New consensus this quarter** — names ≥2 funds *newly* bought, the highest-signal subset.

Flags:
- `CROWDING_MIN_FUNDS=2` — minimum distinct funds holding a name for it to count (default 2).
- `CROWDING_TOP_N=12` — max names shown per section (default 12).

Overlap is computed by CUSIP, so different share classes (e.g. GOOGL vs GOOG) are kept distinct.

## Insider cluster buys

`cluster_buys.py` runs every weekday evening and alerts 🟢 when **multiple distinct insiders make open-market purchases (Form 4 transaction code `P`) of the same company** within a rolling window. A lone insider *sale* is routine (10b5-1 plans, RSU vesting, tax withholding); multiple insiders *buying* their own stock with their own cash is one of the few insider signals with real predictive history. Only code-`P` purchases count — grants, option exercises, and sales are ignored.

It reuses `sec_enrich.enrich_form4` (no new data source) and de-dups via `cluster_state.json`: a cluster alerts once, and again only when a genuinely new purchase joins it (e.g. "now 3 insiders"). Window aging or unchanged clusters don't re-fire.

Flags:
- `CLUSTER_LOOKBACK_DAYS=14` — rolling window for "within N days" (default 14).
- `CLUSTER_MIN_INSIDERS=2` — distinct insiders needed to call it a cluster (default 2).
- `CLUSTER_MAX_FORM4_PER_CIK=60` — cap on Form 4s parsed per company per run (politeness/runtime guard).

## Market regime gauge

`regime.py` runs every Monday and posts a 🌡️ "risk weather" card. It does **not** predict crashes — it *describes* current conditions from free public data and rolls them into a calm / mixed / stressed read so you have context, not a forecast. Signals (each contributes risk points):

- **Trend** — S&P 500 vs its 200-day moving average (Stooq, no key).
- **Volatility** — VIX level (Stooq, no key).
- **Yield curve** — 10y−2y Treasury spread (FRED, optional key).
- **Credit** — high-yield OAS credit spread (FRED, optional key).

Works out of the box on Stooq alone. Set `FRED_API_KEY` (free from the [FRED API](https://fred.stlouisfed.org/docs/api/api_key.html)) as a repo secret to add the yield-curve and credit legs.

## Cross-feed confluence

`confluence.py` runs every Monday and surfaces 🎯 tickers lit up by **multiple independent feeds at once** — any one feed is noise, but overlap is worth a look. It stitches together signals the project already collects:

- **🏛️ congress** — recent congressional trades (`congress_state.json`).
- **🧑‍💼 insider** — recent Form 4 alerts (`state.json`).
- **📋 corporate** — recent 8-K alerts (`state.json`).
- **🏦 institutional** — crowded names from the latest 13F-HRs (reuses `crowding.py`).

Everything is keyed to a ticker (watchlist CIK→ticker via SEC's `company_tickers.json`, best-effort issuer-name matching for 13F holdings). Names active across ≥`CONFLUENCE_MIN_FEEDS` feeds are ranked by feed count; ≥3-feed overlaps get a ⭐.

Flags:
- `CONFLUENCE_LOOKBACK_DAYS=30` — window for the dated feeds (default 30).
- `CONFLUENCE_MIN_FEEDS=2` — minimum distinct feeds to report a ticker (default 2).
- `CONFLUENCE_INCLUDE_13F=1` — set to `0` to skip the (networked) 13F leg.
- `CONFLUENCE_TOP_N=12` — max tickers shown (default 12).

Tunables (env vars):
- `DISCOVERY_TOP_N=10` — entries per source in the digest.
- `DISCOVERY_8K_FEED_COUNT=100` — atom feed page size.

## StockNews state digest

`stocknews_digest.py` runs every Monday alongside the heartbeat. It fetches `dashboards/watchlist_state.md` from the [zmzhong1/StockNews](https://github.com/zmzhong1/StockNews) sister repo (research project that produces falsifiable investment trees per ticker), parses it, and posts a 📊 embed combining the most actionable parts:

- **🔔 Today** — events triggering today (earnings, refresh-due dates).
- **⏳ Reviews due within 7 days** — thesis updates approaching their cadence threshold.
- **📄 10-K cache stale** — source filings older than the methodology threshold.
- **🏆 Top N by Section XII** — ranked tickers from the K.3.5 weighted-score table, with archetype + current action band.

This pairs with the existing `heartbeat` + `discovery` posts: filings + research land in one Discord channel.

Tunables (env vars):
- `STOCKNEWS_BRANCH=phase-1-scaffold` — which StockNews branch to read from.
- `STOCKNEWS_TOP_N=5` — ranked-table rows to surface.

The StockNews repo also pushes its own event-driven embeds (routine summaries, scaffold completions, new tree publications) via `orchestration/notify.py` — those use the same `DISCORD_WEBHOOK` secret on the StockNews side.

## Files

- [sec_watcher.py](sec_watcher.py) — SEC EDGAR watcher (stdlib only)
- [sec_enrich.py](sec_enrich.py) — Form 4 / 8-K / 13D-G / 13F-HR XML parsers + diff helpers (stdlib only)
- [congress_watcher.py](congress_watcher.py) — Congress-trades watcher: kadoa primary + FMP fallback (stdlib only)
- [heartbeat.py](heartbeat.py) — Weekly digest + watcher-health check (stdlib only)
- [discovery.py](discovery.py) — Weekly ticker-discovery digest from congressional-trades + 8-K firehoses (stdlib only)
- [crowding.py](crowding.py) — Weekly 13F cross-fund crowding digest; reuses sec_enrich's 13F parser (stdlib only)
- [cluster_buys.py](cluster_buys.py) — Daily insider cluster-buy detector (open-market code-P purchases); reuses sec_enrich.enrich_form4 (stdlib only)
- [regime.py](regime.py) — Weekly market regime gauge from Stooq (S&P/VIX) + optional FRED macro (stdlib only)
- [confluence.py](confluence.py) — Weekly cross-feed confluence over congress + insider + 8-K + 13F (stdlib only)
- [stocknews_digest.py](stocknews_digest.py) — Weekly cross-portfolio research digest fetched from the StockNews sister repo (stdlib only)
- [executor.py](executor.py) — Guardrailed execution layer; turns confluence signals into vetted order proposals (propose-only by default), reuses confluence.py (stdlib only)
- [robinhood_mcp.py](robinhood_mcp.py) — Robinhood Agentic MCP adapter; documented stub at the live-execution boundary until you wire it (stdlib only)
- [finnhub_signals.py](finnhub_signals.py) — Market-wide signal layer (Finnhub free tier): insider buys + analyst upgrades + earnings beats as confluence feeds, so non-SEC-watched industries surface (stdlib only)
- [research.py](research.py) — Daily rotating company research: fundamentals + news + earnings + analyst trend, cross-checked vs the StockNews thesis; caches research/ + posts a digest (stdlib only)
- [prices.py](prices.py) — Equity price helper: Finnhub /quote (primary) + Stooq fallback; entry price + scorecard mark-to-market (stdlib only)
- [enrichment.py](enrichment.py) — Per-trade grounding: reads the StockNews thesis (INDEX_META) + portfolio position so each proposal is gated on and acknowledges the research (stdlib only)
- [scorecard.py](scorecard.py) — Weekly proposal track record: marks logged proposals to market, posts hit-rate + avg return to the agentic channel (stdlib only)
- [guardrails.json](guardrails.json) — Risk limits for the executor: allow-list, per-order + daily + deployment caps, mode + kill switch, share_size_display
- [proposals_log.json](proposals_log.json) — Committed track record: one entry-priced row per proposal, scored weekly (auto-managed)
- [watchlist.json](watchlist.json) — CIK list + form-type filter + congress_members + stocknews_tickers exclusion list
- [state.json](state.json) — SEC seen-accession state + rolling alert history (auto-managed)
- [congress_state.json](congress_state.json) — Congress seen-trade-ID state + rolling alert history (auto-managed)
- [cluster_state.json](cluster_state.json) — Insider cluster-buy alerted-accession state (auto-managed)
- [.github/workflows/sec-watcher.yml](.github/workflows/sec-watcher.yml) — SEC cron, every 15 min
- [.github/workflows/congress-watcher.yml](.github/workflows/congress-watcher.yml) — Congress cron, hourly
- [.github/workflows/cluster-buys.yml](.github/workflows/cluster-buys.yml) — Cluster-buy cron, weekday evenings
- [.github/workflows/heartbeat.yml](.github/workflows/heartbeat.yml) — Heartbeat cron, weekly Monday (heartbeat → discovery → crowding → regime → confluence → stocknews_digest)
- [.github/workflows/executor.yml](.github/workflows/executor.yml) — Executor + scorecard cron, weekly Monday — propose-only (EXECUTOR_KILL=1 hard stop)
- [test_executor.py](test_executor.py) — Tests for the executor's pure sizing + guardrail logic (stdlib unittest, no network)
- [test_scorecard.py](test_scorecard.py) — Tests for scorecard scoring + executor size-label/track-record helpers (stdlib unittest, no network)
- [test_enrichment.py](test_enrichment.py) — Tests for StockNews/portfolio enrichment + the executor thesis/portfolio gate (stdlib unittest, no network)
- [test_finnhub_signals.py](test_finnhub_signals.py) — Tests for Finnhub feed scoring (insider buys, net upgrade, earnings beat) + quote parsing (stdlib unittest, no network)
- [test_research.py](test_research.py) — Tests for daily-research helpers (rotation slice, metric pick, thesis flags) (stdlib unittest, no network)
- [.gitignore](.gitignore)

## License

Public domain / do whatever you want.
