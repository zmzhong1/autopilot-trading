#!/usr/bin/env python3
"""Tests for executor.py — the pure sizing + guardrail logic that decides what
(if anything) gets traded. Stdlib-only, no network.

These cover the safety core: fail-closed allow-list, the kill switch / enabled
gate, daily + deployment caps, side restrictions, the min-feeds floor, order
sizing, and the live-not-wired degradation. The network parts (gather_signals,
Discord) are intentionally not exercised here.
"""

import os
import unittest
from datetime import datetime, timezone

os.environ.setdefault("SEC_USER_AGENT", "test test@example.com")

import executor  # noqa: E402


NOW = datetime(2026, 6, 27, 15, 0, tzinfo=timezone.utc)


def gr(**over):
    """A permissive baseline guardrail set; override per test."""
    g = dict(executor.DEFAULT_GUARDRAILS)
    g.update({
        "enabled": True,
        "mode": "propose",
        "account_value_usd": 1000.0,
        "allow_list": ["AAPL", "MSFT", "NVDA"],
        "block_list": [],
        "allowed_sides": ["buy"],
        "min_signal_feeds": 3,
        "max_notional_per_order_usd": 100.0,
        "max_pct_account_per_order": 0.05,
        "max_orders_per_day": 3,
        "max_total_deployed_pct": 0.50,
    })
    g.update(over)
    return g


def sig(ticker, feed_count=3, feeds=None):
    return {"ticker": ticker, "issuer": f"{ticker} Inc",
            "feed_count": feed_count,
            "feeds": feeds or ["congress", "insider", "institutional"][:feed_count],
            "total": feed_count, "counts": {}}


class SizeOrderTest(unittest.TestCase):
    def test_takes_smaller_of_abs_and_pct(self):
        # pct cap 5% of 1000 = 50 < abs cap 100 -> 50
        self.assertEqual(executor.size_order(1000, gr()), 50.0)
        # pct cap 5% of 5000 = 250 > abs cap 100 -> 100
        self.assertEqual(executor.size_order(5000, gr()), 100.0)

    def test_never_negative(self):
        self.assertEqual(executor.size_order(-100, gr()), 0.0)


class DecideModeTest(unittest.TestCase):
    def test_disabled_collapses_to_propose(self):
        self.assertEqual(executor.decide_mode(gr(enabled=False, mode="live")),
                         "propose")

    def test_enabled_passes_mode_through(self):
        self.assertEqual(executor.decide_mode(gr(enabled=True, mode="paper")),
                         "paper")

    def test_kill_switch_overrides(self):
        executor.KILL = True
        try:
            self.assertEqual(executor.decide_mode(gr(enabled=True, mode="live")),
                             "propose")
        finally:
            executor.KILL = False

    def test_unknown_mode_is_propose(self):
        self.assertEqual(executor.decide_mode(gr(enabled=True, mode="yolo")),
                         "propose")


class EvaluateProposalsTest(unittest.TestCase):
    def test_happy_path_approves_allowed_strong_signal(self):
        approved, rejected = executor.evaluate_proposals(
            [sig("AAPL")], gr(), 1000.0, {}, NOW)
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["ticker"], "AAPL")
        self.assertEqual(approved[0]["side"], "buy")
        self.assertEqual(approved[0]["notional_usd"], 50.0)
        self.assertEqual(rejected, [])

    def test_allow_list_is_fail_closed(self):
        # Empty allow_list => nothing tradable.
        approved, rejected = executor.evaluate_proposals(
            [sig("AAPL")], gr(allow_list=[]), 1000.0, {}, NOW)
        self.assertEqual(approved, [])
        self.assertEqual(rejected[0]["reason"], "not in allow_list")

    def test_ticker_outside_allow_list_rejected(self):
        approved, rejected = executor.evaluate_proposals(
            [sig("TSLA")], gr(), 1000.0, {}, NOW)
        self.assertEqual(approved, [])
        self.assertIn("allow_list", rejected[0]["reason"])

    def test_block_list_overrides_allow(self):
        approved, rejected = executor.evaluate_proposals(
            [sig("AAPL")], gr(block_list=["AAPL"]), 1000.0, {}, NOW)
        self.assertEqual(approved, [])
        self.assertEqual(rejected[0]["reason"], "in block_list")

    def test_min_feeds_floor(self):
        approved, rejected = executor.evaluate_proposals(
            [sig("AAPL", feed_count=2)], gr(min_signal_feeds=3), 1000.0, {}, NOW)
        self.assertEqual(approved, [])
        self.assertIn("feeds", rejected[0]["reason"])

    def test_daily_cap_limits_approvals(self):
        signals = [sig("AAPL"), sig("MSFT"), sig("NVDA")]
        approved, rejected = executor.evaluate_proposals(
            signals, gr(max_orders_per_day=2), 1000.0, {}, NOW)
        self.assertEqual(len(approved), 2)
        self.assertEqual(len(rejected), 1)
        self.assertIn("daily order cap", rejected[0]["reason"])

    def test_daily_cap_counts_prior_orders_today(self):
        state = {"orders": [{"ts": NOW.isoformat(), "ticker": "AAPL",
                             "status": "proposed"}]}
        approved, rejected = executor.evaluate_proposals(
            [sig("MSFT"), sig("NVDA")], gr(max_orders_per_day=2),
            1000.0, state, NOW)
        # One slot already used today -> only one more approved.
        self.assertEqual(len(approved), 1)

    def test_deploy_cap_blocks_when_full(self):
        # 50% of 1000 = 500 already deployed -> no room for more.
        state = {"orders": [{"ts": "2026-01-01T00:00:00+00:00",
                             "status": "paper_filled", "notional_usd": 500}]}
        approved, rejected = executor.evaluate_proposals(
            [sig("AAPL")], gr(), 1000.0, state, NOW)
        self.assertEqual(approved, [])
        self.assertIn("deployed", rejected[0]["reason"])

    def test_deploy_cap_reserves_within_run(self):
        # account 1000, deploy cap 50% = 500, size 50 -> at most 10 fit; daily
        # cap is the tighter limit, so check deploy cap alone with high daily cap.
        signals = [sig(t) for t in ["AAPL", "MSFT", "NVDA"]]
        approved, _ = executor.evaluate_proposals(
            signals, gr(max_orders_per_day=99, max_total_deployed_pct=0.10),
            1000.0, {}, NOW)
        # 10% of 1000 = 100; size 50 -> exactly 2 fit.
        self.assertEqual(len(approved), 2)

    def test_sell_side_not_allowed_by_default(self):
        # Even if a signal existed, the executor only proposes buys; with
        # allowed_sides lacking 'buy' nothing clears.
        approved, rejected = executor.evaluate_proposals(
            [sig("AAPL")], gr(allowed_sides=["sell"]), 1000.0, {}, NOW)
        self.assertEqual(approved, [])
        self.assertIn("not in allowed_sides", rejected[0]["reason"])

    def test_zero_size_rejected(self):
        approved, rejected = executor.evaluate_proposals(
            [sig("AAPL")], gr(max_notional_per_order_usd=0), 1000.0, {}, NOW)
        self.assertEqual(approved, [])
        self.assertIn("$0", rejected[0]["reason"])


class ExecuteTest(unittest.TestCase):
    def _approved(self):
        return [{"ticker": "AAPL", "side": "buy", "notional_usd": 50.0,
                 "order_type": "market", "feed_count": 3, "feeds": ["x"]}]

    def test_propose_mode_marks_proposed_no_execution(self):
        results, notes = executor.execute(self._approved(), "propose", NOW)
        self.assertEqual(results[0]["status"], "proposed")
        self.assertEqual(notes, [])

    def test_paper_mode_simulates_fill(self):
        results, notes = executor.execute(self._approved(), "paper", NOW)
        self.assertEqual(results[0]["status"], "paper_filled")
        self.assertTrue(any("Paper" in n for n in notes))

    def test_live_mode_degrades_when_unwired(self):
        # robinhood_mcp.is_wired() ships False, so live must NOT place orders.
        results, notes = executor.execute(self._approved(), "live", NOW)
        self.assertEqual(results[0]["status"], "proposed_live_unwired")
        self.assertTrue(any("not wired" in n for n in notes))


class StateAccountingTest(unittest.TestCase):
    def test_deployed_notional_counts_only_fills(self):
        state = {"orders": [
            {"status": "proposed", "notional_usd": 100},      # not counted
            {"status": "paper_filled", "notional_usd": 50},   # counted
            {"status": "live_filled", "notional_usd": 25},    # counted
        ]}
        self.assertEqual(executor.deployed_notional(state), 75.0)

    def test_orders_today_matches_utc_day(self):
        state = {"orders": [
            {"ts": NOW.isoformat()},
            {"ts": "2026-06-26T23:59:00+00:00"},  # prior day
        ]}
        self.assertEqual(executor.orders_today(state, NOW), 1)


if __name__ == "__main__":
    unittest.main()
