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


class GateOnContextTest(unittest.TestCase):
    def test_keeps_and_acknowledges_good_thesis(self):
        enr = lambda t, av: {"thesis": {"found": True, "xii_score": 85,
                                        "verdict": "strong-buy", "fatal_flags": 0},
                             "portfolio": {"checked": False}}
        kept, rej = executor.gate_on_context([_prop("AAPL")], _gr(), 500.0, enr)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["thesis"]["xii_score"], 85)  # acknowledged
        self.assertEqual(rej, [])

    def test_blocks_missing_thesis_when_required(self):
        enr = lambda t, av: {"thesis": {"found": False}, "portfolio": {"checked": False}}
        kept, rej = executor.gate_on_context([_prop("ZZZZ")],
                                             _gr(require_stocknews_thesis=True), 500.0, enr)
        self.assertEqual(kept, [])
        self.assertIn("no StockNews thesis", rej[0]["reason"])

    def test_blocks_on_fatal_flag(self):
        enr = lambda t, av: {"thesis": {"found": True, "xii_score": 90,
                                        "verdict": "strong-buy", "fatal_flags": 1},
                             "portfolio": {"checked": False}}
        kept, rej = executor.gate_on_context([_prop("AAPL")], _gr(), 500.0, enr)
        self.assertEqual(kept, [])
        self.assertIn("fatal flag", rej[0]["reason"])

    def test_blocks_below_min_xii(self):
        enr = lambda t, av: {"thesis": {"found": True, "xii_score": 38,
                                        "verdict": "avoid", "fatal_flags": 0},
                             "portfolio": {"checked": False}}
        kept, rej = executor.gate_on_context([_prop("PLTR")], _gr(min_xii_score=45),
                                             500.0, enr)
        self.assertEqual(kept, [])
        self.assertIn("XII 38%", rej[0]["reason"])

    def test_blocks_overconcentrated_holding(self):
        enr = lambda t, av: {"thesis": {"found": True, "xii_score": 85,
                                        "verdict": "strong-buy", "fatal_flags": 0},
                             "portfolio": {"checked": True, "held": True,
                                           "value_usd": 200}}
        # 200/500 = 40% > 25% cap -> blocked
        kept, rej = executor.gate_on_context([_prop("NVDA")],
                                             _gr(max_existing_position_pct=25), 500.0, enr)
        self.assertEqual(kept, [])
        self.assertIn("of acct", rej[0]["reason"])

    def test_allows_held_under_cap(self):
        enr = lambda t, av: {"thesis": {"found": True, "xii_score": 85,
                                        "verdict": "strong-buy", "fatal_flags": 0},
                             "portfolio": {"checked": True, "held": True,
                                           "value_usd": 50}}  # 10% < 25%
        kept, rej = executor.gate_on_context([_prop("NVDA")],
                                             _gr(max_existing_position_pct=25), 500.0, enr)
        self.assertEqual(len(kept), 1)


if __name__ == "__main__":
    unittest.main()
