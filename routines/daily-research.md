# Daily company research — Claude cloud routine

This is the **LLM-synthesis** companion to [`research.py`](../research.py). The
script gives you a deterministic daily digest (fundamentals + news + earnings +
analyst trend, cached + flagged). This routine adds what a script can't:
**judgment** — Claude reads the same data plus the StockNews thesis and recent
news and writes a short, reasoned "understanding" note per company, calling out
contradictions a rule can't see.

Run **both**: the script on a GitHub Actions cron (always-on, free), and this
routine on a Claude Code web schedule when you want the deeper read.

## How to enable it (Claude Code on the web)

1. Open this repo (`zmzhong1/autopilot-trading`) in Claude Code on the web.
2. Create a **scheduled session / trigger** (see
   https://code.claude.com/docs/en/claude-code-on-the-web) on a daily weekday
   cadence — e.g. ~07:45 ET, after `research.yml` has refreshed the cache.
3. Set the session prompt to the **Routine prompt** below.
4. Ensure the session env has `FINNHUB_API_KEY` (and `STOCKNEWS_GH_TOKEN` if the
   StockNews repo is private) so the routine can read fundamentals + the thesis.

> Scheduling lives in the web UI — it can't be created from inside a session.
> This file is the prompt + contract; you turn on the schedule once.

## Routine prompt

```
You are the daily research analyst for the autopilot-trading system. Today, deep-research
the rotating slice of tickers for this weekday (use research.py's daily_slice over the
guardrails.json allow_list, or just read today's research/*.json the cron already wrote).

For each ticker:
1. Read the fresh Finnhub data (fundamentals, recent news, earnings, analyst trend) and
   the cached snapshot in research/{TICKER}.json.
2. Read the StockNews thesis (reports/{TICKER}/tree_v1_en.md INDEX_META + Section XII).
3. Write a 3-5 sentence "understanding" note: what the business is doing now, what changed
   this week, and whether the fresh data SUPPORTS or CONTRADICTS the StockNews thesis.
4. If it contradicts (analysts cooling on a buy-rated name, earnings miss under high H-0,
   thesis past review_due, a material news event), say so explicitly and recommend a
   StockNews refresh.

Then:
- Append your per-company notes to research/{TICKER}.json under a "claude_note" field.
- Commit the updated research/ files (message: "chore: daily research notes [skip ci]").
- Post a concise digest to the agentic Discord channel via orchestration/notify or by
  running: do NOT place any trades — this is research only.

Be skeptical and specific. Flag divergences loudly; a quiet "all fine" is the least useful
outcome. Keep notes English-only and cite the StockNews XII/H-0 numbers you're reacting to.
```

## What it must NOT do

- **Never place, review, or stage a trade.** This routine is read-only research.
- Don't rewrite StockNews trees — flag that a refresh is warranted; the refresh
  is StockNews's job.
- Keep within the free Finnhub budget — it researches a daily *slice*, not the
  whole list every run.

## Relationship to the rest of the system

- `research.py` (cron) writes `research/{TICKER}.json` → the executor's
  `enrichment.research_note` reads the `flags`, surfacing `🔬 ⚠️ …` on any
  proposal for that name.
- This routine enriches the same files with a `claude_note`, so the
  understanding compounds day over day without re-fetching everything.
