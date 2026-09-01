from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

import ebay_private_seller_monitor as monitor


class FakeClient:
    def __init__(self):
        self.calls = []

    def search(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        return []


class PrivateSellerMonitorTests(unittest.TestCase):
    def test_supplied_config_builds_expected_quota_aware_plan(self):
        config = monitor.load_config(Path("data/ebay_private_searches.json"))
        state = monitor.load_state(Path("data/ebay_private_seller_state.json"))
        plan = monitor.build_search_plan(
            config,
            state,
            datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        )
        self.assertLessEqual(len(plan), 55)
        lanes = {step["lane"] for step in plan}
        self.assertTrue(
            {"broad", "collection", "wrong_category", "hot_canon",
             "library_rotation", "contributor", "auction_ending"}.issubset(lanes)
        )

    def test_query_forces_individual_seller_filter(self):
        client = FakeClient()
        state = {
            "query_last_checked": {
                "broad:BEST_OFFER+FIXED_PRICE:photography book": "2026-09-01T11:00:00Z"
            }
        }
        items = monitor.run_query(
            client,
            state,
            lane="broad",
            query="photography book",
            category_ids="261186",
            buying_options=["FIXED_PRICE", "BEST_OFFER"],
            search_in_description=True,
            limit=30,
            delivery_country="GB",
            max_price_gbp=750,
            detected_at="2026-09-01T12:00:00Z",
            incremental=True,
            ending_start_date=None,
            ending_end_date=None,
        )
        self.assertEqual(items, [])
        call = client.calls[0]
        self.assertEqual(call["seller_account_type"], "INDIVIDUAL")
        self.assertEqual(call["delivery_country"], "GB")
        self.assertTrue(call["search_in_description"])
        self.assertEqual(call["price_max"], 750)
        self.assertEqual(call["item_start_date"], "2026-09-01T10:48:00Z")

    def test_fallback_collection_listing_can_surface_for_review(self):
        item = {
            "key": "ebay:1",
            "title": "Old photography books job lot collection",
            "context": "house clearance used books",
            "price_gbp": 20.0,
            "private_seller": True,
            "search_lane": "collection",
        }
        classified = monitor.classify(item)
        self.assertFalse(classified["recognized"])
        self.assertGreaterEqual(classified["opportunity_score"], 50)

    def test_state_has_pending_live_queue(self):
        state = monitor.load_state(Path("/path/that/does/not/exist.json"))
        self.assertEqual(state["pending_live"], {})


if __name__ == "__main__":
    unittest.main()
