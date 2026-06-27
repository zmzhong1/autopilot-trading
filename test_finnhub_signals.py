#!/usr/bin/env python3
"""Tests for finnhub_signals pure scoring logic — insider-buy counting, net
analyst-upgrade detection, recent earnings beat. Stdlib-only, no network (the
network wrappers are thin; the logic under test is pure)."""

import os
import unittest

os.environ.setdefault("SEC_USER_AGENT", "test test@example.com")

import finnhub_signals as fh  # noqa: E402


class CountInsiderBuysTest(unittest.TestCase):
    def test_counts_only_open_market_purchases_since(self):
        rows = [
            {"transactionCode": "P", "transactionDate": "2026-06-20", "change": 100},
            {"transactionCode": "P", "transactionDate": "2026-06-21", "change": 50},
            {"transactionCode": "S", "transactionDate": "2026-06-22", "change": -10},  # sale
            {"transactionCode": "P", "transactionDate": "2026-05-01", "change": 10},   # too old
            {"transactionCode": "A", "transactionDate": "2026-06-25", "change": 5},     # grant
        ]
        self.assertEqual(fh.count_insider_buys(rows, "2026-06-15"), 2)

    def test_ignores_nonpositive_change(self):
        rows = [{"transactionCode": "P", "transactionDate": "2026-06-20", "change": 0}]
        self.assertEqual(fh.count_insider_buys(rows, "2026-06-01"), 0)

    def test_empty(self):
        self.assertEqual(fh.count_insider_buys([], "2026-06-01"), 0)
        self.assertEqual(fh.count_insider_buys(None, "2026-06-01"), 0)


class NetUpgradeTest(unittest.TestCase):
    def test_more_bulls_is_upgrade(self):
        rows = [
            {"period": "2026-05-01", "strongBuy": 5, "buy": 10, "hold": 5, "sell": 2, "strongSell": 0},
            {"period": "2026-06-01", "strongBuy": 8, "buy": 10, "hold": 4, "sell": 2, "strongSell": 0},
        ]
        self.assertTrue(fh.is_net_upgrade(rows))

    def test_fewer_bears_is_upgrade(self):
        rows = [
            {"period": "2026-05-01", "strongBuy": 5, "buy": 10, "sell": 4, "strongSell": 1},
            {"period": "2026-06-01", "strongBuy": 5, "buy": 10, "sell": 1, "strongSell": 0},
        ]
        self.assertTrue(fh.is_net_upgrade(rows))

    def test_no_change_is_not_upgrade(self):
        rows = [
            {"period": "2026-05-01", "strongBuy": 5, "buy": 10, "sell": 2, "strongSell": 0},
            {"period": "2026-06-01", "strongBuy": 5, "buy": 10, "sell": 2, "strongSell": 0},
        ]
        self.assertFalse(fh.is_net_upgrade(rows))

    def test_unsorted_input_uses_latest_two(self):
        # API returns newest-first; helper must sort so it compares the right months.
        rows = [
            {"period": "2026-06-01", "strongBuy": 8, "buy": 10, "sell": 2, "strongSell": 0},
            {"period": "2026-05-01", "strongBuy": 5, "buy": 10, "sell": 2, "strongSell": 0},
        ]
        self.assertTrue(fh.is_net_upgrade(rows))

    def test_single_month_not_enough(self):
        self.assertFalse(fh.is_net_upgrade([{"period": "2026-06-01", "buy": 10}]))


class RecentBeatTest(unittest.TestCase):
    def test_positive_surprise_pct(self):
        rows = [{"period": "2026-06-15", "surprisePercent": 4.2}]
        self.assertTrue(fh.recent_beat(rows, "2026-06-01"))

    def test_actual_over_estimate_when_no_pct(self):
        rows = [{"period": "2026-06-15", "actual": 2.1, "estimate": 1.9}]
        self.assertTrue(fh.recent_beat(rows, "2026-06-01"))

    def test_miss_is_false(self):
        rows = [{"period": "2026-06-15", "surprisePercent": -1.0}]
        self.assertFalse(fh.recent_beat(rows, "2026-06-01"))

    def test_old_beat_ignored(self):
        rows = [{"period": "2026-03-15", "surprisePercent": 5.0}]
        self.assertFalse(fh.recent_beat(rows, "2026-06-01"))


class GatherTest(unittest.TestCase):
    def test_no_key_returns_empty(self):
        orig = fh.API_KEY
        fh.API_KEY = ""
        try:
            self.assertEqual(fh.gather(["AAPL", "MSFT"], None), {})
        finally:
            fh.API_KEY = orig


class FinnhubQuoteParseTest(unittest.TestCase):
    def test_parses_current_price(self):
        import prices
        self.assertEqual(prices.parse_finnhub_quote({"c": 224.36, "pc": 220.0}),
                         224.36)

    def test_zero_means_unknown_symbol(self):
        # Finnhub returns c==0 for an unknown symbol -> None so we fall back.
        import prices
        self.assertIsNone(prices.parse_finnhub_quote({"c": 0}))

    def test_garbage_is_none(self):
        import prices
        self.assertIsNone(prices.parse_finnhub_quote(None))
        self.assertIsNone(prices.parse_finnhub_quote({"c": "n/a"}))


if __name__ == "__main__":
    unittest.main()
