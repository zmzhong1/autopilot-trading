#!/usr/bin/env python3
"""Tests for live_bridge.py — the pure vetting that stands between a proposal
and a real Robinhood order. No network, no files touched (state is passed in).
"""

import os
import unittest
from datetime import date, datetime, timedelta, timezone

os.environ.setdefault("SEC_USER_AGENT", "test test@example.com")
os.environ.pop("EXECUTOR_KILL", None)

import live_bridge as lb  # noqa: E402
import executor  # noqa: E402

TODAY = date(2026, 9, 7)
NOW = datetime(2026, 9, 7, 14, 40, tzinfo=timezone.utc)


def gr(**over):
    g = dict(executor.DEFAULT_GUARDRAILS)
    g.update({
        "enabled": True, "mode": "live",
        "account_value_usd": 500.0,
        "allow_list": ["AAPL", "MSFT", "NVDA", "AJNMY"],
        "block_list": [],
        "allowed_sides": ["buy"],
        "max_notional_per_order_usd": 250.0,
        "max_pct_account_per_order": 0.10,
        "max_orders_per_day": 2,
        "max_total_deployed_pct": 0.70,
        "max_existing_position_pct": 25.0,
        "min_conviction": "medium",
        "rebuy_cooldown_days": 14,
        "snapshot_max_age_min": 120,
        "max_proposal_age_days": 7,
        "live_account_last4": "2732",
        "require_fractional_tradability": True,
    })
    g.update(over)
    return g


def snap(**over):
    s = {"ts": NOW.isoformat(timespec="seconds"), "account_last4": "2732",
         "total_value": 500.0, "equity_value": 0.0, "cash": 500.0,
         "buying_power": 500.0, "positions": [], "open_orders": [],
         "quotes": {}, "fractional_ok": {"AAPL": True, "MSFT": True, "AJNMY": False}}
    s.update(over)
    return s


def prop(ticker, day=TODAY, conviction="medium", fatal=0, found=True, feeds=4,
         status="proposed"):
    return {"date": day.isoformat(), "ticker": ticker, "side": "buy",
            "status": status, "feed_count": feeds,
            "feeds": ["congress", "insider", "institutional", "analyst"][:feeds],
            "stocknews": {"found": found, "conviction": conviction,
                          "fatal_flags": fatal, "xii_score": 85}}


class LatestProposalsTest(unittest.TestCase):
    def test_newest_per_ticker_within_window(self):
        rows = [prop("AAPL", TODAY - timedelta(days=14)),
                prop("AAPL", TODAY - timedelta(days=7)),
                prop("MSFT", TODAY - timedelta(days=8)),   # too old
                prop("NVDA", TODAY, status="live_placed")]  # already promoted
        out = lb.latest_proposals(rows, TODAY, 7)
        self.assertEqual([r["ticker"] for r in out], ["AAPL"])
        self.assertEqual(out[0]["date"], (TODAY - timedelta(days=7)).isoformat())

    def test_rows_with_live_block_are_skipped(self):
        rows = [dict(prop("AAPL"), live={"order_id": "x"})]
        self.assertEqual(lb.latest_proposals(rows, TODAY, 7), [])

    def test_sorted_by_feed_count_desc(self):
        rows = [prop("AAPL", feeds=3), prop("MSFT", feeds=5)]
        out = lb.latest_proposals(rows, TODAY, 7)
        self.assertEqual([r["ticker"] for r in out], ["MSFT", "AAPL"])


class PostureTest(unittest.TestCase):
    def test_disabled_emits_nothing(self):
        o, s, n = lb.select_live_orders([prop("AAPL")], snap(), [], gr(enabled=False), TODAY, NOW)
        self.assertEqual(o, [])
        self.assertIn("enabled", n[0])

    def test_non_live_mode_emits_nothing(self):
        o, _, n = lb.select_live_orders([prop("AAPL")], snap(), [], gr(mode="paper"), TODAY, NOW)
        self.assertEqual(o, [])
        self.assertIn("not live", n[0])

    def test_kill_env_wins(self):
        lb.KILL = True
        try:
            o, _, n = lb.select_live_orders([prop("AAPL")], snap(), [], gr(), TODAY, NOW)
        finally:
            lb.KILL = False
        self.assertEqual(o, [])
        self.assertIn("EXECUTOR_KILL", n[0])

    def test_missing_or_stale_snapshot(self):
        o, _, n = lb.select_live_orders([prop("AAPL")], {}, [], gr(), TODAY, NOW)
        self.assertEqual(o, [])
        old = snap(ts=(NOW - timedelta(hours=5)).isoformat())
        o, _, n = lb.select_live_orders([prop("AAPL")], old, [], gr(), TODAY, NOW)
        self.assertEqual(o, [])
        self.assertIn("stale", n[0])

    def test_wrong_account_refuses(self):
        o, _, n = lb.select_live_orders([prop("AAPL")], snap(account_last4="7467"), [], gr(), TODAY, NOW)
        self.assertEqual(o, [])
        self.assertIn("7467", n[0])


class SelectionTest(unittest.TestCase):
    def test_happy_path_sizes_against_live_value(self):
        o, s, _ = lb.select_live_orders([prop("AAPL"), prop("MSFT")], snap(), [], gr(), TODAY, NOW)
        self.assertEqual([x["ticker"] for x in o], ["AAPL", "MSFT"])
        self.assertEqual(o[0]["dollar_amount"], "50.00")
        self.assertEqual(o[0]["type"], "market")
        self.assertEqual(o[0]["market_hours"], "regular_hours")
        self.assertEqual(o[0]["size_pct"], 10.0)
        # deterministic idempotency key
        self.assertEqual(o[0]["ref_id"], lb.ref_id_for(TODAY.isoformat(), "AAPL"))

    def test_sizes_from_snapshot_not_config(self):
        o, _, _ = lb.select_live_orders([prop("AAPL")], snap(total_value=1000.0, buying_power=1000.0),
                                        [], gr(account_value_usd=500.0), TODAY, NOW)
        self.assertEqual(o[0]["dollar_amount"], "100.00")

    def test_allow_and_block_rechecked(self):
        o, s, _ = lb.select_live_orders([prop("TSLA")], snap(), [], gr(), TODAY, NOW)
        self.assertEqual(o, []); self.assertIn("allow_list", s[0]["reason"])
        o, s, _ = lb.select_live_orders([prop("AAPL")], snap(), [], gr(block_list=["AAPL"]), TODAY, NOW)
        self.assertEqual(o, []); self.assertIn("block_list", s[0]["reason"])

    def test_research_gates_rechecked(self):
        o, s, _ = lb.select_live_orders([prop("AAPL", conviction="low")], snap(), [], gr(), TODAY, NOW)
        self.assertIn("conviction", s[0]["reason"])
        o, s, _ = lb.select_live_orders([prop("AAPL", fatal=1)], snap(), [], gr(), TODAY, NOW)
        self.assertIn("fatal", s[0]["reason"])
        o, s, _ = lb.select_live_orders([prop("AAPL", found=False)], snap(), [], gr(), TODAY, NOW)
        self.assertIn("thesis", s[0]["reason"])

    def test_otc_not_fractional_skipped(self):
        o, s, _ = lb.select_live_orders([prop("AJNMY")], snap(), [], gr(), TODAY, NOW)
        self.assertEqual(o, []); self.assertIn("fractional", s[0]["reason"])

    def test_daily_cap_counts_committed_live_orders(self):
        live = [{"date": TODAY.isoformat(), "ticker": "NVDA", "state": "filled"}]
        o, s, _ = lb.select_live_orders([prop("AAPL"), prop("MSFT")], snap(), live, gr(), TODAY, NOW)
        self.assertEqual([x["ticker"] for x in o], ["AAPL"])
        self.assertIn("daily cap", s[0]["reason"])

    def test_same_day_duplicate_is_idempotent(self):
        live = [{"date": TODAY.isoformat(), "ticker": "AAPL", "state": "placed"}]
        o, s, _ = lb.select_live_orders([prop("AAPL")], snap(), live, gr(), TODAY, NOW)
        self.assertEqual(o, []); self.assertIn("already ordered today", s[0]["reason"])

    def test_cancelled_live_order_does_not_count(self):
        live = [{"date": TODAY.isoformat(), "ticker": "AAPL", "state": "cancelled"}]
        o, _, _ = lb.select_live_orders([prop("AAPL")], snap(), live, gr(), TODAY, NOW)
        self.assertEqual(len(o), 1)

    def test_rebuy_cooldown(self):
        live = [{"date": (TODAY - timedelta(days=7)).isoformat(), "ticker": "AAPL", "state": "filled"}]
        o, s, _ = lb.select_live_orders([prop("AAPL")], snap(), live, gr(), TODAY, NOW)
        self.assertEqual(o, []); self.assertIn("cooldown", s[0]["reason"])
        live = [{"date": (TODAY - timedelta(days=15)).isoformat(), "ticker": "AAPL", "state": "filled"}]
        o, _, _ = lb.select_live_orders([prop("AAPL")], snap(), live, gr(), TODAY, NOW)
        self.assertEqual(len(o), 1)

    def test_concentration_cap_uses_live_positions_and_open_orders(self):
        s1 = snap(positions=[{"symbol": "AAPL", "quantity": 0.3, "average_buy_price": 300,
                              "market_value": 100.0}], equity_value=100.0, cash=400.0,
                  buying_power=400.0)
        o, s, _ = lb.select_live_orders([prop("AAPL")], s1, [], gr(), TODAY, NOW)
        self.assertEqual(o, []); self.assertIn("% of acct", s[0]["reason"])   # 100+50=150 > 125
        s2 = snap(open_orders=[{"symbol": "AAPL", "side": "buy", "state": "queued",
                                "notional_usd": 50.0}])
        o, s, _ = lb.select_live_orders([prop("AAPL")], s2, [], gr(), TODAY, NOW)
        self.assertEqual(o, []); self.assertIn("pending", s[0]["reason"])

    def test_deploy_cap_uses_live_equity(self):
        s1 = snap(equity_value=320.0, cash=180.0, buying_power=180.0)  # cap 350
        o, s, _ = lb.select_live_orders([prop("AAPL")], s1, [], gr(), TODAY, NOW)
        self.assertEqual(o, []); self.assertIn("deploy cap", s[0]["reason"])

    def test_buying_power_binds(self):
        o, s, _ = lb.select_live_orders([prop("AAPL")], snap(buying_power=20.0), [], gr(), TODAY, NOW)
        self.assertEqual(o, []); self.assertIn("buying power", s[0]["reason"])

    def test_reserves_within_run(self):
        # two proposals, buying power only covers one
        o, s, _ = lb.select_live_orders([prop("AAPL"), prop("MSFT")], snap(buying_power=60.0), [], gr(),
                                        TODAY, NOW)
        self.assertEqual([x["ticker"] for x in o], ["AAPL"])
        self.assertIn("buying power", s[0]["reason"])


class SnapshotTest(unittest.TestCase):
    def test_build_from_raw_mcp_payloads(self):
        portfolio = {"data": {"total_value": "500", "equity_value": "0", "cash": "500",
                              "buying_power": {"buying_power": "500.0000"}}}
        positions = {"data": {"positions": [{"symbol": "AAPL", "quantity": "0.15",
                                             "average_buy_price": "320.00"}]}}
        orders = {"data": {"orders": [
            {"id": "o1", "symbol": "MSFT", "side": "buy", "state": "queued",
             "dollar_based_amount": {"amount": "50.00"}},
            {"id": "o2", "symbol": "NVDA", "side": "buy", "state": "cancelled",
             "dollar_based_amount": {"amount": "50.00"}}]}}
        quotes = {"data": {"results": [{"quote": {"symbol": "AAPL", "last_trade_price": "320.00"}}]}}
        trad = {"data": {"results": [
            {"symbol": "AAPL", "tradeable": True, "state": "active", "fractional_tradability": "tradable"},
            {"symbol": "AJNMY", "tradeable": True, "state": "active", "fractional_tradability": "untradable"}]}}
        s = lb.build_snapshot(portfolio, positions, orders, quotes, trad, account_last4="2732", now=NOW)
        self.assertEqual(s["total_value"], 500.0)
        self.assertEqual(s["buying_power"], 500.0)
        self.assertEqual(s["positions"][0]["market_value"], 48.0)
        self.assertEqual([o["symbol"] for o in s["open_orders"]], ["MSFT"])
        self.assertEqual(s["open_orders"][0]["notional_usd"], 50.0)
        self.assertEqual(s["fractional_ok"], {"AAPL": True, "AJNMY": False})
        self.assertEqual(s["account_last4"], "2732")

    def test_age(self):
        self.assertAlmostEqual(lb.snapshot_age_minutes({"ts": NOW.isoformat()}, NOW + timedelta(minutes=30)), 30.0)
        self.assertIsNone(lb.snapshot_age_minutes({}, NOW))


class RecordReconcileTest(unittest.TestCase):
    def test_record_marks_proposal_and_is_idempotent(self):
        rows = [prop("AAPL", TODAY - timedelta(days=1))]
        order = {"ticker": "AAPL", "side": "buy", "notional_usd": 50.0,
                 "proposal_date": rows[0]["date"], "feeds": rows[0]["feeds"]}
        live, rows, e = lb.record_order([], rows, order, "oid-1", "ref-1", "placed", NOW, "2732")
        self.assertEqual(len(live), 1)
        self.assertEqual(rows[0]["status"], "live_placed")
        self.assertEqual(rows[0]["live"]["order_id"], "oid-1")
        # same order id again -> update, not duplicate
        live, rows, e = lb.record_order(live, rows, order, "oid-1", "ref-1", "filled", NOW,
                                        "2732", avg_price=319.5, quantity=0.1565)
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["state"], "filled")
        self.assertEqual(live[0]["avg_price"], 319.5)

    def test_reconcile_from_orders_payload(self):
        rows = [prop("AAPL")]
        order = {"ticker": "AAPL", "notional_usd": 50.0, "proposal_date": rows[0]["date"]}
        live, rows, _ = lb.record_order([], rows, order, "oid-1", "ref-1", "placed", NOW, "2732")
        payload = {"data": {"orders": [{"id": "oid-1", "state": "filled",
                                        "average_price": "318.20", "cumulative_quantity": "0.157130"}]}}
        n = lb.reconcile(live, rows, payload, NOW)
        self.assertEqual(n, 1)
        self.assertEqual(live[0]["state"], "filled")
        self.assertEqual(rows[0]["status"], "live_filled")
        self.assertEqual(rows[0]["entry_price"], 318.2)
        # cancelled propagates
        payload = {"data": {"orders": [{"id": "oid-1", "state": "cancelled"}]}}
        lb.reconcile(live, rows, payload, NOW)
        self.assertEqual(rows[0]["status"], "live_cancelled")

    def test_ref_id_stable(self):
        self.assertEqual(lb.ref_id_for("2026-09-07", "aapl"), lb.ref_id_for("2026-09-07", "AAPL"))
        self.assertNotEqual(lb.ref_id_for("2026-09-07", "AAPL"), lb.ref_id_for("2026-09-08", "AAPL"))


if __name__ == "__main__":
    unittest.main()
