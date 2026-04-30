# Autopilot Trading — Free SEC + Congress Watcher

A $0/month alert pipeline for the data Autopilot, Quiver Quant, and Unusual Whales charge for:

- **SEC insider / institutional filings** (Form 4, 13D, 13G, 13F, 8-K) — sub-15-minute alerts via [sec_watcher.py](sec_watcher.py)
- **Congressional trades** (STOCK Act PTRs scraped from Capitol Trades) — hourly alerts via [congress_watcher.py](congress_watcher.py)

Both watchers run on GitHub Actions cron and post new filings to a Discord webhook. Built because the upstream data is 100% public, and the paid apps just sell the automation layer.

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

**Capitol Trades (`congress_watcher.py`)**

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
- `MAX_ALERTS_PER_RUN=20` — cap per-run alerts (default 20)
- `CAPITOL_TRADES_PAGE_SIZE=96` — Congress watcher only; max trades fetched per run

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

Two workflows run on cron:
- [sec-watcher.yml](.github/workflows/sec-watcher.yml) — every 15 min, weekdays 12:00–23:00 UTC (8 AM – 7 PM ET). ~30s per run.
- [congress-watcher.yml](.github/workflows/congress-watcher.yml) — hourly at :07 past, weekdays 12:00–23:00 UTC. ~10s per run.

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

Each entry is a case-insensitive substring matched against Capitol Trades' politician string (which has format "Name Party Chamber State", e.g. "Nancy Pelosi Democrat House CA"). Use last names for unambiguous folks, full names if needed.

**Empty list `[]` = match ALL politicians** (firehose mode — ~96 trades per run).

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

Capitol Trades' BFF API blocks the `/trades` endpoint, but the SSR HTML at `https://www.capitoltrades.com/trades?pageSize=96` exposes the latest 96 trades server-rendered. The script:

1. Fetches that HTML with a browser User-Agent (no Playwright at runtime — stdlib `urllib`).
2. Splits by `<tr data-state="false">` boundaries and extracts each row's politician ID, issuer ID, trade ID, and 9 displayed cells.
3. Filters by configured `congress_members` substrings.
4. Compares trade IDs against `congress_state.json` to find new ones.
5. Posts each new trade to Discord with type emoji, size range, owner, dates, and a link to the trade detail page.

Trade IDs are stable and sequential, so `sorted(trade_id)` ≈ chronological — alerts go oldest-first so newest is most-recent in Discord.

If Capitol Trades changes their HTML structure, the parser will return zero trades and the script logs `[WARN] No trades parsed`. Re-run discovery with `playwright-cli goto https://www.capitoltrades.com/trades` to update the row boundary regex.

## What this does NOT cover

### Real-time congressional alerts (faster than Capitol Trades)
This script polls Capitol Trades hourly. If you want **minute-latency** Congress alerts (5-10 min faster than Capitol Trades reflects them), you still need:

- **[Unusual Whales free Discord](https://discord.com/invite/unusualwhales)** — auto-posts within minutes of EDGAR/PTR acceptance.
- Twitter follows: [@PelosiTracker_](https://twitter.com/PelosiTracker_), [@unusualwhales](https://twitter.com/unusualwhales), [@capitol2iq](https://twitter.com/capitol2iq).

Capitol Trades has its own ingestion lag of ~15-60 min from when a PTR hits house.gov/senate.gov. Plus the underlying STOCK Act 30-day median filing lag. Best case end-to-end: ~30 days from trade to alert.

### Real-time options flow / dark pool / unusual activity
Out of scope. This watches official SEC disclosures only. For options flow, that's what Unusual Whales / Cheddar Flow paid tiers actually sell — there's no free equivalent because the data feeds are licensed by exchanges.

### Trade execution
This is a data-alerting watcher, not a copy-trading platform. To actually copy-trade based on alerts, manually place orders in a free broker (Fidelity, Schwab, Robinhood) when an alert fires. Or use a paid copy-trading service like [Dub](https://www.dubapp.com) ($9.99/mo unlimited) or [Autopilot](https://www.joinautopilot.com) ($100/yr per portfolio).

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

**"Congress watcher returns 0 trades / `No trades parsed`"** — Capitol Trades changed their HTML. Re-run discovery: `playwright-cli goto https://www.capitoltrades.com/trades`, then `playwright-cli --raw eval "JSON.stringify([...document.querySelectorAll('table tbody tr')].slice(0,1).map(r => ({cells: [...r.querySelectorAll('td')].map(td => td.innerText.trim())})))"` and update the regex in `parse_trades()`.

**"Congress watcher missing my favorite politician"** — They probably haven't traded in the last 96 trades on Capitol Trades. Increase `CAPITOL_TRADES_PAGE_SIZE` env var (max ~96 confirmed working; higher may break) or check their politician page directly.

## Hard truths

- **You cannot beat 13F's 45-day window for free, period.** Statutory.
- **Pro algos trade Form 4 within seconds.** Free retail can move from "weeks behind" to "minutes behind," not "ahead."
- **All free copy-trading carries lag risk** — Autopilot, Dub, eToro all wait for the public filing. No time machines.

## Files

- [sec_watcher.py](sec_watcher.py) — SEC EDGAR watcher (stdlib only)
- [congress_watcher.py](congress_watcher.py) — Capitol Trades scraper (stdlib only)
- [watchlist.json](watchlist.json) — CIK list + form-type filter + congress_members list
- [state.json](state.json) — SEC seen-accession state (auto-managed)
- [congress_state.json](congress_state.json) — Congress seen-trade-ID state (auto-managed)
- [.github/workflows/sec-watcher.yml](.github/workflows/sec-watcher.yml) — SEC cron, every 15 min
- [.github/workflows/congress-watcher.yml](.github/workflows/congress-watcher.yml) — Congress cron, hourly
- [.gitignore](.gitignore)

## License

Public domain / do whatever you want.
