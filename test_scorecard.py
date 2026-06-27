#!/usr/bin/env python3
"""Tests for scorecard.compute_scorecard (pure mark-to-market) and the executor
helpers that feed it: _size_label and record_proposals_log. Stdlib-only, no
network — prices are injected.
"""

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

os.environ.setdefault("SEC_USER_AGENT", "test test@example.com")

import scorecard  # noqa: E402
import executor  # noqa: E402

TODAY = date(2026, 6, 27)


def row(ticker, entry, dt="2026-06-20", side="buy", feeds=("congress", "insider", "institutional")):
    return {"ticker": ticker, "side": side, "date": dt, "entry_price": entry,
            "feeds": list(feeds)}


class ComputeScorecardTest(unittest.TestCase):
    def test_basic_buy_return(self):
        card = scorecard.compute_scorecard(
            [row("AAPL", 100.0)], {"AAPL": 110.0}, today=TODAY)
        self.assertEqual(card["n_scored"], 1)
        self.assertEqual(card["scored"][0]["return_pct"], 10.0)
        self.assertEqual(card["scored"][0]["hold_days"], 7)
        self.assertEqual(card["hit_rate"], 100.0)
        self.assertEqual(card["avg_return"], 10.0)

    def test_sell_side_inverts_sign(self):
        # A 'sell' signal profits when price falls.
        card = scorecard.compute_scorecard(
            [row("NVDA", 100.0, side="sell")], {"NVDA": 90.0}, today=TODAY)
        self.assertEqual(card["scored"][0]["return_pct"], 10.0)

    def test_rows_without_price_are_counted_but_not_scored(self):
        rows = [row("AAPL", 100.0), row("ZZZZ", 50.0)]
        card = scorecard.compute_scorecard(rows, {"AAPL": 105.0}, today=TODAY)
        self.assertEqual(card["n"], 2)
        self.assertEqual(card["n_scored"], 1)  # ZZZZ has no current price

    def test_rows_without_entry_price_skipped(self):
        rows = [{"ticker": "AAPL", "side": "buy", "date": "2026-06-20",
                 "entry_price": None}]
        card = scorecard.compute_scorecard(rows, {"AAPL": 105.0}, today=TODAY)
        self.assertEqual(card["n_scored"], 0)

    def test_nonpositive_entry_skipped(self):
        card = scorecard.compute_scorecard(
            [row("AAPL", 0.0)], {"AAPL": 105.0}, today=TODAY)
        self.assertEqual(card["n_scored"], 0)

    def test_hit_rate_and_best_worst(self):
        rows = [row("A", 100.0), row("B", 100.0), row("C", 100.0)]
        prices = {"A": 120.0, "B": 90.0, "C": 100.0}  # +20%, -10%, 0%
        card = scorecard.compute_scorecard(rows, prices, today=TODAY)
        self.assertEqual(card["n_scored"], 3)
        self.assertAlmostEqual(card["hit_rate"], 33.3, places=1)  # only A > 0
        self.assertAlmostEqual(card["avg_return"], 3.33, places=2)
        self.assertEqual(card["best"]["ticker"], "A")
        self.assertEqual(card["worst"]["ticker"], "B")

    def test_empty_is_safe(self):
        card = scorecard.compute_scorecard([], {}, today=TODAY)
        self.assertEqual(card["n_scored"], 0)
        self.assertEqual(card["hit_rate"], 0.0)
        self.assertIsNone(card["best"])

    def test_build_embed_renders_without_dollars(self):
        card = scorecard.compute_scorecard([row("AAPL", 100.0)], {"AAPL": 110.0},
                                           today=TODAY)
        emb = scorecard.build_embed(card)
        blob = json.dumps(emb)
        self.assertIn("AAPL", blob)
        self.assertNotIn("$", blob)  # privacy: scorecard never shows dollars


class SizeLabelTest(unittest.TestCase):
    def _gr(self, mode):
        g = dict(executor.DEFAULT_GUARDRAILS)
        g["share_size_display"] = mode
        return g

    def test_pct_hides_dollars(self):
        self.assertEqual(executor._size_label(50.0, 500.0, self._gr("pct")),
                         "10.0% acct")

    def test_usd_shows_dollars(self):
        self.assertEqual(executor._size_label(50.0, 500.0, self._gr("usd")),
                         "$50.00")

    def test_none_is_empty(self):
        self.assertEqual(executor._size_label(50.0, 500.0, self._gr("none")), "")

    def test_pct_safe_when_account_zero(self):
        self.assertEqual(executor._size_label(50.0, 0.0, self._gr("pct")),
                         "0.0% acct")


class RecordProposalsLogTest(unittest.TestCase):
    def test_logs_tracked_statuses_with_injected_entry_price(self):
        results = [
            {"ticker": "AAPL", "side": "buy", "notional_usd": 50.0,
             "status": "proposed", "ts": "2026-06-27T14:00:00+00:00",
             "feeds": ["congress", "insider", "institutional"], "feed_count": 3},
            {"ticker": "MSFT", "side": "buy", "notional_usd": 50.0,
             "status": "live_error", "ts": "...", "feeds": [], "feed_count": 3},
        ]
        p = Path(tempfile.mkdtemp()) / "proposals_log.json"
        now = datetime(2026, 6, 27, 14, 0, tzinfo=timezone.utc)
        executor.record_proposals_log(results, 500.0, now, path=p,
                                      price_fn=lambda t: 200.0)
        log = json.loads(p.read_text())
        self.assertEqual(len(log["proposals"]), 1)  # live_error not tracked
        entry = log["proposals"][0]
        self.assertEqual(entry["ticker"], "AAPL")
        self.assertEqual(entry["entry_price"], 200.0)
        self.assertEqual(entry["size_pct"], 10.0)  # 50/500

    def test_appends_across_runs(self):
        p = Path(tempfile.mkdtemp()) / "proposals_log.json"
        now = datetime(2026, 6, 27, tzinfo=timezone.utc)
        r = [{"ticker": "AAPL", "side": "buy", "notional_usd": 50.0,
              "status": "proposed", "ts": "t", "feeds": [], "feed_count": 3}]
        executor.record_proposals_log(r, 500.0, now, path=p, price_fn=lambda t: 100.0)
        executor.record_proposals_log(r, 500.0, now, path=p, price_fn=lambda t: 100.0)
        log = json.loads(p.read_text())
        self.assertEqual(len(log["proposals"]), 2)


if __name__ == "__main__":
    unittest.main()
