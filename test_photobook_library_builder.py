from __future__ import annotations

import csv
import unittest
from pathlib import Path

import photobook_library_builder as builder


class PhotobookLibraryBuilderTests(unittest.TestCase):
    def test_checked_in_snapshot_has_source_backed_operating_floor(self):
        path = Path("data/photobook_recognition/openlibrary_publisher_backlists.csv")
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertGreaterEqual(len(rows), 2500)
        self.assertLessEqual(len(rows), 5000)
        self.assertTrue(all(row["Record ID"].startswith("openlibrary:") for row in rows))
        self.assertTrue(all(row["Source"].startswith("https://openlibrary.org/works/") for row in rows))

    def test_record_from_open_library_work(self):
        row = builder.record_from_doc(
            {
                "key": "/works/OL123W",
                "title": "A Serious Photobook",
                "author_name": ["Example Photographer", "Example Writer"],
                "first_publish_year": 2004,
            },
            {"name": "MACK", "tier": "B", "priority": 4},
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["Record ID"], "openlibrary:OL123W")
        self.assertEqual(row["Contributor"], "Example Photographer")
        self.assertEqual(row["Contributor aliases"], "Example Writer")
        self.assertEqual(row["Year"], "2004")
        self.assertEqual(row["Publisher"], "MACK")
        self.assertEqual(row["Source"], "https://openlibrary.org/works/OL123W")

    def test_instructional_titles_are_excluded(self):
        row = builder.record_from_doc(
            {
                "key": "/works/OL999W",
                "title": "Digital Photography for Dummies",
                "author_name": ["Example Author"],
                "first_publish_year": 2010,
            },
            {"name": "Example Press"},
        )
        self.assertIsNone(row)

    def test_merge_deduplicates_same_work_across_publishers(self):
        base = {
            field: "" for field in builder.CSV_FIELDS
        }
        base.update(
            {
                "Record ID": "openlibrary:OL1W",
                "Contributor": "Jane Example",
                "Title": "One Book",
                "Canon sources": "Publisher A",
                "Search priority": "4",
            }
        )
        second = dict(base)
        second["Canon sources"] = "Publisher B"
        rows = builder.merge_records([[base], [second]])
        self.assertEqual(len(rows), 1)
        self.assertIn("Publisher A", rows[0]["Canon sources"])
        self.assertIn("Publisher B", rows[0]["Canon sources"])


if __name__ == "__main__":
    unittest.main()
