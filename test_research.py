#!/usr/bin/env python3
"""Tests for research.py pure helpers — daily rotation slice, metric extraction,
and thesis-vs-research flag detection. Stdlib-only, no network."""

import os
import unittest
from datetime import date

os.environ.setdefault("SEC_USER_AGENT", "test test@example.com")

import research  # noqa: E402

TODAY = date(2026, 6, 27)


class DailySliceTest(unittest.TestCase):
    def test_chunks_and_rotates(self):
        uni = [f"T{i:02d}" for i in range(20)]  # 20 names, per_day 7 -> 3 groups
        g0 = research.daily_slice(uni, 0, 7)
        g1 = research.daily_slice(uni, 1, 7)
        g2 = research.daily_slice(uni, 2, 7)
        self.assertEqual(len(g0), 7)
        self.assertEqual(len(g2), 6)  # last group is the remainder
        # Groups are disjoint and cover the whole sorted universe.
        self.assertEqual(sorted(g0 + g1 + g2), sorted(uni))

    def test_rotation_wraps(self):
        uni = [f"T{i}" for i in range(10)]  # 2 groups of 5
        self.assertEqual(research.daily_slice(uni, 0, 5),
                         research.daily_slice(uni, 2, 5))  # wraps every 2 days

    def test_empty_and_bad_inputs(self):
        self.assertEqual(research.daily_slice([], 3, 7), [])
        self.assertEqual(research.daily_slice(["A"], 0, 0), [])

    def test_dedup_and_sort(self):
        self.assertEqual(research.daily_slice(["B", "A", "A"], 0, 7), ["A", "B"])


class PickMetricsTest(unittest.TestCase):
    def test_extracts_known_fields(self):
        payload = {"metric": {"peTTM": 28.4, "netProfitMarginTTM": 21.1,
                              "junkField": 9}}
        out = research.pick_metrics(payload)
        self.assertEqual(out["P/E"], 28.4)
        self.assertEqual(out["net margin %"], 21.1)
        self.assertNotIn("junkField", out)

    def test_handles_missing_and_garbage(self):
        self.assertEqual(research.pick_metrics(None), {})
        self.assertEqual(research.pick_metrics({"metric": {"peTTM": "n/a"}}), {})


class ComputeFlagsTest(unittest.TestCase):
    def _thesis(self, **over):
        t = {"found": True, "verdict": "strong-buy", "h0": 80,
             "review_due": "2026-12-31"}
        t.update(over)
        return t

    def test_no_thesis_flag(self):
        flags = research.compute_flags({"found": False}, {}, today=TODAY)
        self.assertEqual(flags, ["no StockNews thesis on file"])

    def test_stale_thesis_flag(self):
        flags = research.compute_flags(
            self._thesis(review_due="2026-06-09"), {}, today=TODAY)
        self.assertTrue(any("stale" in f for f in flags))

    def test_analyst_divergence_flag(self):
        flags = research.compute_flags(
            self._thesis(), {"analyst_upgrade": False}, today=TODAY)
        self.assertTrue(any("analyst" in f for f in flags))

    def test_earnings_miss_under_high_h0(self):
        flags = research.compute_flags(
            self._thesis(h0=82), {"earnings_beat": False}, today=TODAY)
        self.assertTrue(any("earnings" in f for f in flags))

    def test_clean_thesis_no_flags(self):
        flags = research.compute_flags(
            self._thesis(), {"analyst_upgrade": True, "earnings_beat": True},
            today=TODAY)
        self.assertEqual(flags, [])


if __name__ == "__main__":
    unittest.main()
