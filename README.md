# Autopilot Trading — Free SEC EDGAR Watcher

A $0/month, sub-15-minute alert pipeline for SEC insider/institutional filings that mirrors what paid apps like [Autopilot](https://www.joinautopilot.com), Quiver Quant, and Unusual Whales charge for. Polls SEC EDGAR every 15 minutes via GitHub Actions, posts new filings to a Discord channel via webhook.

Built because Autopilot's underlying data is 100% free public data, and they don't have a time edge — they just sell automation. This script gives you the data layer for free.

## What it watches

By default, the [watchlist.json](watchlist.json) tracks:

| Group | What | Default form filter |
|---|---|---|
| Big tech (Apple, Microsoft, Amazon, Alphabet, Tesla, NVIDIA) | Insider trades + material events | Form 4, 8-K |
| Berkshire Hathaway (Buffett) | Quarterly holdings + activist stakes + Berkshire's own insiders | 13F-HR, SC 13D, SC 13G, 4, 8-K |
| Scion Asset Management (Burry) | Quarterly holdings + activist stakes | 13F-HR, SC 13D, SC 13G |
| Pershing Square (Ackman) | Quarterly holdings + activist stakes | 13F-HR, SC 13D, SC 13G |
| Bridgewater (Dalio) | Quarterly holdings | 13F-HR |
| Renaissance Technologies (Simons) | Quarterly holdings | 13F-HR |
| Citadel Advisors (Griffin) | Quarterly holdings + activist stakes | 13F-HR, SC 13D, SC 13G |
| Soros Fund Management | Quarterly holdings + activist stakes | 13F-HR, SC 13D |

All CIKs verified against SEC EDGAR. Edit [watchlist.json](watchlist.json) to add/remove.

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

# First run: seeds state, posts ZERO alerts
python3 sec_watcher.py

# Subsequent runs: alerts on truly new filings only
python3 sec_watcher.py
```

Flags:
- `DRY_RUN=1` — log alerts to stdout instead of Discord (useful for editing watchlist)
- `MAX_ALERTS_PER_RUN=20` — cap per-run alerts (default 20; raise if you watch many CIKs)

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

The [.github/workflows/sec-watcher.yml](.github/workflows/sec-watcher.yml) cron fires every 15 min, weekdays, 12:00–23:00 UTC (8 AM – 7 PM ET). Edit the cron to taste. Free GitHub Actions minutes (2,000/mo) are plenty — each run uses ~30 seconds.

### 4. Trigger the first run manually

In GitHub: `Actions → SEC Watcher → Run workflow`. The first run is silent (seeds state). The second run alerts on anything filed since.

## Editing the watchlist

[watchlist.json](watchlist.json) has two arrays plus reference comments (any `_*` keys are ignored by the script). Each entry:

```json
{ "cik": "0001067983", "name": "Berkshire Hathaway", "forms": ["13F-HR", "SC 13D"] }
```

- **`cik`** — 10-digit zero-padded SEC CIK. Look up at https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany.
- **`name`** — Display name in Discord alerts.
- **`forms`** — Form types to watch. Empty/omitted = all forms.

The form filter matches exactly OR with `/A` amendment suffix. So `["SC 13D"]` catches both `SC 13D` and `SC 13D/A` (amendments).

After editing, **delete `state.json`** to re-seed without spamming alerts on already-seen filings:

```bash
rm state.json
python3 sec_watcher.py   # silent re-seed
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

## What this does NOT cover

### Congressional trades (Pelosi, Crenshaw, etc.)
Not in this watcher. Congressional STOCK Act PTRs are filed at house.gov/senate.gov, not SEC EDGAR. The free aggregators that used to expose them as JSON (House Stock Watcher, Senate Stock Watcher) appear to be defunct. Best free alternatives:

- **[Unusual Whales free Discord](https://discord.com/invite/unusualwhales)** — auto-posts congressional trades within minutes of filing. Best free real-time option.
- **[Capitol Trades](https://www.capitoltrades.com)** — cleanest browse UI; pair with [Visualping](https://visualping.io) free tier for change-detection alerts on a politician's page.
- **Twitter follows:** [@PelosiTracker_](https://twitter.com/PelosiTracker_), [@unusualwhales](https://twitter.com/unusualwhales), [@capitol2iq](https://twitter.com/capitol2iq).
- Primary source: [disclosures-clerk.house.gov](https://disclosures-clerk.house.gov/FinancialDisclosure) (PDFs, painful) and [efdsearch.senate.gov](https://efdsearch.senate.gov).

A future enhancement could add a Capitol Trades scraper to this same watcher.

### Real-time options flow / dark pool / unusual activity
Out of scope. This watches official SEC disclosures only. For options flow, that's what Unusual Whales / Cheddar Flow paid tiers actually sell — there's no free equivalent because the data feeds are licensed by exchanges.

### Trade execution
This is a data-alerting watcher, not a copy-trading platform. To actually copy-trade based on alerts, manually place orders in a free broker (Fidelity, Schwab, Robinhood) when an alert fires. Or use a paid copy-trading service like [Dub](https://www.dubapp.com) ($9.99/mo unlimited) or [Autopilot](https://www.joinautopilot.com) ($100/yr per portfolio).

## How it works

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

**"I want a different CIK"** — Add it to [watchlist.json](watchlist.json), then `rm state.json` and re-run to re-seed silently.

## Hard truths

- **You cannot beat 13F's 45-day window for free, period.** Statutory.
- **Pro algos trade Form 4 within seconds.** Free retail can move from "weeks behind" to "minutes behind," not "ahead."
- **All free copy-trading carries lag risk** — Autopilot, Dub, eToro all wait for the public filing. No time machines.

## Files

- [sec_watcher.py](sec_watcher.py) — main script (stdlib only, no pip deps)
- [watchlist.json](watchlist.json) — your CIK + form-type config
- [state.json](state.json) — seen-accession state (auto-managed; safe to delete to reset)
- [.github/workflows/sec-watcher.yml](.github/workflows/sec-watcher.yml) — 15-min cron
- [.gitignore](.gitignore)

## License

Public domain / do whatever you want.
