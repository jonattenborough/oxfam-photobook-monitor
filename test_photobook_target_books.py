from __future__ import annotations

import unittest

import ebay_private_recall_monitor as recall
import photobook_target_books as targets


def listing(title: str, *, price: float = 25) -> dict:
    return {
        "key": "ebay:test", "title": title, "category_id": "261186",
        "category_path": "Books", "price_gbp": price,
        "private_seller": True, "seller_account_type": "INDIVIDUAL", "search_lane": "broad",
    }


class TargetBooksTests(unittest.TestCase):
    def test_registry_includes_every_artist_and_only_curated_books(self):
        coverage = targets.coverage()
        self.assertEqual([coverage[tier]["photographers"] for tier in ("1", "2", "3")], [50, 70, 55])
        self.assertIn("Simon Wheatley", [row["name"] for row in targets.registry().values()
                                           if row["books"]])
        self.assertNotIn("Landscapes", [row["Title"] for row in
                                        targets.registry()["richard billingham"]["books"]])
        self.assertLess(coverage["3"]["with_books"], 55)

    def test_title_without_name_is_reviewed_with_price_unassessed(self):
        item = recall.recall_classify(listing("Ray's a Laugh Scalo 1996 first edition"), 72)
        self.assertGreaterEqual(item["opportunity_score"], 72)
        self.assertEqual(item["book_judgment"]["target_book"], "Richard Billingham: Ray's a Laugh")
        self.assertTrue(item["book_judgment"]["title_only"])
        self.assertEqual(item["book_judgment"]["identification_confidence"], "low")
        self.assertEqual(item["book_judgment"]["price_opportunity"], "unassessed")
        self.assertFalse(item.get("recall_first_unknown", False))

    def test_target_alias_and_later_edition_traps(self):
        original = recall.recall_classify(listing("Dont Call Me Urban Northumbria 2010"), 72)
        self.assertTrue(original["book_judgment"]["title_only"])
        self.assertGreaterEqual(original["opportunity_score"], 72)
        later = recall.recall_classify(listing("Simon Wheatley Don't Call Me Urban Backdoor Editions 2025"), 72)
        self.assertEqual(later["book_judgment"]["identification_confidence"], "conflict")
        self.assertTrue(later["book_judgment"]["known_later_edition"])
        self.assertLess(later["opportunity_score"], 72)

    def test_known_other_book_and_later_complete_works_do_not_get_first_edition_priority(self):
        for title in ("Richard Billingham Landscapes 2008 exhibition catalogue",
                      "Stephen Shore Uncommon Places The Complete Works 2015"):
            with self.subTest(title=title):
                item = recall.recall_classify(listing(title), 72)
                self.assertLess(item["opportunity_score"], 72)

    def test_common_short_titles_require_author_attribution(self):
        for title in ("Untitled Aperture 1995 photography book",
                      "The Americans paperback photo book"):
            with self.subTest(title=title):
                judgment = targets.assess_listing(listing(title))
                self.assertIsNone(judgment["target_book"])

    def test_title_groups_cover_distinctive_books_without_exceeding_query_limit(self):
        groups = targets.title_query_groups()
        self.assertTrue(all(len(group["query"]) <= 90 for group in groups))
        terms = [term for group in groups for term in group["terms"]]
        self.assertIn("Ray's a Laugh", terms)
        self.assertIn("Conversations with the Dead", terms)


if __name__ == "__main__":
    unittest.main()
