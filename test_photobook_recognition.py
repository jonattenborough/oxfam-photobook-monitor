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

    def test_canonical_edition_metadata_beats_publisher_reissue_metadata(self):
        row = next(
            row for row in recognition.load_library()
            if pb.normalize(row.get("Contributor")) == pb.normalize("Paul Graham")
            and pb.normalize(row.get("Title")) == pb.normalize("A1: The Great North Road")
        )
        self.assertEqual(row.get("Year"), "1983")
        self.assertEqual(row.get("Publisher"), "Grey Editions")

    def test_lower_authority_backlist_cannot_fill_canonical_edition_fields(self):
        canonical = recognition._prepare(
            {
                "Record ID": "canon:test",
                "Contributor": "Example Photographer",
                "Title": "Example Book",
                "Year": "",
                "Publisher": "",
                "Canon sources": "Parr/Badger Vol. III",
            }
        )
        reissue = recognition._prepare(
            {
                "Record ID": "openlibrary:test",
                "Contributor": "Example Photographer",
                "Title": "Example Book",
                "Year": "2025",
                "Publisher": "Reissue Press",
                "Canon sources": "Open Library publisher snapshot",
            }
        )
        merged = recognition._merge_record(canonical, reissue)
        self.assertEqual(merged.get("Year", ""), "")
        self.assertEqual(merged.get("Publisher", ""), "")

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

    def test_signed_numbered_print_edition_gets_object_bonus(self):
        item = {
            "title": "Rahim Fortune I can't stand to see you cry special edition",
            "description": "Signed and numbered copy with original 8x10 pigment print",
            "publisher": "Loose Joints",
            "publication_year": "2021",
            "price_gbp": 220.0,
            "private_seller": True,
            "seller_account_type": "INDIVIDUAL",
            "buying_options": ["FIXED_PRICE", "BEST_OFFER"],
        }
        match = recognition.match_listing(item)[0]
        score, reasons = recognition.opportunity_score(item, match)
        self.assertGreaterEqual(match["collectible_format_bonus"], 18)
        self.assertIn("book or edition with an original print", match["collectible_format_evidence"])
        self.assertTrue(any(reason.startswith("collectible object:") for reason in reasons))
        self.assertGreaterEqual(score, 72)

    def test_limitation_and_artist_proof_marks_are_recognized(self):
        item = {
            "title": "Chloe Dewe Mathews Thames Log special edition",
            "description": "Signed copy no. 3/30 with pigment print A/P",
        }
        match = recognition.match_listing(item)[0]
        bonus, reasons, labels = recognition.collectible_format_evidence(item, match)
        self.assertGreaterEqual(bonus, 18)
        self.assertIn("numbered copy", labels)
        self.assertIn("unique work or artist proof", labels)
        self.assertTrue(any(reason.startswith("collectible object:") for reason in reasons))

    def test_british_documentary_print_edition_is_a_priority_object(self):
        item = {
            "title": "Craig Easton BANK TOP GOST special edition",
            "description": "Signed and numbered 18/50 with original silver gelatin print",
            "publisher": "GOST Books",
            "publication_year": "2022",
            "price_gbp": 140.0,
            "private_seller": True,
            "seller_account_type": "INDIVIDUAL",
            "buying_options": ["FIXED_PRICE"],
        }
        match = recognition.match_listing(item)[0]
        score, reasons = recognition.opportunity_score(item, match)
        self.assertEqual(match["contributor"], "Craig Easton")
        self.assertEqual(match["documentary_relevance"], "HIGH")
        self.assertIn("silver gelatin print", match["collectible_variants"])
        self.assertGreaterEqual(match["collectible_format_bonus"], 18)
        self.assertTrue(any(reason.startswith("collectible object:") for reason in reasons))
        self.assertGreaterEqual(score, 72)

    def test_plain_expensive_recent_book_needs_market_signal(self):
        item = {
            "title": "Eleonora Agostini A Study on Waitressing photography book",
            "publisher": "Witty Books",
            "publication_year": "2025",
            "price_gbp": 400.0,
            "private_seller": True,
            "seller_account_type": "INDIVIDUAL",
            "buying_options": ["FIXED_PRICE"],
        }
        match = recognition.match_listing(item)[0]
        score, reasons = recognition.opportunity_score(item, match)
        self.assertLess(score, 72)
        self.assertIn(
            "respected recent title lacks a current bargain or special-edition signal",
            reasons,
        )

    def test_first_monograph_metadata_is_exposed_by_matcher(self):
        match = recognition.match_listing(
            {"title": "Sabiha Cimen HAFIZ Red Hook Editions photobook"}
        )[0]
        self.assertEqual(match["first_monograph"], "YES")
        self.assertEqual(match["documentary_relevance"], "HIGH")
        self.assertIn("First PhotoBook winner 2022", match["awards_and_evidence"])

    def test_one_of_multiple_listing_isbns_can_confirm_target(self):
        status, reasons = recognition.assess_edition(
            {"isbn": "1597111741 | 9781597111744"},
            {"isbn": "9781597111744", "year": "", "publisher": ""},
        )
        self.assertEqual(status, "confirmed")
        self.assertIn("target ISBN matches", reasons)

    def test_year_and_publisher_alone_are_plausible_not_confirmed(self):
        status, reasons = recognition.assess_edition(
            {"publisher": "Aperture", "publication_year": "1972"},
            {"isbn": "", "year": "1972", "publisher": "Aperture"},
        )
        self.assertEqual(status, "plausible")
        self.assertIn("target publisher matches", reasons)
        self.assertIn("target year 1972 appears", reasons)

    def test_single_book_collection_subtitle_is_not_bundle_evidence(self):
        self.assertFalse(
            recognition.collection_bundle_evidence(
                {"title": "The Gourmand's Egg: A Collection of Stories and Recipes"}
            )
        )
        self.assertTrue(
            recognition.collection_bundle_evidence(
                {"title": "12 photography books collection from house clearance"}
            )
        )
        self.assertFalse(
            recognition.collection_bundle_evidence(
                {"description": "An ideal addition to any photography book collection"}
            )
        )

    def test_explicit_series_volume_selects_the_matching_record(self):
        matches = recognition.match_listing(
            {"title": "Good Morning, America Volume II - Mark Power - signed GOST 2019"}
        )
        self.assertTrue(matches)
        self.assertEqual(pb.normalize(matches[0]["title"]), "good morning america volume two")

    def test_longer_exact_title_wins_when_listing_mentions_two_known_titles(self):
        matches = recognition.match_listing(
            {"title": "Hiroshi Sugimoto Time Exposed Photo Book Theaters Seascapes"}
        )
        self.assertTrue(matches)
        self.assertEqual(pb.normalize(matches[0]["title"]), "time exposed")

    def test_serious_condition_risk_suppresses_reading_copy_alert(self):
        item = {
            "title": "The Family of Man Edward Steichen",
            "description": "Ex-library paperback with withdrawn stamp",
            "publisher": "Museum of Modern Art",
            "price_gbp": 8.66,
            "private_seller": True,
            "seller_account_type": "INDIVIDUAL",
            "buying_options": ["FIXED_PRICE", "BEST_OFFER"],
        }
        match = recognition.match_listing(item)[0]
        score, reasons = recognition.opportunity_score(item, match)
        self.assertLess(score, 72)
        self.assertTrue(any(reason.startswith("condition risk:") for reason in reasons))

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
