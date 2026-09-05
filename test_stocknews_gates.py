#!/usr/bin/env python3
"""Tests for the StockNews-derived gates added 2026-09-05: decision-journal
action, sovereign band, and the macro-regime gate on cycle_exposure. Pure —
enrich_fn is injected, no network."""

import os
import unittest
from datetime import date, datetime, timezone

os.environ.setdefault("SEC_USER_AGENT", "test test@example.com")

import enrichment  # noqa: E402
import executor  # noqa: E402

TODAY = date(2026, 9, 7)
NOW = datetime(2026, 9, 7, 14, 0, tzinfo=timezone.utc)

TREE = """# NVIDIA (NVDA) — Investment Tree v1

<!-- INDEX_META
schema: 3
updated: 2026-08-10
review_due: 2026-11-28
verdicts: 8 ✅ · 8 ⚠️ · 3 ✗ · 1 ⊗
h0: 78%
prob: 30/50/20
price: $223.96
durability: 21/25
archetype: dominant-incumbent-under-forced-dual-transition
archetype_category: incumbent-under-threat
mispricing_source: capital-intensity-misunderstanding
fatal_flags: 0
xii_score: 86%
cycle_exposure: ai-capex-high
sovereign_exposure: sovereign-impaired
-->
"""

DECISIONS = """{"date": "2026-05-03", "ticker": "X", "action": "watch"}
not json
{"date": "2026-08-01", "ticker": "X", "action": "WAIT", "size_pct_of_portfolio": 0, "size_reason": "too rich"}
"""


def gr(**over):
    g = dict(executor.DEFAULT_GUARDRAILS)
    g.update({"enabled": True, "mode": "propose", "account_value_usd": 500.0,
              "allow_list": ["NVDA", "AAPL"], "min_signal_feeds": 3,
              "min_conviction": "medium"})
    g.update(over)
    return g


def thesis(**over):
    t = {"found": True, "ticker": "NVDA", "xii_score": 86, "h0": 78,
         "prob": (30, 50, 20), "durability": 21, "fatal_flags": 0,
         "verdict": "strong-buy", "review_due": "2026-11-28",
         "cycle_exposure": None, "sovereign_exposure": None,
         "archetype_category": "incumbent-under-threat", "mispricing_source": "x"}
    t.update(over)
    return t


def approved(ticker="NVDA", feeds=3):
    return [{"ticker": ticker, "side": "buy", "feed_count": feeds,
             "feeds": ["congress", "insider", "institutional", "analyst"][:feeds],
             "notional_usd": 50.0, "order_type": "market", "issuer": ticker}]


def enrich_with(th, decision=None):
    def fn(ticker, account_value):
        return {"thesis": th, "decision": decision or {"found": False},
                "portfolio": {"checked": False}, "research": {}, "events_8k": []}
    return fn


class ParsingTest(unittest.TestCase):
    def test_bands_parsed_from_index_meta(self):
        th = enrichment.stocknews_thesis("NVDA", tree_text=TREE)
        self.assertEqual(th["cycle_exposure"], "ai-capex-high")
        self.assertEqual(th["sovereign_exposure"], "sovereign-impaired")

    def test_missing_bands_are_none(self):
        th = enrichment.stocknews_thesis("X", tree_text=TREE.replace(
            "cycle_exposure: ai-capex-high\nsovereign_exposure: sovereign-impaired\n", ""))
        self.assertIsNone(th["cycle_exposure"])
        self.assertIsNone(th["sovereign_exposure"])

    def test_free_text_band_takes_leading_token(self):
        self.assertEqual(enrichment._parse_band(
            "uncorrelated — consumer-discretionary, not AI-capex.", enrichment.CYCLE_BANDS),
            "uncorrelated")
        self.assertIsNone(enrichment._parse_band("something-else", enrichment.CYCLE_BANDS))

    def test_latest_decision_row_wins_and_lowercases(self):
        d = enrichment.parse_latest_decision(DECISIONS)
        self.assertTrue(d["found"])
        self.assertEqual(d["action"], "wait")
        self.assertEqual(d["date"], "2026-08-01")
        self.assertEqual(d["size_reason"], "too rich")

    def test_empty_decision_file(self):
        self.assertFalse(enrichment.parse_latest_decision("")["found"])
        self.assertFalse(enrichment.stocknews_decision("X", text="")["found"])


class DecisionGateTest(unittest.TestCase):
    def test_wait_blocks(self):
        kept, rej = executor.gate_on_context(
            approved(), gr(), 500.0,
            enrich_with(thesis(), {"found": True, "action": "wait", "date": "2026-08-01"}),
            today=TODAY)
        self.assertEqual(kept, [])
        self.assertIn("decision 'wait'", rej[0]["reason"])

    def test_watch_passes(self):
        kept, rej = executor.gate_on_context(
            approved(), gr(), 500.0,
            enrich_with(thesis(), {"found": True, "action": "watch", "date": "2026-08-01"}),
            today=TODAY)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["decision"]["action"], "watch")

    def test_no_journal_passes(self):
        kept, _ = executor.gate_on_context(approved(), gr(), 500.0,
                                           enrich_with(thesis()), today=TODAY)
        self.assertEqual(len(kept), 1)

    def test_block_list_configurable(self):
        kept, _ = executor.gate_on_context(
            approved(), gr(block_decision_actions=[]), 500.0,
            enrich_with(thesis(), {"found": True, "action": "skip"}), today=TODAY)
        self.assertEqual(len(kept), 1)


class SovereignGateTest(unittest.TestCase):
    def test_impaired_blocks(self):
        kept, rej = executor.gate_on_context(
            approved(), gr(), 500.0,
            enrich_with(thesis(sovereign_exposure="sovereign-impaired")), today=TODAY)
        self.assertEqual(kept, [])
        self.assertIn("sovereign-impaired", rej[0]["reason"])

    def test_other_bands_pass(self):
        for b in ("sovereign-insulated", "sovereign-mixed", "sovereign-exposed", None):
            kept, _ = executor.gate_on_context(
                approved(), gr(), 500.0, enrich_with(thesis(sovereign_exposure=b)), today=TODAY)
            self.assertEqual(len(kept), 1, b)


class RegimeGateTest(unittest.TestCase):
    REGIME = {"active": True, "bands": ["ai-capex-high"], "extra_feeds": 1,
              "cap_conviction": "medium"}

    def test_high_band_needs_extra_feed(self):
        kept, rej = executor.gate_on_context(
            approved(feeds=3), gr(regime_gate=self.REGIME), 500.0,
            enrich_with(thesis(cycle_exposure="ai-capex-high")), today=TODAY)
        self.assertEqual(kept, [])
        self.assertIn("regime gate", rej[0]["reason"])
        self.assertIn(">=4", rej[0]["reason"])

    def test_high_band_with_extra_feed_passes_capped_at_medium(self):
        kept, _ = executor.gate_on_context(
            approved(feeds=4), gr(regime_gate=self.REGIME), 500.0,
            enrich_with(thesis(cycle_exposure="ai-capex-high")), today=TODAY)
        self.assertEqual(len(kept), 1)
        # thesis() scores high (xii 86 + h0 78 + dur 21 + favorable) -> capped
        self.assertEqual(kept[0]["assessment"]["conviction"], "medium")
        self.assertTrue(any("regime gate caps" in r for r in kept[0]["assessment"]["reasons"]))

    def test_other_bands_unaffected(self):
        for b in ("ai-capex-mid-s-curve", "ai-capex-low", "uncorrelated", None):
            kept, _ = executor.gate_on_context(
                approved(feeds=3), gr(regime_gate=self.REGIME), 500.0,
                enrich_with(thesis(cycle_exposure=b)), today=TODAY)
            self.assertEqual(len(kept), 1, b)
            self.assertEqual(kept[0]["assessment"]["conviction"], "high", b)

    def test_inactive_gate_is_a_noop(self):
        kept, _ = executor.gate_on_context(
            approved(feeds=3), gr(regime_gate=dict(self.REGIME, active=False)), 500.0,
            enrich_with(thesis(cycle_exposure="ai-capex-high")), today=TODAY)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["assessment"]["conviction"], "high")

    def test_cap_still_blocked_by_min_conviction_when_below(self):
        # cap at 'low' -> below min_conviction medium -> rejected downstream
        kept, rej = executor.gate_on_context(
            approved(feeds=4), gr(regime_gate=dict(self.REGIME, cap_conviction="low")), 500.0,
            enrich_with(thesis(cycle_exposure="ai-capex-high")), today=TODAY)
        self.assertEqual(kept, [])
        self.assertIn("conviction low", rej[0]["reason"])


class AckAndLogTest(unittest.TestCase):
    def test_ack_label_shows_cycle_and_journal(self):
        r = {"thesis": thesis(cycle_exposure="ai-capex-high"),
             "decision": {"found": True, "action": "watch", "date": "2026-08-01"},
             "portfolio": {"checked": False}, "assessment": {"conviction": "medium"}}
        label = executor._ack_label(r)
        self.assertIn("cycle ai-capex-high", label)
        self.assertIn("journal watch", label)

    def test_proposals_log_carries_new_fields(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "log.json"
            results = [{"ticker": "NVDA", "side": "buy", "notional_usd": 50.0,
                        "status": "proposed", "ts": NOW.isoformat(),
                        "feeds": ["congress"], "feed_count": 4,
                        "thesis": thesis(cycle_exposure="ai-capex-high"),
                        "decision": {"found": True, "action": "watch"},
                        "assessment": {"conviction": "medium"}, "portfolio": {}}]
            executor.record_proposals_log(results, 500.0, NOW, path=path,
                                          price_fn=lambda t: 230.0)
            row = json.loads(path.read_text())["proposals"][0]
            self.assertEqual(row["stocknews"]["cycle_exposure"], "ai-capex-high")
            self.assertEqual(row["stocknews"]["decision"], "watch")


if __name__ == "__main__":
    unittest.main()
