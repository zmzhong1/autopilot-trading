#!/usr/bin/env python3
"""Regression tests for sec_watcher: dedup-by-accession + self-distinguishing
embeds for same-day same-company Form 4 filings.

Stdlib-only (unittest), no network — SEC/Discord calls are stubbed. Run with:
    SEC_USER_AGENT='test test@example.com' python3 -m unittest test_sec_watcher
The env var is set below so the module imports without its startup guard firing.
"""

import os
import unittest

os.environ.setdefault("SEC_USER_AGENT", "test test@example.com")
os.environ["DRY_RUN"] = "1"  # alert() returns True without hitting Discord

import sec_watcher  # noqa: E402  (must follow env setup)
import sec_enrich  # noqa: E402


# The real 2026-05-27 Alphabet incident: five distinct accessions, one issuer.
ALPHABET_CIK = "0001652044"
ACCESSIONS = [
    "0001193125-26-242628",
    "0001193125-26-242630",
    "0001193125-26-242634",
    "0001193125-26-242636",
    "0001193125-26-242640",
]


def _submissions(accessions, form="8-K"):
    """Minimal EDGAR submissions payload, newest-first like the real API."""
    accs = list(reversed(accessions))  # API returns reverse-chronological
    n = len(accs)
    return {
        "filings": {
            "recent": {
                "accessionNumber": accs,
                "form": [form] * n,
                "filingDate": ["2026-05-27"] * n,
                "primaryDocument": ["primary.htm"] * n,
                "items": [""] * n,
            }
        }
    }


class DedupByAccessionTest(unittest.TestCase):
    def setUp(self):
        # Stub the only network call check_entry makes for 8-K (no enrichment).
        self._orig_get_json = sec_watcher.http_get_json
        sec_watcher.http_get_json = lambda url: _submissions(ACCESSIONS, form="8-K")

    def tearDown(self):
        sec_watcher.http_get_json = self._orig_get_json

    def test_distinct_accessions_send_once_then_dedup(self):
        entry = {"cik": ALPHABET_CIK, "name": "Alphabet Inc (Google)", "forms": ["8-K"]}
        # CIK already seeded (not first run, not new) but none of these accessions seen.
        state = {"sec_seen": {ALPHABET_CIK: ["0000000000-00-000000"]},
                 "first_run_done": True, "alert_history": []}

        sent = sec_watcher.check_entry(entry, state, is_first_run=False, alerts_left=20)
        self.assertEqual(sent, len(ACCESSIONS), "all distinct accessions alert on first sight")
        for acc in ACCESSIONS:
            self.assertIn(acc, state["sec_seen"][ALPHABET_CIK])

        # Second run over the identical feed must re-send nothing.
        resent = sec_watcher.check_entry(entry, state, is_first_run=False, alerts_left=20)
        self.assertEqual(resent, 0, "already-seen accessions are deduped, not re-posted")


class SelfDistinguishingEmbedTest(unittest.TestCase):
    def tearDown(self):
        # Restore in case a test patched enrichment.
        import importlib
        importlib.reload(sec_enrich)
        sec_watcher.sec_enrich = sec_enrich

    def test_form4_fallback_embeds_are_distinct_per_accession(self):
        # Force enrichment to fail -> exercise the fallback path that caused the
        # "5 identical cards" incident.
        sec_watcher.sec_enrich.enrich_form4 = lambda *a, **k: None

        embeds = []
        for acc in ACCESSIONS:
            url = sec_watcher.filing_url(ALPHABET_CIK, acc, "primary.xml")
            embed, headline = sec_watcher.build_embed(
                "Alphabet Inc (Google)", "4", "2026-05-27", url,
                ALPHABET_CIK, acc, "primary.xml", "")
            embeds.append(embed)
            # Each card must carry its own accession (footer + field + headline).
            self.assertEqual(embed["footer"]["text"], f"📎 {acc}")
            self.assertIn(acc, headline)
            field_values = [f["value"] for f in embed.get("fields", [])]
            self.assertIn(acc, field_values)
            self.assertIn(acc, embed["url"])  # deep link, not the generic browse page

        # No two fallback cards render identically.
        footers = [e["footer"]["text"] for e in embeds]
        self.assertEqual(len(set(footers)), len(ACCESSIONS))

    def test_enriched_form4_embed_shows_insider_and_accession(self):
        acc = ACCESSIONS[0]
        sec_watcher.sec_enrich.enrich_form4 = lambda *a, **k: {
            "issuer_name": "Alphabet Inc.",
            "ticker": "GOOGL",
            "insider": "PICHAI SUNDAR",
            "role": "CEO",
            "transactions": [{
                "code": "S", "label": "Open-market sale", "side": "sell",
                "shares": 1000.0, "price": 175.0, "value": 175000.0,
                "post_holdings": 50000.0, "security": "Class A", "derivative": False,
            }],
            "total_value": 175000.0,
            "dominant_side": "sell",
        }
        url = sec_watcher.filing_url(ALPHABET_CIK, acc, "primary.xml")
        embed, headline = sec_watcher.build_embed(
            "Alphabet Inc (Google)", "4", "2026-05-27", url,
            ALPHABET_CIK, acc, "primary.xml", "")

        self.assertIn("PICHAI SUNDAR", embed["title"])      # reporting person
        self.assertIn(acc, embed["footer"]["text"])         # accession footer
        # Accession + EDGAR deep link present as a field.
        filing_fields = [f for f in embed["fields"] if f["name"] == "Filing"]
        self.assertTrue(filing_fields and acc in filing_fields[0]["value"])
        self.assertIn("-index.htm", filing_fields[0]["value"])


class SameDayForm4BatchingTest(unittest.TestCase):
    def setUp(self):
        self._orig_get_json = sec_watcher.http_get_json
        sec_watcher.http_get_json = lambda url: _submissions(ACCESSIONS, form="4")
        # Distinct insider per accession so the batched card has distinct lines.
        self._orig_enrich = sec_watcher.sec_enrich.enrich_form4

        def fake_enrich(cik, acc, primary_doc, *_a, **_k):
            idx = ACCESSIONS.index(acc)
            return {
                "issuer_name": "Alphabet Inc.", "ticker": "GOOGL",
                "insider": f"INSIDER {idx}", "role": "Director",
                "transactions": [{"code": "S", "label": "Sale", "side": "sell",
                                  "shares": 100.0, "price": 10.0, "value": 1000.0,
                                  "post_holdings": None, "security": "A",
                                  "derivative": False}],
                "total_value": 1000.0, "dominant_side": "sell",
            }

        sec_watcher.sec_enrich.enrich_form4 = fake_enrich

    def tearDown(self):
        sec_watcher.http_get_json = self._orig_get_json
        sec_watcher.sec_enrich.enrich_form4 = self._orig_enrich

    def test_same_day_form4s_collapse_to_one_post(self):
        entry = {"cik": ALPHABET_CIK, "name": "Alphabet Inc (Google)", "forms": ["4"]}
        state = {"sec_seen": {ALPHABET_CIK: ["0000000000-00-000000"]},
                 "first_run_done": True, "alert_history": []}

        posts = sec_watcher.check_entry(entry, state, is_first_run=False, alerts_left=20)
        self.assertEqual(posts, 1, "five same-day Form 4s batch into a single card")
        # All five accessions are still individually recorded (dedup + digest).
        for acc in ACCESSIONS:
            self.assertIn(acc, state["sec_seen"][ALPHABET_CIK])
        self.assertEqual(len(state["alert_history"]), len(ACCESSIONS))

        # Nothing re-posts on the next identical poll.
        resent = sec_watcher.check_entry(entry, state, is_first_run=False, alerts_left=20)
        self.assertEqual(resent, 0)

    def test_batch_embed_lists_every_accession_distinctly(self):
        filings = [{"accession": a, "form": "4", "filing_date": "2026-05-27",
                    "primary_doc": "primary.xml", "items": ""} for a in ACCESSIONS]
        embed, headline = sec_watcher._form4_batch_embed(
            "Alphabet Inc (Google)", ALPHABET_CIK, filings, "2026-05-27")

        self.assertIn(str(len(ACCESSIONS)), embed["title"])
        for idx, acc in enumerate(ACCESSIONS):
            self.assertIn(f"INSIDER {idx}", embed["description"])
            self.assertIn(sec_watcher.edgar_index_url(ALPHABET_CIK, acc),
                          embed["description"])
        self.assertEqual(embed["footer"]["text"], f"📎 {len(ACCESSIONS)} accessions on 2026-05-27")
        net_field = [f for f in embed["fields"] if f["name"] == "Net buy / sell"]
        self.assertTrue(net_field)


if __name__ == "__main__":
    unittest.main()
