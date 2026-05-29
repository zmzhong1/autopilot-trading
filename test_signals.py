#!/usr/bin/env python3
"""Tests for the pure logic in cluster_buys, regime, and confluence.

Stdlib-only, no network. SEC_USER_AGENT is set so the modules import past their
startup guards.
"""

import os
import unittest

os.environ.setdefault("SEC_USER_AGENT", "test test@example.com")

import cluster_buys  # noqa: E402
import regime  # noqa: E402
import confluence  # noqa: E402


# -------------------- cluster_buys.detect_clusters --------------------

class DetectClustersTest(unittest.TestCase):
    def _p(self, insider, acc, value, role="Director", date="2026-05-20"):
        return {"insider": insider, "accession": acc, "value": value,
                "shares": value / 10, "role": role, "filing_date": date}

    def test_cluster_requires_min_distinct_insiders(self):
        data = {
            "BigCo": [self._p("Alice", "a1", 100), self._p("Bob", "a2", 200)],
            "SoloCo": [self._p("Carol", "a3", 500)],  # one insider -> not a cluster
        }
        clusters = cluster_buys.detect_clusters(data, min_insiders=2)
        self.assertEqual([c["company"] for c in clusters], ["BigCo"])
        self.assertEqual(clusters[0]["insider_count"], 2)
        self.assertEqual(clusters[0]["total_value"], 300)
        self.assertCountEqual(clusters[0]["accessions"], ["a1", "a2"])

    def test_same_insider_multiple_filings_count_once(self):
        data = {"BigCo": [self._p("Alice", "a1", 100), self._p("Alice", "a2", 50),
                          self._p("Bob", "a3", 200)]}
        clusters = cluster_buys.detect_clusters(data, min_insiders=2)
        self.assertEqual(clusters[0]["insider_count"], 2, "Alice's two filings = one insider")
        alice = [i for i in clusters[0]["insiders"] if i["insider"] == "Alice"][0]
        self.assertEqual(alice["value"], 150, "Alice's purchases summed")

    def test_ranked_by_insider_count_then_value(self):
        data = {
            "A": [self._p("x", "1", 10), self._p("y", "2", 10)],            # 2 insiders
            "B": [self._p("x", "3", 5), self._p("y", "4", 5), self._p("z", "5", 5)],  # 3
        }
        clusters = cluster_buys.detect_clusters(data, min_insiders=2)
        self.assertEqual([c["company"] for c in clusters], ["B", "A"])


# -------------------- regime.assess_regime --------------------

class AssessRegimeTest(unittest.TestCase):
    def test_calm_when_uptrend_and_low_vol(self):
        r = regime.assess_regime(spx_close=5000, spx_sma200=4500, vix=12,
                                 curve_spread=1.0, hy_spread=3.0)
        self.assertEqual(r["score"], 0)
        self.assertIn("Calm", r["level"])
        self.assertEqual(r["color"], regime.COLOR_CALM)

    def test_stressed_accumulates_risk_points(self):
        r = regime.assess_regime(spx_close=4000, spx_sma200=4500, vix=40,
                                 curve_spread=-0.5, hy_spread=8.0)
        # below 200dma(1) + vix>35(3) + inverted(1) + hy>7(2) = 7
        self.assertEqual(r["score"], 7)
        self.assertIn("Stressed", r["level"])
        self.assertEqual(r["color"], regime.COLOR_STRESSED)

    def test_missing_inputs_are_skipped(self):
        r = regime.assess_regime(spx_close=None, spx_sma200=None, vix=18,
                                 curve_spread=None, hy_spread=None)
        names = [c["name"] for c in r["components"]]
        self.assertEqual(len(names), 1, "only VIX available")
        self.assertIn("VIX", names[0])
        self.assertEqual(r["score"], 0)


# -------------------- confluence.collect_signals + score_confluence --------------------

class ConfluenceTest(unittest.TestCase):
    def test_collect_and_score_across_feeds(self):
        name_to_ticker = {"APPLE": "AAPL", "NVIDIA": "NVDA"}
        sec_hist = [
            {"ts": "2026-05-20T00:00:00+00:00", "filer": "Apple Inc", "form": "4"},
            {"ts": "2026-05-21T00:00:00+00:00", "filer": "Apple Inc", "form": "8-K"},
            {"ts": "2026-05-21T00:00:00+00:00", "filer": "NVIDIA Corp", "form": "4"},
        ]
        congress_hist = [
            {"ts": "2026-05-22T00:00:00+00:00", "issuer": "Apple Inc AAPL:US"},
        ]
        crowded = [{"issuer": "APPLE INC"}]  # institutional via name match
        sig = confluence.collect_signals(sec_hist, congress_hist, name_to_ticker,
                                         {}, crowded=crowded, cutoff=None)
        ranked = confluence.score_confluence(sig, min_feeds=2)
        # AAPL: insider + corporate + congress + institutional = 4 feeds; NVDA: 1.
        self.assertEqual(ranked[0]["ticker"], "AAPL")
        self.assertEqual(ranked[0]["feed_count"], 4)
        self.assertCountEqual(ranked[0]["feeds"],
                              ["congress", "corporate", "insider", "institutional"])
        self.assertEqual([c["ticker"] for c in ranked], ["AAPL"], "NVDA has <2 feeds")

    def test_cutoff_filters_old_entries(self):
        from datetime import datetime, timezone
        name_to_ticker = {"APPLE": "AAPL"}
        sec_hist = [
            {"ts": "2020-01-01T00:00:00+00:00", "filer": "Apple Inc", "form": "4"},
        ]
        congress_hist = [
            {"ts": "2026-05-22T00:00:00+00:00", "issuer": "Apple Inc AAPL:US"},
        ]
        cutoff = datetime(2026, 5, 1, tzinfo=timezone.utc)
        sig = confluence.collect_signals(sec_hist, congress_hist, name_to_ticker,
                                         {}, cutoff=cutoff)
        ranked = confluence.score_confluence(sig, min_feeds=2)
        self.assertEqual(ranked, [], "old insider entry dropped -> only 1 feed left")


if __name__ == "__main__":
    unittest.main()
