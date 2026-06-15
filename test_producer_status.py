#!/usr/bin/env python3
"""Tests for producer_status — the per-producer liveness ledger the weekly
heartbeat uses to detect a digest producer that has silently stopped posting.

Stdlib-only (unittest), no network. Run:
    python3 -m unittest test_producer_status
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import producer_status as ps


class RecordLoadTest(unittest.TestCase):
    def setUp(self):
        self._orig = ps.STATUS_PATH
        ps.STATUS_PATH = Path(tempfile.mkdtemp()) / "producer_status.json"

    def tearDown(self):
        ps.STATUS_PATH = self._orig

    def test_record_then_load_roundtrip(self):
        ps.record("regime", ok=True)
        ps.record("confluence", ok=False)
        data = ps.load()
        self.assertEqual(set(data), {"regime", "confluence"})
        self.assertTrue(data["regime"]["ok"])
        self.assertFalse(data["confluence"]["ok"])
        self.assertIn("last_run", data["regime"])

    def test_record_upserts_in_place(self):
        ps.record("regime", ok=False)
        ps.record("regime", ok=True)
        self.assertEqual(set(ps.load()), {"regime"})
        self.assertTrue(ps.load()["regime"]["ok"])

    def test_load_missing_returns_empty(self):
        self.assertEqual(ps.load(), {})


class StaleTest(unittest.TestCase):
    NOW = datetime(2026, 6, 15, tzinfo=timezone.utc)
    PRODUCERS = ["regime", "confluence", "discovery"]

    def _status(self, **ages_days):
        return {p: {"last_run": (self.NOW - timedelta(days=d)).isoformat(), "ok": True}
                for p, d in ages_days.items()}

    def test_empty_ledger_yields_no_warnings(self):
        # First run ever — nothing recorded — must not flag every producer.
        self.assertEqual(ps.stale({}, self.PRODUCERS, self.NOW), [])

    def test_fresh_producers_not_stale(self):
        status = self._status(regime=2, confluence=5, discovery=1)
        self.assertEqual(ps.stale(status, self.PRODUCERS, self.NOW, max_age_days=8), [])

    def test_old_producer_flagged(self):
        status = self._status(regime=2, confluence=20, discovery=1)
        self.assertEqual([p for p, _ in ps.stale(status, self.PRODUCERS, self.NOW, 8)],
                         ["confluence"])

    def test_missing_producer_flagged_when_others_present(self):
        status = self._status(regime=2, confluence=5)  # discovery never recorded
        self.assertEqual([p for p, _ in ps.stale(status, self.PRODUCERS, self.NOW, 8)],
                         ["discovery"])

    def test_failed_run_flagged_even_if_recent(self):
        status = {"regime": {"last_run": (self.NOW - timedelta(days=1)).isoformat(),
                             "ok": False}}
        self.assertEqual([p for p, _ in ps.stale(status, ["regime"], self.NOW, 8)],
                         ["regime"])


if __name__ == "__main__":
    unittest.main()
