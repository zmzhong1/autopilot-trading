# Daily company research — Claude cloud routine

This is the **LLM-synthesis** companion to [`research.py`](../research.py). The
script gives you a deterministic daily digest (fundamentals + news + earnings +
analyst trend, cached + flagged). This routine adds what a script can't:
**judgment** — Claude reads the same data plus the StockNews thesis and recent
news and writes a short, reasoned "understanding" note per company, calling out
contradictions a rule can't see.

Run **both**: the script on a GitHub Actions cron (always-on, free), and this
routine on a Claude Code web schedule when you want the deeper read.

## Heads up: the deterministic version already runs

You do **not** need this routine for the daily digest — [research.yml](../.github/workflows/research.yml)
already runs `research.py` every weekday on GitHub Actions (with your
`FINNHUB_API_KEY`) and posts the digest. This routine is the *optional*
LLM-synthesis layer on top: it adds a reasoned `claude_note` per company.

## How to enable it (one-time, in your claude.ai account)

Routine creation is account-gated — it has to be done by you, from the web UI or
the CLI. It can't be created from inside a session.

1. Go to **[claude.ai/code/routines](https://claude.ai/code/routines) → New routine**
   (or run `/schedule weekdays at 7:45am — run routines/daily-research.md research`
   in a local CLI session logged in with your subscription).
2. **Prompt:** paste the **Routine prompt** block below.
3. **Repository:** `zmzhong1/autopilot-trading`.
4. **Environment — this is the step that makes it actually work:**
   - Env vars: `FINNHUB_API_KEY` (and `STOCKNEWS_GH_TOKEN` if StockNews is private).
   - **Network access → Custom**, and allow `finnhub.io`, `raw.githubusercontent.com`,
     `api.github.com` (keep the default package list checked). The Default
     "Trusted" network **blocks `finnhub.io`**, so without this the routine can't
     fetch quotes — this is the usual reason it "doesn't work".
5. **Trigger:** Schedule → **Weekdays** (minimum interval is 1 hour).
6. **Create**, then **Run now** to test.

## Routine prompt

```
You are the daily research analyst for the autopilot-trading repo. This is read-only
research — NEVER place, review, or stage a trade.

Step 1 — run the deterministic pass:
  python3 research.py
It researches today's rotating slice of the allow-list (Finnhub fundamentals + news +
earnings + analyst trend), writes research/{TICKER}.json, and posts the digest. (Requires
FINNHUB_API_KEY and finnhub.io network access — see this file's setup section.)

Step 2 — add judgment the script can't:
For each research/{TICKER}.json written today, also read the StockNews thesis at
reports/{TICKER}/tree_v1_en.md (INDEX_META + Section XII) and append a "claude_note" field:
a 3-5 sentence read of what the business is doing now, what changed this week, and whether
the fresh data SUPPORTS or CONTRADICTS the thesis. If it contradicts (analysts cooling on a
buy-rated name, earnings miss under high H-0, past review_due, a material news event), say so
explicitly and recommend a StockNews refresh. Cite the XII/H-0 numbers you react to.

Step 3 — commit the enriched cache:
  git add research/ && git commit -m "chore: daily research notes [skip ci]"
(Claude Code on the web pushes this to a claude/ branch automatically.)

Be skeptical and specific. A quiet "all fine" is the least useful outcome.
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
