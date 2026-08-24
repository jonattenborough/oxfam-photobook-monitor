import unittest

from catalogue_audit import CanonMatcher, score_item, select_queue


class CatalogueAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matcher = CanonMatcher()

    def test_marzaroli_is_collection_candidate(self):
        item = {
            "sku": "HD_TEST_1",
            "title": "Waiting for the Magic: The Photography of Oscar Marzaroli",
            "author": "Oscar Marzaroli",
            "price_gbp": 60.0,
            "description": "First edition documentary photographs of Glasgow and everyday life.",
            "publisher": "Birlinn",
        }
        scored = score_item(item, self.matcher)
        self.assertIn("collection", scored["tracks"])
        self.assertGreaterEqual(scored["collection_score"], 50)

    def test_cheap_steidl_monograph_is_both_tracks(self):
        item = {
            "sku": "HD_TEST_2",
            "title": "Sophy Rickett (Photoworks)",
            "author": "Sophy Rickett",
            "price_gbp": 15.0,
            "description": "A photographic monograph surveying the artist's work.",
            "publisher": "Steidl",
        }
        scored = score_item(item, self.matcher)
        self.assertIn("collection", scored["tracks"])
        self.assertIn("cheap", scored["tracks"])

    def test_generic_technical_manual_is_rejected(self):
        item = {
            "sku": "HD_TEST_3",
            "title": "The Digital Photography Handbook",
            "author": "A. Writer",
            "price_gbp": 3.99,
            "description": "A complete guide to photography, Photoshop and camera techniques.",
        }
        scored = score_item(item, self.matcher)
        self.assertEqual(scored["tracks"], [])

    def test_canon_name_in_description_is_not_an_exact_title_match(self):
        item = {
            "sku": "HD_TEST_DESCRIPTION_ONLY",
            "title": "A General History of Modern Photography",
            "author": "Various",
            "price_gbp": 9.99,
            "description": "Includes examples by Diane Arbus and discussion of her Aperture monograph.",
        }
        self.assertEqual(self.matcher.match(item), [])

    def test_queue_deduplicates_two_track_item(self):
        item = {
            "sku": "HD_TEST_4", "tracks": ["collection", "cheap"],
            "collection_score": 60, "cheap_score": 55, "price_gbp": 10.0,
        }
        queue = select_queue([item], 10, 10)
        self.assertEqual(len(queue), 1)


if __name__ == "__main__":
    unittest.main()
