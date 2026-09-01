from __future__ import annotations

import unittest

import parr_badger_runner as pb
import photobook_recognition as recognition


class PhotobookRecognitionTests(unittest.TestCase):
    def setUp(self):
        recognition.load_library.cache_clear()
        recognition._library_token_index.cache_clear()

    def test_library_contains_existing_canon_and_priority_supplement(self):
        stats = recognition.library_stats()
        self.assertGreaterEqual(stats["records"], 2000)
        self.assertLessEqual(stats["records"], 5000)
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

    def test_name_order_variants_merge_into_one_record(self):
        rows = [
            row for row in recognition.load_library()
            if pb.normalize(row.get("Title")) == pb.normalize("Life Is Good & Good for You in New York")
            and recognition._contributor_identity(row.get("Contributor"))
            == recognition._contributor_identity("William Klein")
        ]
        self.assertEqual(len(rows), 1)

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

    def test_diane_arbus_biographies_do_not_match_eponymous_monograph(self):
        titles = [
            "Diane Arbus: Magazine Work by Thomas W. Southall",
            "Diane Arbus: A Chronology by Doon Arbus and Elisabeth Sussman",
            "Diane Arbus: Portrait of a Photographer by Arthur Lubow",
            "Silent Dialogues: Diane Arbus and Howard Nemerov",
        ]
        forbidden = {
            pb.normalize("Diane Arbus"),
            pb.normalize("Diane Arbus: An Aperture Monograph"),
        }
        for title in titles:
            with self.subTest(title=title):
                matches = recognition.match_listing({"title": title})
                self.assertFalse(any(pb.normalize(match["title"]) in forbidden for match in matches))

    def test_exact_arbus_monograph_still_matches(self):
        item = {
            "title": "Diane Arbus Aperture Monograph 1972 first edition hardcover",
            "publisher": "Aperture",
            "publication_year": "1972",
        }
        matches = recognition.match_listing(item)
        self.assertTrue(matches)
        self.assertEqual(matches[0]["record_id"], "diane-arbus-monograph")

    def test_issue_320_untitled_listing_matches_the_correct_work(self):
        item = {
            "title": "Diane Arbus: Untitled 1st Edition Hardcover Book Aperture Foundation 2011",
            "publisher": "Aperture Foundation",
            "publication_year": "2011",
            "edition": "1st Edition",
            "price_gbp": 37.09,
            "private_seller": True,
            "seller_account_type": "INDIVIDUAL",
            "buying_options": ["FIXED_PRICE", "BEST_OFFER"],
            "search_lane": "library_rotation",
        }
        match = recognition.match_listing(item)[0]
        self.assertEqual(pb.normalize(match["title"]), "untitled")
        self.assertEqual(pb.normalize(match["contributor"]), "diane arbus")
        score, reasons = recognition.opportunity_score(item, match)
        self.assertLess(score, 72)
        self.assertNotIn("casual seller wording", reasons)
        self.assertIn("publisher-backlist record lacks independent value evidence", reasons)

    def test_low_tier_backlist_book_is_not_alerted_on_price_alone(self):
        item = {
            "title": "Diane Arbus Untitled Large Format Hardcover Book Beautiful plates damaged spine",
            "publisher": "Thames & Hudson",
            "price_gbp": 14.99,
            "private_seller": True,
            "seller_account_type": "INDIVIDUAL",
            "buying_options": ["FIXED_PRICE"],
            "search_lane": "library_rotation",
        }
        match = recognition.match_listing(item)[0]
        self.assertEqual(pb.normalize(match["title"]), "untitled")
        score, reasons = recognition.opportunity_score(item, match)
        self.assertLess(score, 72)
        self.assertIn("publisher-backlist record lacks independent value evidence", reasons)

    def test_known_reprint_is_not_an_urgent_collectible_candidate(self):
        item = {
            "title": "Diane Arbus An Aperture Monograph 40th Anniversary Edition",
            "publisher": "Aperture",
            "publication_year": "2012",
            "edition": "40th Anniversary Edition",
            "price_gbp": 20.0,
            "private_seller": True,
            "seller_account_type": "INDIVIDUAL",
            "buying_options": ["FIXED_PRICE"],
        }
        match = recognition.match_listing(item)[0]
        score, _ = recognition.opportunity_score(item, match)
        self.assertEqual(match["edition_status"], "mismatch")
        self.assertLess(score, 72)

    def test_one_of_multiple_listing_isbns_can_confirm_target(self):
        status, reasons = recognition.assess_edition(
            {"isbn": "1597111741 | 9781597111744"},
            {"isbn": "9781597111744", "year": "", "publisher": ""},
        )
        self.assertEqual(status, "confirmed")
        self.assertIn("target ISBN matches", reasons)

    def test_conflicting_publisher_suppresses_famous_men_reprint(self):
        item = {
            "title": "Let Us Now Praise Famous Men by James Agee and Walker Evans Picador PB",
            "publisher": "Picador",
            "price_gbp": 12.94,
            "private_seller": True,
            "seller_account_type": "INDIVIDUAL",
            "buying_options": ["FIXED_PRICE", "BEST_OFFER"],
        }
        match = recognition.match_listing(item)[0]
        score, _ = recognition.opportunity_score(item, match)
        self.assertEqual(match["edition_status"], "mismatch")
        self.assertLess(score, 72)

    def test_search_query_stays_inside_ebay_limit(self):
        row = {
            "Contributor": "A" * 70,
            "Title": "B" * 70,
        }
        self.assertLessEqual(len(recognition.search_query_for_record(row)), 100)


if __name__ == "__main__":
    unittest.main()
