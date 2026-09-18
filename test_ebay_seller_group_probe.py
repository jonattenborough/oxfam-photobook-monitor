from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import ebay_seller_group_probe as probe
import ebay_seller_monitor as monitor


def seller(name: str, marketplace: str = "EBAY_GB", delivery: str = ""):
    row = {"id": name, "marketplace": marketplace}
    if delivery:
        row["delivery_country"] = delivery
    return row


def state_for(rows, stamps=None):
    stamps = stamps or {}
    return {
        "sellers": {
            monitor.seller_key(row["marketplace"], row["id"]): {
                "initialized": True,
                "last_successful_fetch": stamps.get(row["id"], "2026-09-18T05:00:00Z"),
            }
            for row in rows
        }
    }


class FakeClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []
        self.browse_calls = 0

    def search(self, query, **kwargs):
        self.calls.append((query, kwargs))
        self.browse_calls += 1
        return self.pages.pop(0) if self.pages else []


class GroupProbeTests(unittest.TestCase):
    def test_only_initialized_sellers_are_grouped(self):
        rows = [seller("a"), seller("b"), seller("c")]
        state = state_for(rows[:2])
        groups = probe.eligible_groups(rows, state, group_size=8)
        self.assertEqual([[row["id"] for row in group] for group in groups], [["a", "b"]])

    def test_marketplaces_and_delivery_context_do_not_mix(self):
        rows = [
            seller("uk1"),
            seller("us1", "EBAY_US", "GB"),
            seller("us2", "EBAY_US", "GB"),
        ]
        groups = probe.eligible_groups(rows, state_for(rows), group_size=8)
        self.assertEqual(
            [(group[0]["marketplace"], [row["id"] for row in group]) for group in groups],
            [("EBAY_GB", ["uk1"]), ("EBAY_US", ["us1", "us2"])],
        )

    def test_group_uses_oldest_checkpoint_with_overlap(self):
        rows = [seller("a"), seller("b")]
        state = state_for(rows, {"a": "2026-09-18T05:00:00Z", "b": "2026-09-18T05:20:00Z"})
        self.assertEqual(probe.group_incremental_start(rows, state), "2026-09-18T04:50:00Z")

    def test_group_search_passes_all_sellers_and_oldest_start(self):
        rows = [seller("a"), seller("b")]
        client = FakeClient([[{"seller": {"username": "a"}}, {"seller": {"username": "b"}}]])
        with patch.object(probe.ebay_api, "EbayBrowseClient", return_value=client):
            result = probe.scan_group(rows, state_for(rows), max_pages=2)
        self.assertTrue(result["complete_within_probe"])
        self.assertEqual(result["rows_by_seller"], {"a": 1, "b": 1})
        kwargs = client.calls[0][1]
        self.assertEqual(kwargs["seller_ids"], ["a", "b"])
        self.assertEqual(kwargs["item_start_date"], "2026-09-18T04:50:00Z")
        self.assertEqual(kwargs["category_ids"], monitor.BOOKS_CATEGORY_ID)
        self.assertTrue(kwargs["fixed_price_only"])

    def test_full_last_page_is_dense_not_complete(self):
        rows = [seller("a"), seller("b")]
        page = [{"seller": {"username": "a"}}] * 200
        client = FakeClient([page, page])
        with patch.object(probe.ebay_api, "EbayBrowseClient", return_value=client):
            result = probe.scan_group(rows, state_for(rows), max_pages=2)
        self.assertTrue(result["dense"])
        self.assertFalse(result["complete_within_probe"])
        self.assertEqual(result["browse_calls"], 2)

    def test_incomplete_or_mixed_group_is_refused(self):
        with self.assertRaises(ValueError):
            probe.eligible_groups([], {}, group_size=1)
        rows = [seller("a"), seller("b", "EBAY_US", "GB")]
        with self.assertRaises(ValueError):
            probe.scan_group(rows, state_for(rows), max_pages=1)


if __name__ == "__main__":
    unittest.main()
