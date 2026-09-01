from __future__ import annotations

import unittest

import parr_badger_runner as pb
import photobook_recognition as recognition


class PhotobookRecognitionTests(unittest.TestCase):
    def setUp(self):
        recognition.load_library.cache_clear()

    def test_library_contains_existing_canon_and_priority_supplement(self):
        stats = recognition.library_stats()
        self.assertGreaterEqual(stats["records"], 628)
        self.assertIn("S", stats["tiers"])
        self.assertIn("0", stats["priorities"])

    def test_priority_seed_promotes_known_target(self):
        rows = [
            row for row in recognition.load_library()
            if pb.normalize(row.get("Contributor")) == pb.normalize("Richard Billingham")
            and pb.normalize(row.get("Title")) == pb.normalize("Ray's a Laugh")
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0].get("Search priority")), "0")
        self.assertEqual(str(rows[0].get("Collectibility tier")), "S")

    def test_title_alias_recognizes_badly_written_ruscha_listing(self):
        item = {
            "title": "Ed Ruscha Twenty Six Gasoline Stations old photo book",
            "context": "Used book",
        }
        matches = recognition.match_listing(item)
        self.assertTrue(matches)
        self.assertEqual(
            pb.normalize(matches[0]["title"]),
            pb.normalize("Twentysix Gasoline Stations"),
        )

    def test_cheap_private_listing_scores_high(self):
        item = {
            "title": "Richard Billingham Rays a Laugh old photography book",
            "context": "Used book from house clearance",
            "price_gbp": 20.0,
            "price_value": 20.0,
            "price_currency": "GBP",
            "private_seller": True,
            "seller_account_type": "INDIVIDUAL",
            "seller_feedback_score": 42,
            "buying_options": ["FIXED_PRICE"],
        }
        match = recognition.match_listing(item)[0]
        score, reasons = recognition.opportunity_score(item, match)
        self.assertGreaterEqual(score, 72)
        self.assertIn("private individual seller", reasons)

    def test_search_query_stays_inside_ebay_limit(self):
        row = {
            "Contributor": "A" * 70,
            "Title": "B" * 70,
        }
        self.assertLessEqual(len(recognition.search_query_for_record(row)), 100)


if __name__ == "__main__":
    unittest.main()
