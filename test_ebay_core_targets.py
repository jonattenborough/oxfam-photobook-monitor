from __future__ import annotations

import unittest

import ebay_core_targets as targets


class EbayCoreTargetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = targets.load_targets()

    def test_single_source_contains_exact_tiered_175(self):
        counts = {
            tier: len(self.config["tiers"][tier]["names"])
            for tier in ("1", "2", "3")
        }
        self.assertEqual(counts, {"1": 50, "2": 70, "3": 55})
        identities = {
            targets.normalized(name)
            for tier in ("1", "2", "3")
            for name in self.config["tiers"][tier]["names"]
        }
        self.assertEqual(len(identities), 175)

    def test_grouped_queries_cover_every_name_and_alias_under_limit(self):
        grouped = targets.tier_query_groups(self.config, character_limit=90)
        for tier in ("1", "2", "3"):
            self.assertTrue(all(len(group["query"]) <= 90 for group in grouped[tier]))
            packed = {
                targets.normalized(term)
                for group in grouped[tier]
                for term in group["terms"]
            }
            expected = {
                targets.normalized(term)
                for term in targets.photographer_terms(self.config, tier)
            }
            self.assertEqual(packed, expected)

    def test_alias_matches_canonical_name_and_tier(self):
        matches = targets.matches_for_item({"title": "Sian Davey Looking for Alice hardback"})
        self.assertEqual(matches[0]["name"], "Siân Davey")
        self.assertEqual(matches[0]["tier"], "1")

    def test_name_match_does_not_accept_longer_word_substring(self):
        self.assertEqual(
            targets.matches_for_item({"title": "Tom Woodward biography"}),
            [],
        )

    def test_target_object_context_demotes_non_book_namesake(self):
        item = {
            "title": "1993-94 NBA Topps #217 Paul Graham Hawks",
            "category_id": "212",
            "category_path": "Sports Trading Cards",
        }
        self.assertEqual(targets.target_object_context(item), "name_only")

    def test_target_object_context_accepts_photographic_object(self):
        item = {
            "title": "Paul Graham photography monograph first edition",
            "category_id": "261186",
            "category_path": "Books",
        }
        self.assertEqual(targets.target_object_context(item), "supported")

    def test_target_object_context_generic_book_is_not_same_as_photo_proof(self):
        item = {
            "title": "Alec Soth uncommon hardback",
            "category_id": "261186",
            "category_path": "Books",
        }
        self.assertEqual(targets.target_object_context(item), "book_context")

    def test_target_object_context_rejects_celebrity_autobiography_even_in_books(self):
        item = {
            "title": "Guy Martin My Autobiography hardcover",
            "category_id": "261186",
            "category_path": "Books",
        }
        self.assertEqual(targets.target_object_context(item), "name_only")


if __name__ == "__main__":
    unittest.main()
