from __future__ import annotations

import unittest
from unittest.mock import patch
from types import SimpleNamespace

import ebay_charity_issue_sync as sync


class CharityIssueSyncTests(unittest.TestCase):
    def test_reconciliation_lists_every_recent_issue_page(self):
        responses = [SimpleNamespace(stdout=__import__("json").dumps([
            {"title": "OTHER:"} for _ in range(99)
        ] + [{"title": "CHARITY_NEW: first", "body": "- **Listing:** https://www.ebay.co.uk/itm/123456789012"}])),
                     SimpleNamespace(stdout='[{"title":"CHARITY_NEW: second","body":"- **Listing:** https://www.ebay.co.uk/itm/123456789013"}]')]
        with patch.object(sync.subprocess, "run", side_effect=responses) as run:
            issues = sync.recent_issues("owner/repo", "2026-09-25T12:00:00Z")
        self.assertEqual(sync.published_ids(issues), {"123456789012", "123456789013"})
        self.assertEqual(run.call_count, 2)

    def test_rebatch_after_partial_publication_does_not_repeat_listings(self):
        published = sync.published_ids([
            {"title": "CHARITY_NEW: 2 candidates | batch 1/2 | key old",
             "body": "- **Listing:** https://www.ebay.co.uk/itm/123456789012?q=photo\n"
                     "- **Listing:** https://www.ebay.com/itm/other/123456789013\n"},
            {"title": "EXTERNAL_NEW: other source", "body": "- **Listing:** https://www.ebay.co.uk/itm/123456789014"},
        ])
        self.assertEqual(published, {"123456789012", "123456789013"})
        current = [
            {"external_id": str(number), "url": f"https://www.ebay.co.uk/itm/{number}"}
            for number in (123456789012, 123456789013, 123456789015)
        ]
        self.assertEqual([sync.listing_id(item) for item in current
                          if sync.listing_id(item) not in published], ["123456789015"])


if __name__ == "__main__":
    unittest.main()
