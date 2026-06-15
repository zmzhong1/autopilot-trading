#!/usr/bin/env python3
"""Regression tests for congress_watcher: transient-failure resilience.

Capitol Trades intermittently rate-limits (HTTP 429) the GitHub Actions runner;
a throttle must not crash the workflow. These tests verify retry/backoff on the
fetch, and the soft-fail path that skips a run (exit 0) while escalating to
Discord if failures persist.

Stdlib-only (unittest), no network — HTTP and Discord calls are stubbed. Run:
    python3 -m unittest test_congress_watcher
"""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path

os.environ["DRY_RUN"] = "1"  # alert() returns True without hitting Discord

import congress_watcher as cw  # noqa: E402  (must follow env setup)


def _http_error(code, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError("https://capitoltrades.test", code, "err", headers, None)


class RetryAfterTest(unittest.TestCase):
    def test_numeric_header_honored_and_capped(self):
        self.assertEqual(cw._retry_after_seconds(_http_error(429, "7"), 0), 7)
        self.assertEqual(
            cw._retry_after_seconds(_http_error(429, "9999"), 0), cw.BACKOFF_CAP_SEC)

    def test_missing_or_date_header_falls_back_to_backoff(self):
        self.assertEqual(
            cw._retry_after_seconds(_http_error(429), 0),
            min(cw.BACKOFF_BASE_SEC, cw.BACKOFF_CAP_SEC))
        # HTTP-date Retry-After is not parsed -> exponential backoff for attempt 1.
        self.assertEqual(
            cw._retry_after_seconds(_http_error(429, "Wed, 21 Oct 2026 07:28:00 GMT"), 1),
            min(cw.BACKOFF_BASE_SEC * 2, cw.BACKOFF_CAP_SEC))


class FetchRetryTest(unittest.TestCase):
    def setUp(self):
        self._orig_urlopen = cw.urllib.request.urlopen
        self._orig_sleep = cw.time.sleep
        cw.time.sleep = lambda _s: None  # no real backoff waits
        self.calls = 0

    def tearDown(self):
        cw.urllib.request.urlopen = self._orig_urlopen
        cw.time.sleep = self._orig_sleep

    def test_retries_then_succeeds(self):
        def flaky(req, timeout=30):
            self.calls += 1
            if self.calls == 1:
                raise _http_error(429, "0")
            return io.BytesIO(b"<html>ok</html>")
        cw.urllib.request.urlopen = flaky
        self.assertEqual(cw.fetch_trades_html(96, retries=3), "<html>ok</html>")
        self.assertEqual(self.calls, 2)

    def test_exhaustion_reraises(self):
        def always_429(req, timeout=30):
            self.calls += 1
            raise _http_error(429, "0")
        cw.urllib.request.urlopen = always_429
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            cw.fetch_trades_html(96, retries=3)
        self.assertEqual(ctx.exception.code, 429)
        self.assertEqual(self.calls, 3)

    def test_non_retryable_raises_immediately(self):
        def forbidden(req, timeout=30):
            self.calls += 1
            raise _http_error(403)
        cw.urllib.request.urlopen = forbidden
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            cw.fetch_trades_html(96, retries=3)
        self.assertEqual(ctx.exception.code, 403)
        self.assertEqual(self.calls, 1)


class SoftFailTest(unittest.TestCase):
    PATCHED = ("STATE_PATH", "fetch_trades_html", "alert", "ESCALATE_AFTER_FAILURES",
               "parse_trades", "matches_watchlist")

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp()) / "state.json"
        self._orig = {k: getattr(cw, k) for k in self.PATCHED}
        cw.STATE_PATH = self._tmp
        cw.ESCALATE_AFTER_FAILURES = 3
        self.alerts = []
        cw.alert = lambda *a, **k: self.alerts.append(a) or True

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(cw, k, v)

    def _seed(self, data):
        self._tmp.write_text(json.dumps(data), encoding="utf-8")

    def test_fetch_failure_soft_fails_and_escalates_on_threshold(self):
        def boom(page_size, retries=cw.FETCH_RETRIES):
            raise _http_error(429, "0")
        cw.fetch_trades_html = boom
        self._seed({"first_run_done": True, "seen_trade_ids": [], "alert_history": []})

        rcs = [cw.main() for _ in range(cw.ESCALATE_AFTER_FAILURES)]
        self.assertEqual(rcs, [0, 0, 0], "soft-fail returns 0, never crashes the workflow")
        st = json.loads(self._tmp.read_text(encoding="utf-8"))
        self.assertEqual(st["consecutive_fetch_failures"], 3)
        self.assertIn("HTTP 429", st["last_fetch_error"])
        self.assertEqual(len(self.alerts), 1, "Discord pinged once, at the threshold")

    def test_success_resets_failure_streak(self):
        cw.fetch_trades_html = lambda page_size, retries=cw.FETCH_RETRIES: "<html>fresh</html>"
        cw.parse_trades = lambda html: [{"trade_id": "1", "politician": "Nobody"}]
        cw.matches_watchlist = lambda *a: False
        self._seed({"first_run_done": True, "seen_trade_ids": [], "alert_history": [],
                    "consecutive_fetch_failures": 5, "last_fetch_error": "stale"})

        self.assertEqual(cw.main(), 0)
        st = json.loads(self._tmp.read_text(encoding="utf-8"))
        self.assertEqual(st["consecutive_fetch_failures"], 0)
        self.assertNotIn("last_fetch_error", st)


class BuildAlertClipTest(unittest.TestCase):
    """A malformed scrape that stuffs a giant blob into one cell must not produce
    an embed Discord rejects (>1024-char field / >256-char title) — that would
    wedge the trade forever (never marked seen, re-rejected every run)."""

    def _trade(self, **over):
        t = {"trade_id": "1", "politician": "Jane Doe", "issuer": "ACME INC",
             "trade_type": "buy", "owner": "Self", "price": "$100",
             "size_range": "$1K–$15K", "tx_date": "2026-06-01",
             "pub_time": "2026-06-10", "days_lag": "9"}
        t.update(over)
        return t

    def test_oversized_cell_is_clipped_to_field_limit(self):
        embed, _ = cw.build_alert(self._trade(size_range="X" * 5000))
        self.assertTrue(all(len(f["value"]) <= 1024 for f in embed["fields"]))
        self.assertLessEqual(len(embed["title"]), 256)

    def test_normal_values_pass_through_unchanged(self):
        embed, _ = cw.build_alert(self._trade())
        size = [f for f in embed["fields"] if f["name"] == "Size"][0]
        self.assertEqual(size["value"], "$1K–$15K")

    def test_empty_cell_falls_back_to_dash(self):
        embed, _ = cw.build_alert(self._trade(owner=""))
        owner = [f for f in embed["fields"] if f["name"] == "Owner"][0]
        self.assertEqual(owner["value"], "—")


if __name__ == "__main__":
    unittest.main()
