#!/usr/bin/env python3
"""Tests for crowding.compute_crowding — the pure cross-fund 13F overlap logic.

Stdlib-only, no network. SEC_USER_AGENT is set so the module imports past its
startup guard.
"""

import os
import unittest

os.environ.setdefault("SEC_USER_AGENT", "test test@example.com")

import crowding  # noqa: E402


def _h(cusip, issuer, value_usd, shares=0):
    return {"cusip": cusip, "issuer": issuer, "value_usd": value_usd, "shares": shares}


class ComputeCrowdingTest(unittest.TestCase):
    def test_only_names_held_by_min_funds_are_returned(self):
        funds = [
            {"name": "Buffett", "new_cusips": set(),
             "holdings": [_h("AAPL0", "APPLE INC", 100), _h("KO000", "COCA COLA", 50)]},
            {"name": "Ackman", "new_cusips": set(),
             "holdings": [_h("AAPL0", "APPLE INC", 80), _h("CMG00", "CHIPOTLE", 70)]},
            {"name": "Citadel", "new_cusips": set(),
             "holdings": [_h("AAPL0", "APPLE INC CLASS A", 60)]},
        ]
        crowded = crowding.compute_crowding(funds, min_funds=2)
        cusips = [c["cusip"] for c in crowded]
        self.assertEqual(cusips, ["AAPL0"], "only AAPL is held by >=2 funds")

        aapl = crowded[0]
        self.assertEqual(aapl["fund_count"], 3)
        self.assertEqual(aapl["total_value"], 240)
        # Longest issuer label wins.
        self.assertEqual(aapl["issuer"], "APPLE INC CLASS A")
        self.assertEqual(set(f["name"] for f in aapl["funds"]),
                         {"Buffett", "Ackman", "Citadel"})

    def test_ranking_by_fund_count_then_value(self):
        funds = [
            {"name": "F1", "new_cusips": set(),
             "holdings": [_h("X", "X CO", 10), _h("Y", "Y CO", 1000)]},
            {"name": "F2", "new_cusips": set(),
             "holdings": [_h("X", "X CO", 10), _h("Y", "Y CO", 1000)]},
            {"name": "F3", "new_cusips": set(), "holdings": [_h("X", "X CO", 10)]},
        ]
        crowded = crowding.compute_crowding(funds, min_funds=2)
        # X held by 3 funds, Y by 2 -> X ranks first despite lower value.
        self.assertEqual([c["cusip"] for c in crowded], ["X", "Y"])

    def test_new_consensus_flagging(self):
        funds = [
            {"name": "F1", "new_cusips": {"NEW"},
             "holdings": [_h("NEW", "NEWCO", 100)]},
            {"name": "F2", "new_cusips": {"NEW"},
             "holdings": [_h("NEW", "NEWCO", 200)]},
            {"name": "F3", "new_cusips": set(),
             "holdings": [_h("NEW", "NEWCO", 50)]},
        ]
        crowded = crowding.compute_crowding(funds, min_funds=2)
        newco = crowded[0]
        self.assertEqual(newco["new_count"], 2, "two funds newly bought NEWCO")
        new_funds = {f["name"] for f in newco["funds"] if f["is_new"]}
        self.assertEqual(new_funds, {"F1", "F2"})

    def test_same_fund_multiple_lots_collapse_to_one_fund(self):
        funds = [
            {"name": "F1", "new_cusips": set(),
             "holdings": [_h("Z", "ZCO", 100), _h("Z", "ZCO", 100)]},  # two lots
            {"name": "F2", "new_cusips": set(), "holdings": [_h("Z", "ZCO", 50)]},
        ]
        crowded = crowding.compute_crowding(funds, min_funds=2)
        z = crowded[0]
        self.assertEqual(z["fund_count"], 2, "F1's two lots count as one fund")
        f1 = [f for f in z["funds"] if f["name"] == "F1"][0]
        self.assertEqual(f1["value_usd"], 200, "lots summed within a fund")


if __name__ == "__main__":
    unittest.main()
