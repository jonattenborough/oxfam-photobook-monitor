from __future__ import annotations

import unittest

import ebay_private_issue_sync as sync
import ebay_private_recall_monitor as recall


class PrivateIssueSyncTests(unittest.TestCase):
    def test_batch_reorder_keeps_fingerprint_but_price_drop_changes_it(self):
        item = {"key": "ebay:123456789012", "price_gbp": 29,
                "title": "Ray's a Laugh original photobook"}
        fingerprint = recall.alert_fingerprint(item)
        self.assertEqual(fingerprint, recall.alert_fingerprint({**item, "search_lane": "broad"}))
        self.assertNotEqual(fingerprint, recall.alert_fingerprint({**item, "price_gbp": 19}))
        old = [{"title": "EBAY_PRIVATE_NEW: HOT 10", "body": f"- **Alert fingerprint:** {fingerprint}\n"},
               {"title": "CHARITY_NEW: 1", "body": f"- **Alert fingerprint:** {fingerprint}\n"}]
        self.assertEqual(sync.published_fingerprints(old), {fingerprint})


if __name__ == "__main__":
    unittest.main()
