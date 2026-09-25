"""Checks that issue packets become distinct, traceable listing rows."""

import csv
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
import export_review_backlog as exporter

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def issue(number, title, body, comments=0):
    return {
        "number": number, "title": title, "body": body, "created_at": f"2026-09-24T{number % 24:02}:00:00Z",
        "updated_at": "2026-09-25T08:00:00Z", "comments": comments,
        "html_url": f"https://github.com/test/repo/issues/{number}",
        "comments_url": f"https://api.github.com/repos/test/repo/issues/{number}/comments",
    }


class ExportBacklogTests(unittest.TestCase):
    def test_private_packet_fields_and_warning_are_separated(self):
        source = issue(1, "EBAY_PRIVATE_NEW: HOT 1", """### DISCOVERY 72/100 - Robert Frank The Americans

- **Observed price:** £49.50
- **Private seller:** person
- **Listing:** https://www.ebay.co.uk/itm/123456789012?_skw=book
- **Best recognition:** Robert Frank, *The Americans* | match 100/100 | tier S
- **Verification:** SEARCH RESULT ONLY at 2026-09-24T00:00:00Z
- **Main image:** https://example.org/picture.jpg

### Temporary search warnings

- fetch error
""")
        rows = exporter.parse_issue(source, "EBAY_PRIVATE_NEW", NOW)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["listing_title"], "Robert Frank The Americans")
        self.assertEqual(row["photographer"], "Robert Frank")
        self.assertEqual(row["listing_id"], "123456789012")
        self.assertEqual(row["price"], "49.50")
        self.assertEqual(row["total_price"], "")
        self.assertEqual(row["recognition_tier"], "S")
        self.assertEqual(row["listing_status"], "search_result_only")

    def test_auction_subdomain_url_and_expiry(self):
        source = issue(2, "ENDGAME_4H: 1 auction", """### [Photobook](https://www.benl.ebay.be/itm/123456789012?_skw=test)

- **Ends:** 2026-09-25T11:00:00Z - one hour
- **Bid/price:** EUR 10.50 plus EUR 3.25 shipping - 2 bids
- **Seller:** seller (INDIVIDUAL)
- **Priority target:** The Americans (Tier 1)

![Listing image](https://example.org/photo.jpg)
""")
        row = exporter.parse_issue(source, "ENDGAME_4H", NOW)[0]
        self.assertEqual(row["listing_title"], "Photobook")
        self.assertEqual(row["listing_id"], "123456789012")
        self.assertEqual(row["total_price"], "13.75")
        self.assertEqual(row["auction_status"], "expired")
        self.assertEqual(row["recognition_tier"], "1")
        self.assertEqual(row["image_url"], "https://example.org/photo.jpg")

    def test_dedupe_preserves_all_issue_numbers_and_newest_price(self):
        earlier = exporter.parse_issue(issue(3, "EBAY_PRIVATE_NEW: HOT 1", """### Book

- **Observed price:** £25.00
- **Listing:** https://www.ebay.co.uk/itm/123456789012?hash=earlier
"""), "EBAY_PRIVATE_NEW", NOW)
        later = exporter.parse_issue(issue(4, "EXTERNAL_NEW: 1 market match", """### Book

- **Observed price:** £20.00
- **Listing:** https://www.ebay.de/itm/123456789012?hash=later
"""), "EXTERNAL_NEW", NOW)
        row = exporter.deduplicate(earlier + later)[0]
        self.assertEqual(row["price"], "20.00")
        self.assertEqual(row["source_issue_numbers"], "3; 4")
        self.assertEqual(row["queues"], "EBAY_PRIVATE_NEW; EXTERNAL_NEW")
        self.assertEqual(row["duplicate_count"], 2)

    def test_review_receipt_requires_owner_and_matching_signature(self):
        source = issue(5, "CHARITY_NEW: candidate", "", comments=1)
        cache = {"5": {"signature": [source["updated_at"], 1], "receipt": {"created_at": "2026-09-25T08:00:00Z"}}}
        self.assertEqual(exporter.review_status(source, "test", cache, "", False), "reviewed")
        source["comments"] = 2
        self.assertEqual(exporter.review_status(source, "test", cache, "", False), "review_unverified")

    def test_export_omits_historical_and_escapes_spreadsheet_formulas(self):
        active = issue(6, "OXFAM_NEW: 1 listing", """### =HYPERLINK("bad")

- **SKU:** `HD_123456789`
- **Oxfam price:** £12.00
- **Possible product URL:** https://onlineshop.oxfam.org.uk/book/product/HD_123456789
""")
        historical = issue(7, "EBAY_PRIVATE_NEW: FULL LIBRARY 1/1", "### Old\n\n- **Listing:** https://ebay.co.uk/itm/123456789012\n")
        with tempfile.TemporaryDirectory() as directory:
            summary = exporter.export([active, historical], Path(directory) / "csvs", "test/repo", NOW)
            with (Path(directory) / "csvs/all_backlog.csv").open(encoding="utf-8-sig") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["listing_title"], "'=HYPERLINK(\"bad\")")
            self.assertEqual(summary["excluded_issue_counts"]["historical_full_library"], 1)
            self.assertEqual(summary["unique_listings"], 1)
            self.assertTrue(Path(summary["zip_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
