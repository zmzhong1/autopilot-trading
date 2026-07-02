#!/usr/bin/env python3
"""Tests for the 8-K deep-analysis pipeline: document parsing, materiality,
financial-highlight / personnel extraction, the enriched watcher card, the
committed events feed, and the materiality-aware confluence filter.

Stdlib-only (unittest), no network — all fetches are stubbed. Run with:
    SEC_USER_AGENT='test test@example.com' python3 -m unittest test_enrich_8k
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SEC_USER_AGENT", "test test@example.com")
os.environ["DRY_RUN"] = "1"

import sec_enrich  # noqa: E402
import sec_watcher  # noqa: E402


CIK = "0001045810"
ACCESSION = "0001045810-26-000060"

# A realistic (abridged) earnings 8-K primary document.
EIGHTK_HTML = """
<html><head><title>nvda-20260702.htm</title></head><body>
<div>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</div>
<div>Item 2.02 Results of Operations and Financial Condition.</div>
<p>On July 2, 2026, NVIDIA Corporation announced its financial results for the
first quarter of fiscal year 2027. A copy of the press release is furnished as
Exhibit 99.1 to this Current Report on Form 8-K.</p>
<div>Item 5.02 Departure of Directors or Certain Officers; Election of Directors;
Appointment of Certain Officers; Compensatory Arrangements of Certain Officers.</div>
<p>On June 30, 2026, Ms. Colette Kress notified the Company of her decision to
retire as Executive Vice President and Chief Financial Officer, effective
September 1, 2026. The Board of Directors appointed Ms. Jane Doe as Chief
Financial Officer, effective the same date.</p>
<div>Item 9.01 Financial Statements and Exhibits.</div>
<p>99.1 Press release dated July 2, 2026.</p>
<p>Pursuant to the requirements of the Securities Exchange Act of 1934, the
registrant has duly caused this report to be signed on its behalf.</p>
</body></html>
"""

PRESS_RELEASE_HTML = """
<html><head><title>NVIDIA Announces Financial Results for First Quarter Fiscal 2027</title></head>
<body>
<p>NVIDIA Announces Financial Results for First Quarter Fiscal 2027</p>
<p>Record quarterly revenue of $44.1 billion, up 69% from a year ago.</p>
<p>GAAP earnings per diluted share was $0.76, up 27% from a year ago.</p>
<p>Data Center revenue was $39.1 billion, up 73% from a year ago.</p>
<p>For the second quarter, the company expects revenue of $45.0 billion,
plus or minus 2%.</p>
<p>Certain statements are forward-looking statements and revenue of $1 could
differ materially.</p>
</body></html>
"""

INDEX_JSON = {
    "directory": {
        "item": [
            {"name": "nvda-20260702.htm", "type": "text.gif"},
            {"name": "ex991q1fy27.htm", "type": "text.gif"},
            {"name": "index.json"},
        ]
    }
}


def _http_get_text(url):
    if "nvda-20260702" in url:
        return EIGHTK_HTML
    if "ex991" in url:
        return PRESS_RELEASE_HTML
    raise OSError(f"unexpected fetch: {url}")


def _http_get_json(url):
    if url.endswith("index.json"):
        return INDEX_JSON
    raise OSError(f"unexpected fetch: {url}")


class MaterialityTest(unittest.TestCase):
    def test_earnings_is_high_and_names_the_driver(self):
        level, drivers = sec_enrich.assess_8k_materiality(["2.02", "9.01"])
        self.assertEqual(level, "high")
        self.assertEqual(len(drivers), 1)
        self.assertIn("2.02", drivers[0])

    def test_regfd_plus_exhibits_is_low(self):
        level, _ = sec_enrich.assess_8k_materiality(["7.01", "9.01"])
        self.assertEqual(level, "low")

    def test_restatement_is_critical(self):
        level, _ = sec_enrich.assess_8k_materiality(["4.02"])
        self.assertEqual(level, "critical")

    def test_unknown_code_counts_as_medium(self):
        level, _ = sec_enrich.assess_8k_materiality(["77.99"])
        self.assertEqual(level, "medium")

    def test_no_items_is_low(self):
        self.assertEqual(sec_enrich.assess_8k_materiality([])[0], "low")


class DocumentParsingTest(unittest.TestCase):
    def test_html_to_text_keeps_block_structure(self):
        text = sec_enrich.html_to_text(EIGHTK_HTML)
        self.assertIn("Item 2.02 Results of Operations", text)
        # Block tags become line breaks so headings stay line-anchored.
        self.assertTrue(any(line.startswith("Item 5.02")
                            for line in text.split("\n")))

    def test_split_8k_items_finds_all_sections(self):
        sections = sec_enrich.split_8k_items(sec_enrich.html_to_text(EIGHTK_HTML))
        self.assertEqual(set(sections), {"2.02", "5.02", "9.01"})
        self.assertIn("Colette Kress", sections["5.02"])

    def test_summarize_section_skips_boilerplate(self):
        summary = sec_enrich.summarize_section(
            "Pursuant to the requirements of the Securities Exchange Act, blah. "
            "On July 2, 2026, the Company completed the acquisition of Acme Corp "
            "for $2.0 billion in cash.")
        self.assertIn("Acme Corp", summary)
        self.assertNotIn("Securities Exchange Act", summary)

    def test_financial_highlights_pull_the_numbers(self):
        text = sec_enrich.html_to_text(PRESS_RELEASE_HTML)
        highlights = sec_enrich.extract_financial_highlights(text)
        joined = " ".join(highlights)
        self.assertIn("$44.1 billion", joined)
        self.assertIn("$0.76", joined)
        # Guidance sentences get flagged as forward-looking.
        self.assertTrue(any(h.startswith("🔭") for h in highlights))
        # Boilerplate forward-looking-statements legalese is excluded.
        self.assertNotIn("differ materially", joined)

    def test_personnel_changes_extracted(self):
        sections = sec_enrich.split_8k_items(sec_enrich.html_to_text(EIGHTK_HTML))
        people = sec_enrich.extract_personnel_changes(sections["5.02"])
        joined = " ".join(people)
        self.assertIn("Colette Kress", joined)
        self.assertIn("Jane Doe", joined)


class Enrich8KTest(unittest.TestCase):
    def test_end_to_end_analysis(self):
        a = sec_enrich.enrich_8k(CIK, ACCESSION, "nvda-20260702.htm",
                                 "2.02,5.02,9.01", _http_get_text, _http_get_json)
        self.assertIsNotNone(a)
        self.assertEqual(a["materiality"], "high")
        self.assertEqual(a["codes"], ["2.02", "5.02", "9.01"])
        summaries = {i["code"]: i["summary"] for i in a["items"]}
        self.assertIn("financial results", summaries["2.02"].lower())
        self.assertTrue(a["financial_highlights"])
        self.assertTrue(any("Colette Kress" in p for p in a["personnel"]))
        self.assertIn("First Quarter", a["press_release_title"])
        self.assertIn("ex991", a["press_release_url"])

    def test_items_recovered_from_body_when_api_omits_them(self):
        a = sec_enrich.enrich_8k(CIK, ACCESSION, "nvda-20260702.htm", "",
                                 _http_get_text, _http_get_json)
        self.assertEqual(a["codes"], ["2.02", "5.02", "9.01"])

    def test_total_fetch_failure_returns_none(self):
        def dead(url):
            raise OSError("down")
        self.assertIsNone(sec_enrich.enrich_8k(CIK, ACCESSION, "x.htm", "",
                                               dead, dead))

    def test_fetch_failure_with_items_still_assesses_materiality(self):
        def dead(url):
            raise OSError("down")
        a = sec_enrich.enrich_8k(CIK, ACCESSION, "x.htm", "4.02", dead, dead)
        self.assertEqual(a["materiality"], "critical")
        self.assertEqual(a["financial_highlights"], [])


class WatcherCardTest(unittest.TestCase):
    def setUp(self):
        self._orig_text = sec_watcher.http_get_text
        self._orig_json = sec_watcher.http_get_json
        sec_watcher.http_get_text = _http_get_text
        sec_watcher.http_get_json = _http_get_json

    def tearDown(self):
        sec_watcher.http_get_text = self._orig_text
        sec_watcher.http_get_json = self._orig_json

    def test_enriched_8k_card_carries_analysis(self):
        extras = {}
        embed, headline = sec_watcher.build_embed(
            "NVIDIA Corp", "8-K", "2026-07-02", "https://example.test",
            CIK, ACCESSION, "nvda-20260702.htm", "2.02,5.02,9.01", extras=extras)
        self.assertIn("HIGH materiality", headline)
        self.assertIn("analysis_8k", extras)
        field_names = [f["name"] for f in embed["fields"]]
        self.assertIn("💰 Key figures", field_names)
        self.assertIn("👤 Leadership changes", field_names)
        self.assertIn("$44.1 billion", json.dumps(embed, ensure_ascii=False))
        self.assertEqual(embed["color"],
                         sec_watcher.COLOR_8K_BY_MATERIALITY["high"])

    def test_fetch_failure_falls_back_to_plain_item_card(self):
        def dead(url):
            raise OSError("down")
        sec_watcher.http_get_text = dead
        sec_watcher.http_get_json = dead
        extras = {}
        embed, headline = sec_watcher.build_embed(
            "NVIDIA Corp", "8-K", "2026-07-02", "https://example.test",
            CIK, ACCESSION, "x.htm", "7.01,9.01", extras=extras)
        # Item codes alone still produce an analysis (materiality from codes).
        self.assertIn("analysis_8k", extras)
        self.assertEqual(extras["analysis_8k"]["materiality"], "low")
        self.assertIn("7.01", headline)


class EventsFeedTest(unittest.TestCase):
    def test_append_dedup_and_cap(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "8k_events.jsonl"
            rec = {"accession": "a-1", "ticker": "NVDA"}
            self.assertTrue(sec_watcher.append_8k_event(rec, path=path))
            self.assertFalse(sec_watcher.append_8k_event(rec, path=path),
                             "same accession must not double-log")
            for i in range(5):
                sec_watcher.append_8k_event({"accession": f"b-{i}"}, path=path, cap=3)
            lines = path.read_text().strip().split("\n")
            self.assertEqual(len(lines), 3, "rolling cap enforced")
            self.assertIn("b-4", lines[-1])

    def test_event_record_shape(self):
        sec_watcher._TICKER_CACHE = {CIK: "NVDA"}
        try:
            analysis = sec_enrich.enrich_8k(CIK, ACCESSION, "nvda-20260702.htm",
                                            "2.02,5.02,9.01",
                                            _http_get_text, _http_get_json)
            filing = {"accession": ACCESSION, "form": "8-K",
                      "filing_date": "2026-07-02", "primary_doc": "nvda-20260702.htm",
                      "items": "2.02,5.02,9.01"}
            rec = sec_watcher.build_8k_event_record(
                "NVIDIA Corp", CIK, filing, analysis, "https://example.test")
            self.assertEqual(rec["ticker"], "NVDA")
            self.assertEqual(rec["materiality"], "high")
            self.assertEqual([i["code"] for i in rec["items"]],
                             ["2.02", "5.02", "9.01"])
            self.assertTrue(rec["financial_highlights"])
        finally:
            sec_watcher._TICKER_CACHE = None


class ConfluenceMaterialityFilterTest(unittest.TestCase):
    def test_low_materiality_8k_does_not_count_as_corporate_feed(self):
        import confluence
        name_to_ticker = {"NVIDIA": "NVDA"}
        low = [{"ts": "2026-07-02T13:00:00+00:00", "filer": "NVIDIA Corp",
                "form": "8-K", "materiality": "low"}]
        sig = confluence.collect_signals(low, [], name_to_ticker, {})
        self.assertNotIn("NVDA", sig)

        high = [{"ts": "2026-07-02T13:00:00+00:00", "filer": "NVIDIA Corp",
                 "form": "8-K", "materiality": "high"}]
        sig = confluence.collect_signals(high, [], name_to_ticker, {})
        self.assertIn("corporate", sig["NVDA"]["feeds"])

    def test_legacy_entries_without_materiality_still_count(self):
        import confluence
        name_to_ticker = {"NVIDIA": "NVDA"}
        legacy = [{"ts": "2026-07-02T13:00:00+00:00", "filer": "NVIDIA Corp",
                   "form": "8-K"}]
        sig = confluence.collect_signals(legacy, [], name_to_ticker, {})
        self.assertIn("corporate", sig["NVDA"]["feeds"])


class RecentEventsContextTest(unittest.TestCase):
    def test_recent_8k_events_filters_by_ticker_and_age(self):
        import enrichment
        from datetime import date
        with tempfile.TemporaryDirectory() as td:
            events_dir = Path(td) / "events"
            events_dir.mkdir()
            rows = [
                {"ticker": "NVDA", "filing_date": "2026-07-02", "accession": "a",
                 "materiality": "high",
                 "items": [{"code": "2.02", "label": "Results",
                            "summary": "Announced Q1 results."}],
                 "financial_highlights": ["Revenue of $44.1 billion, up 69%."]},
                {"ticker": "NVDA", "filing_date": "2025-01-01", "accession": "old",
                 "materiality": "high", "items": []},
                {"ticker": "TSLA", "filing_date": "2026-07-02", "accession": "b",
                 "materiality": "low", "items": []},
            ]
            (events_dir / "8k_events.jsonl").write_text(
                "\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            evs = enrichment.recent_8k_events("NVDA", days=45, root=td,
                                              today=date(2026, 7, 2))
            self.assertEqual(len(evs), 1)
            self.assertEqual(evs[0]["codes"], ["2.02"])
            self.assertEqual(evs[0]["materiality"], "high")
            self.assertIn("Q1 results", evs[0]["summary"])

    def test_missing_feed_returns_empty(self):
        import enrichment
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(enrichment.recent_8k_events("NVDA", root=td), [])


if __name__ == "__main__":
    unittest.main()
