#!/usr/bin/env python3
"""Regression tests for congress_watcher.

Covers transient-failure resilience (retry/backoff + soft-fail), the kadoa /
FMP source parsing and normalization, the source-agnostic synthetic id, the
kadoa-primary -> FMP-fallback orchestration, and the one-time reseed on a
source change.

Stdlib-only (unittest), no network — HTTP and Discord calls are stubbed. Run:
    python3 -m unittest test_congress_watcher
"""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import date
from pathlib import Path

os.environ["DRY_RUN"] = "1"  # alert() returns True without hitting Discord

import congress_watcher as cw  # noqa: E402  (must follow env setup)


def _http_error(code, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError("https://feed.test", code, "err", headers, None)


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


class FetchJsonRetryTest(unittest.TestCase):
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
            return io.BytesIO(json.dumps([{"ok": True}]).encode())
        cw.urllib.request.urlopen = flaky
        self.assertEqual(cw.fetch_json("https://feed.test", retries=3), [{"ok": True}])
        self.assertEqual(self.calls, 2)

    def test_exhaustion_reraises(self):
        def always_429(req, timeout=30):
            self.calls += 1
            raise _http_error(429, "0")
        cw.urllib.request.urlopen = always_429
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            cw.fetch_json("https://feed.test", retries=3)
        self.assertEqual(ctx.exception.code, 429)
        self.assertEqual(self.calls, 3)

    def test_non_retryable_raises_immediately(self):
        def forbidden(req, timeout=30):
            self.calls += 1
            raise _http_error(403)
        cw.urllib.request.urlopen = forbidden
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            cw.fetch_json("https://feed.test", retries=3)
        self.assertEqual(ctx.exception.code, 403)
        self.assertEqual(self.calls, 1)


class NormalizeTradeTypeTest(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(cw._normalize_trade_type("Purchase"), "Buy")
        self.assertEqual(cw._normalize_trade_type("Sale (Full)"), "Sell")
        self.assertEqual(cw._normalize_trade_type("Sale (Partial)"), "Sell")
        self.assertEqual(cw._normalize_trade_type("Exchange"), "Exchange")
        self.assertEqual(cw._normalize_trade_type(""), "—")
        # The result must be buy/sell-detectable the way build_alert keys off it.
        self.assertIn("buy", cw._normalize_trade_type("Purchase").lower())
        self.assertIn("sell", cw._normalize_trade_type("Sale (Partial)").lower())


# A single Pelosi NVDA purchase, expressed in each source's native shape.
KADOA_PELOSI = {
    "id": "senate_abc_t0", "filer_name": "Nancy Pelosi", "chamber": "house",
    "branch": "congress", "ticker": "NVDA", "asset_name": "NVIDIA Corp",
    "transaction_type": "Purchase", "amount_range_label": "$1,001 - $15,000",
    "transaction_date": "2026-06-01", "filing_date": "2026-06-10",
    "days_to_file": 9, "owner": "Spouse", "doc_url": "https://efd/ptr/abc"}
FMP_PELOSI = {
    "firstName": "Nancy", "lastName": "Pelosi", "symbol": "NVDA",
    "assetDescription": "NVIDIA Corp", "type": "Purchase",
    "amount": "$1,001-$15,000", "transactionDate": "2026-06-01",
    "dateRecieved": "2026-06-10", "owner": "Spouse", "link": "https://fmp/x"}


class ParseKadoaTest(unittest.TestCase):
    def test_maps_fields_and_drops_executive_rows(self):
        rows = [KADOA_PELOSI, {"id": "e1", "filer_name": "An Official",
                               "chamber": None, "ticker": "AAPL",
                               "transaction_type": "Sale (Full)"}]
        trades = cw.parse_kadoa(rows)
        self.assertEqual(len(trades), 1, "executive-branch (chamber null) row dropped")
        t = trades[0]
        self.assertEqual(t["politician"], "Nancy Pelosi")
        self.assertEqual(t["ticker"], "NVDA")
        self.assertEqual(t["issuer"], "NVIDIA Corp NVDA")
        self.assertEqual(t["trade_type"], "Buy")
        self.assertEqual(t["owner"], "Spouse")
        self.assertEqual(t["doc_url"], "https://efd/ptr/abc")
        self.assertTrue(t["trade_id"].startswith("c"))


class ParseFmpTest(unittest.TestCase):
    def test_maps_split_name_and_misspelled_date(self):
        t = cw.parse_fmp([FMP_PELOSI], "house")[0]
        self.assertEqual(t["politician"], "Nancy Pelosi")
        self.assertEqual(t["ticker"], "NVDA")
        self.assertEqual(t["trade_type"], "Buy")
        self.assertEqual(t["pub_time"], "2026-06-10")  # from 'dateRecieved'
        self.assertEqual(t["doc_url"], "https://fmp/x")


class SyntheticIdTest(unittest.TestCase):
    def test_same_trade_same_id_across_sources(self):
        # kadoa "Nancy Pelosi" + "$1,001 - $15,000" vs FMP split name + "$1,001-$15,000"
        # must collapse to one id (name-key + whitespace-insensitive amount).
        k = cw.parse_kadoa([KADOA_PELOSI])[0]
        f = cw.parse_fmp([FMP_PELOSI], "house")[0]
        self.assertEqual(k["trade_id"], f["trade_id"])

    def test_different_trades_differ(self):
        k = cw.parse_kadoa([KADOA_PELOSI])[0]
        other = dict(KADOA_PELOSI, transaction_type="Sale (Full)")
        self.assertNotEqual(k["trade_id"], cw.parse_kadoa([other])[0]["trade_id"])


class IsStaleTest(unittest.TestCase):
    TODAY = date(2026, 6, 15)

    def test_fresh_is_not_stale(self):
        self.assertFalse(cw.is_stale([{"pub_time": "2026-06-14"}], self.TODAY, 4))

    def test_old_is_stale(self):
        self.assertTrue(cw.is_stale([{"pub_time": "2026-06-01"}], self.TODAY, 4))

    def test_missing_or_empty_is_stale(self):
        self.assertTrue(cw.is_stale([], self.TODAY))
        self.assertTrue(cw.is_stale([{"pub_time": ""}], self.TODAY))


class FetchOrchestrationTest(unittest.TestCase):
    TODAY = date(2026, 6, 15)
    PATCHED = ("fetch_kadoa_trades", "fetch_fmp_trades")

    def setUp(self):
        self._orig = {k: getattr(cw, k) for k in self.PATCHED}

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(cw, k, v)

    def test_fresh_kadoa_is_used(self):
        cw.fetch_kadoa_trades = lambda: [{"pub_time": "2026-06-14", "trade_id": "c1"}]
        cw.fetch_fmp_trades = lambda: self.fail("FMP must not be called when kadoa is fresh")
        trades, source = cw.fetch_congress_trades(today=self.TODAY)
        self.assertEqual(source, "kadoa")

    def test_stale_kadoa_falls_back_to_fmp(self):
        cw.fetch_kadoa_trades = lambda: [{"pub_time": "2026-01-01", "trade_id": "c1"}]
        cw.fetch_fmp_trades = lambda: [{"pub_time": "2026-06-14", "trade_id": "c2"}]
        trades, source = cw.fetch_congress_trades(today=self.TODAY)
        self.assertEqual(source, "fmp")

    def test_kadoa_error_falls_back_to_fmp(self):
        def boom():
            raise RuntimeError("CDN down")
        cw.fetch_kadoa_trades = boom
        cw.fetch_fmp_trades = lambda: [{"pub_time": "2026-06-14", "trade_id": "c2"}]
        _, source = cw.fetch_congress_trades(today=self.TODAY)
        self.assertEqual(source, "fmp")

    def test_both_unavailable_raises(self):
        cw.fetch_kadoa_trades = lambda: []

        def no_key():
            raise RuntimeError("FMP_API_KEY not set")
        cw.fetch_fmp_trades = no_key
        with self.assertRaises(RuntimeError):
            cw.fetch_congress_trades(today=self.TODAY)


class SoftFailTest(unittest.TestCase):
    PATCHED = ("STATE_PATH", "fetch_congress_trades", "alert",
               "ESCALATE_AFTER_FAILURES", "matches_watchlist")

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
        def boom(today=None):
            raise _http_error(429, "0")
        cw.fetch_congress_trades = boom
        self._seed({"first_run_done": True, "source_version": cw.SOURCE_VERSION,
                    "seen_trade_ids": [], "alert_history": []})

        rcs = [cw.main() for _ in range(cw.ESCALATE_AFTER_FAILURES)]
        self.assertEqual(rcs, [0, 0, 0], "soft-fail returns 0, never crashes the workflow")
        st = json.loads(self._tmp.read_text(encoding="utf-8"))
        self.assertEqual(st["consecutive_fetch_failures"], 3)
        self.assertIn("429", st["last_fetch_error"])
        self.assertEqual(len(self.alerts), 1, "Discord pinged once, at the threshold")

    def test_success_resets_failure_streak(self):
        cw.fetch_congress_trades = lambda today=None: (
            [{"trade_id": "c1", "politician": "Nobody"}], "kadoa")
        cw.matches_watchlist = lambda *a: False
        self._seed({"first_run_done": True, "source_version": cw.SOURCE_VERSION,
                    "seen_trade_ids": [], "alert_history": [],
                    "consecutive_fetch_failures": 5, "last_fetch_error": "stale"})

        self.assertEqual(cw.main(), 0)
        st = json.loads(self._tmp.read_text(encoding="utf-8"))
        self.assertEqual(st["consecutive_fetch_failures"], 0)
        self.assertNotIn("last_fetch_error", st)


class ReseedTest(unittest.TestCase):
    PATCHED = ("STATE_PATH", "fetch_congress_trades", "alert", "matches_watchlist")

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp()) / "state.json"
        self._orig = {k: getattr(cw, k) for k in self.PATCHED}
        cw.STATE_PATH = self._tmp
        self.alerts = []
        cw.alert = lambda *a, **k: self.alerts.append(a) or True
        cw.matches_watchlist = lambda *a: True

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(cw, k, v)

    def test_source_change_reseeds_without_alerting(self):
        # Old Capitol-Trades-era state: first run done, numeric ids, no source_version.
        self._tmp.write_text(json.dumps(
            {"first_run_done": True, "seen_trade_ids": ["10000064890"],
             "alert_history": []}), encoding="utf-8")
        cw.fetch_congress_trades = lambda today=None: (
            [{"trade_id": "cabc123", "politician": "Pelosi"}], "kadoa")

        self.assertEqual(cw.main(), 0)
        self.assertEqual(len(self.alerts), 0,
                         "a source migration seeds the backlog, it does not alert")
        st = json.loads(self._tmp.read_text(encoding="utf-8"))
        self.assertEqual(st["source_version"], cw.SOURCE_VERSION)
        self.assertIn("cabc123", st["seen_trade_ids"])


class BuildAlertClipTest(unittest.TestCase):
    """A malformed feed value stuffed into one cell must not produce an embed
    Discord rejects (>1024-char field / >256-char title) — that would wedge the
    trade forever (never marked seen, re-rejected every run)."""

    def _trade(self, **over):
        t = {"trade_id": "c1", "politician": "Jane Doe", "issuer": "ACME INC ACME",
             "trade_type": "Buy", "owner": "Self", "size_range": "$1K–$15K",
             "tx_date": "2026-06-01", "pub_time": "2026-06-10", "days_lag": "9",
             "doc_url": "https://efd/x"}
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
