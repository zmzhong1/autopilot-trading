"""Tests for heartbeat.live_stale — the live-execution liveness check."""

import unittest
from datetime import datetime, timezone

import heartbeat

NOW = datetime(2026, 9, 28, 13, 0, tzinfo=timezone.utc)
LIVE = {"enabled": True, "mode": "live"}
FRESH_SNAP = {"ts": "2026-09-21T14:41:00+00:00"}


def order(state, ts="2026-09-21T14:41:52+00:00", **kw):
    return dict({"ticker": "AAPL", "date": ts[:10], "state": state, "ts": ts}, **kw)


class LiveStaleTest(unittest.TestCase):
    def test_not_live_is_silent(self):
        for gr in ({"enabled": False, "mode": "live"}, {"enabled": True, "mode": "propose"}):
            self.assertEqual(heartbeat.live_stale(gr, {}, [order("queued")], NOW), [])

    def test_fresh_snapshot_no_orders_is_healthy(self):
        self.assertEqual(heartbeat.live_stale(LIVE, FRESH_SNAP, [], NOW), [])

    def test_missing_snapshot_flags(self):
        notes = heartbeat.live_stale(LIVE, {}, [], NOW)
        self.assertEqual(len(notes), 1)
        self.assertIn("never", notes[0])

    def test_snapshot_older_than_a_missed_week_flags(self):
        # 2026-09-07 snapshot seen on 2026-09-28: the 09-14 and 09-21 runs never committed
        notes = heartbeat.live_stale(LIVE, {"ts": "2026-09-07T14:41:38+00:00"}, [], NOW)
        self.assertEqual(len(notes), 1)
        self.assertIn("2026-09-07", notes[0])

    def test_snapshot_one_week_old_is_ok(self):
        # heartbeat can run before that Monday's routine; 7d old must not flag
        self.assertEqual(heartbeat.live_stale(LIVE, {"ts": "2026-09-21T13:00:00+00:00"}, [], NOW), [])

    def test_old_open_order_flags(self):
        notes = heartbeat.live_stale(LIVE, FRESH_SNAP, [order("queued", "2026-09-07T14:41:52+00:00")], NOW)
        self.assertEqual(len(notes), 1)
        self.assertIn("AAPL", notes[0])
        self.assertIn("queued", notes[0])

    def test_recent_open_order_is_ok(self):
        self.assertEqual(
            heartbeat.live_stale(LIVE, FRESH_SNAP, [order("queued", "2026-09-28T12:00:00+00:00")], NOW), [])

    def test_terminal_orders_never_flag(self):
        old = "2026-09-07T14:41:52+00:00"
        orders = [order("filled", old), order("cancelled", old), order("rejected", old)]
        self.assertEqual(heartbeat.live_stale(LIVE, FRESH_SNAP, orders, NOW), [])

    def test_updated_ts_takes_precedence(self):
        o = order("confirmed", "2026-09-07T14:41:52+00:00", updated_ts="2026-09-28T10:00:00+00:00")
        self.assertEqual(heartbeat.live_stale(LIVE, FRESH_SNAP, [o], NOW), [])


if __name__ == "__main__":
    unittest.main()
