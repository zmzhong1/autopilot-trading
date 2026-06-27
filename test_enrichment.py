#!/usr/bin/env python3
"""Tests for enrichment (StockNews INDEX_META parsing + verdict bands + portfolio
context) and executor.gate_on_context (the thesis/portfolio gate that grounds
each trade). Stdlib-only, no network — markdown and HTTP are injected.
"""

import os
import unittest

os.environ.setdefault("SEC_USER_AGENT", "test test@example.com")

import enrichment  # noqa: E402
import executor  # noqa: E402


TREE = """# Apple (AAPL) — Investment Tree v1
<!-- INDEX_META
schema: 2
updated: 2026-05-04
verdicts: 7 ✅ · 7 ⚠️ · 0 ✗ · 1 ⊗
h0: 65%
fatal_flags: 0
xii_score: 85%
archetype: platform-services-transition
review_due: 2026-06-15
-->
body text
"""


class IndexMetaParseTest(unittest.TestCase):
    def test_parses_meta_block(self):
        m = enrichment.parse_index_meta(TREE)
        self.assertEqual(m["xii_score"], "85%")
        self.assertEqual(m["fatal_flags"], "0")
        self.assertEqual(m["h0"], "65%")

    def test_no_block_returns_empty(self):
        self.assertEqual(enrichment.parse_index_meta("no meta here"), {})
        self.assertEqual(enrichment.parse_index_meta(None), {})


class VerdictBandTest(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(enrichment.verdict_band(90), "strong-buy")
        self.assertEqual(enrichment.verdict_band(70), "moderate-buy")
        self.assertEqual(enrichment.verdict_band(50), "wait")
        self.assertEqual(enrichment.verdict_band(30), "avoid")
        self.assertEqual(enrichment.verdict_band(None), "unknown")


class StocknewsThesisTest(unittest.TestCase):
    def test_thesis_from_injected_tree(self):
        th = enrichment.stocknews_thesis("AAPL", tree_text=TREE)
        self.assertTrue(th["found"])
        self.assertEqual(th["xii_score"], 85)
        self.assertEqual(th["fatal_flags"], 0)
        self.assertEqual(th["verdict"], "strong-buy")

    def test_missing_tree(self):
        th = enrichment.stocknews_thesis("ZZZZ", tree_text=None) \
            if False else enrichment.stocknews_thesis("ZZZZ", tree_text="")
        self.assertFalse(th["found"])

    def test_portfolio_unchecked_without_creds(self):
        pf = enrichment.portfolio_context("AAPL")
        self.assertFalse(pf["checked"])

    def test_portfolio_parsed_from_injected_api(self):
        payload = ('{"holdings":[{"ticker":"AAPL","shares":3,"market_value":600}],'
                   '"allocation":{"stock_pct":42},"target_stock_pct":40}')
        pf = enrichment.portfolio_context("AAPL", _opener=lambda url: payload)
        self.assertTrue(pf["checked"])
        self.assertTrue(pf["held"])
        self.assertEqual(pf["value_usd"], 600)


def _prop(ticker):
    return {"ticker": ticker, "side": "buy", "notional_usd": 50.0,
            "order_type": "market", "feed_count": 3, "feeds": ["x", "y", "z"]}


def _gr(**over):
    g = dict(executor.DEFAULT_GUARDRAILS)
    g.update(over)
    return g


def _thesis(xii=90, h0=85, dur=21, prob=(30, 50, 20), fatal=0, found=True,
            verdict="strong-buy", **extra):
    t = {"found": found, "xii_score": xii, "h0": h0, "durability": dur,
         "prob": prob, "fatal_flags": fatal, "verdict": verdict}
    t.update(extra)
    return t


def _enr(thesis, portfolio=None):
    return lambda t, av: {"thesis": thesis, "portfolio": portfolio or {"checked": False}}


class AssessPurchaseTest(unittest.TestCase):
    def test_high_conviction_quality_plus_confidence(self):
        # GOOGL-like: XII 90, H-0 90, durable, favorable asymmetry, fresh.
        a = enrichment.assess_purchase(_thesis(xii=90, h0=90, dur=20, prob=(30, 55, 15)))
        self.assertEqual(a["conviction"], "high")
        self.assertTrue(a["good_purchase"])
        self.assertEqual(a["asymmetry"], "favorable")

    def test_high_xii_low_confidence_unfavorable_is_low(self):
        # COST-like: XII 91 but H-0 60, bull<bear, 'unfavorable' mispricing.
        a = enrichment.assess_purchase(_thesis(
            xii=91, h0=60, dur=23, prob=(18, 52, 30),
            mispricing_source="asymmetric-upside-vs-downside-unfavorable",
            archetype_category="compounder-under-review"))
        self.assertEqual(a["conviction"], "low")
        self.assertFalse(a["good_purchase"])
        self.assertEqual(a["asymmetry"], "unfavorable")

    def test_fatal_flag_is_avoid(self):
        a = enrichment.assess_purchase(_thesis(xii=81, fatal=1))
        self.assertEqual(a["conviction"], "avoid")

    def test_stale_caps_high_to_medium(self):
        from datetime import date
        a = enrichment.assess_purchase(
            _thesis(xii=90, h0=90, dur=21, prob=(30, 50, 20),
                    review_due="2026-06-09"), today=date(2026, 6, 27))
        self.assertTrue(a["stale"])
        self.assertEqual(a["conviction"], "medium")  # capped from high

    def test_missing_thesis(self):
        a = enrichment.assess_purchase({"found": False})
        self.assertEqual(a["conviction"], "none")
        self.assertFalse(a["good_purchase"])


class GateOnContextTest(unittest.TestCase):
    def test_keeps_and_acknowledges_good_thesis(self):
        kept, rej = executor.gate_on_context(
            [_prop("GOOGL")], _gr(), 500.0, _enr(_thesis(xii=90, h0=90)))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["thesis"]["xii_score"], 90)        # acknowledged
        self.assertEqual(kept[0]["assessment"]["conviction"], "high")  # acknowledged
        self.assertEqual(rej, [])

    def test_blocks_missing_thesis_when_required(self):
        kept, rej = executor.gate_on_context(
            [_prop("ZZZZ")], _gr(require_stocknews_thesis=True), 500.0,
            _enr({"found": False}))
        self.assertEqual(kept, [])
        self.assertIn("no StockNews thesis", rej[0]["reason"])

    def test_blocks_on_fatal_flag(self):
        kept, rej = executor.gate_on_context(
            [_prop("F")], _gr(), 500.0, _enr(_thesis(xii=81, fatal=1)))
        self.assertEqual(kept, [])
        self.assertIn("fatal flag", rej[0]["reason"])

    def test_blocks_below_min_xii(self):
        kept, rej = executor.gate_on_context(
            [_prop("SIVE")], _gr(min_xii_score=45), 500.0,
            _enr(_thesis(xii=38, verdict="avoid")))
        self.assertEqual(kept, [])
        self.assertIn("XII 38%", rej[0]["reason"])

    def test_blocks_low_conviction_even_with_high_xii(self):
        # The crux: XII 91% but the research says low conviction -> skipped.
        kept, rej = executor.gate_on_context(
            [_prop("COST")], _gr(min_conviction="medium"), 500.0,
            _enr(_thesis(xii=91, h0=60, dur=23, prob=(18, 52, 30),
                         mispricing_source="...unfavorable")))
        self.assertEqual(kept, [])
        self.assertIn("conviction low", rej[0]["reason"])

    def test_blocks_overconcentrated_holding(self):
        kept, rej = executor.gate_on_context(
            [_prop("NVDA")], _gr(max_existing_position_pct=25), 500.0,
            _enr(_thesis(xii=90, h0=85), {"checked": True, "held": True,
                                          "value_usd": 200}))  # 40% > 25%
        self.assertEqual(kept, [])
        self.assertIn("of acct", rej[0]["reason"])

    def test_allows_held_under_cap(self):
        kept, rej = executor.gate_on_context(
            [_prop("NVDA")], _gr(max_existing_position_pct=25), 500.0,
            _enr(_thesis(xii=90, h0=85), {"checked": True, "held": True,
                                          "value_usd": 50}))  # 10% < 25%
        self.assertEqual(len(kept), 1)


if __name__ == "__main__":
    unittest.main()
