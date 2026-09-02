from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

import ebay_private_seller_backfill as backfill
import ebay_private_seller_monitor as live_monitor


def raw_item(item_id: int, title: str, price: float = 20.0) -> dict:
    return {
        "itemId": f"v1|{item_id}|0",
        "title": title,
        "itemWebUrl": f"https://www.ebay.co.uk/itm/{item_id}",
        "price": {"value": f"{price:.2f}", "currency": "GBP"},
        "seller": {"username": "private-seller", "sellerAccountType": "INDIVIDUAL"},
        "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"],
        "itemCreationDate": "2026-08-20T10:00:00.000Z",
    }


class FakeClient:
    def __init__(self, pages, live_detail=None):
        self.pages = list(pages)
        self.live_detail = live_detail or {
            "seller": {"username": "private-seller", "sellerAccountType": "INDIVIDUAL"},
            "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"],
            "price": {"value": "20.00", "currency": "GBP"},
        }
        self.search_calls = []
        self.live_calls = []

    def search(self, query, **kwargs):
        self.search_calls.append({"query": query, **kwargs})
        return self.pages.pop(0)

    def live_status(self, item_id):
        self.live_calls.append(item_id)
        return True, "live", self.live_detail


def one_step() -> dict:
    return {
        "lane": "contemporary_exact",
        "query": "Richard Billingham Ray's a Laugh",
        "window_start": "2026-08-01T00:00:00Z",
        "window_end": "2026-09-01T00:00:00Z",
        "category_ids": "261186",
        "search_in_description": True,
        "buying_options": ["FIXED_PRICE", "BEST_OFFER"],
        "offset": 0,
    }


class PrivateSellerBackfillTests(unittest.TestCase):
    def setUp(self):
        self.config = live_monitor.load_config(Path("data/ebay_private_searches.json"))

    def test_quota_budget_keeps_larger_backfill_reserve(self):
        class QuotaClient:
            def browse_quota(self):
                return {"remaining": 1080, "limit": 5000}

        budget, quota, warning = backfill.api_call_budget(QuotaClient(), 60)
        self.assertEqual(budget, 60)
        self.assertEqual(quota["remaining"], 1080)
        self.assertIsNone(warning)

    def test_unknown_quota_uses_small_fallback_cap(self):
        class BrokenQuotaClient:
            def browse_quota(self):
                raise RuntimeError("temporary failure")

        budget, quota, warning = backfill.api_call_budget(BrokenQuotaClient(), 60)
        self.assertEqual(budget, backfill.UNKNOWN_QUOTA_CAP)
        self.assertIsNone(quota)
        self.assertIn("limiting this backfill", warning)

    def test_accelerator_can_use_a_custom_cap_and_reserve(self):
        class QuotaClient:
            def browse_quota(self):
                return {"remaining": 3500, "limit": 5000}

        budget, quota, warning = backfill.api_call_budget(
            QuotaClient(),
            3000,
            hard_cap=2750,
            quota_reserve=750,
        )
        self.assertEqual(budget, 2750)
        self.assertEqual(quota["remaining"], 3500)
        self.assertIsNone(warning)

        budget, _, _ = backfill.api_call_budget(
            QuotaClient(),
            450,
            hard_cap=450,
            quota_reserve=3250,
        )
        self.assertEqual(budget, 250)

    def test_plan_prioritizes_collectible_curated_and_broad_coverage(self):
        start = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        end = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        plan = backfill.build_backfill_plan(self.config, start, end, slice_days=7)
        self.assertGreaterEqual(len(plan), 150)
        self.assertEqual(
            [step["lane"] for step in plan[:10]],
            ["collectible_format"] * 4 + ["collection"] * 4 + ["wrong_category"] * 2,
        )
        self.assertEqual(
            [step["lane"] for step in plan[10:34]],
            ["contemporary_exact", "classic_exact"] * 12,
        )
        self.assertTrue(all(step["lane"] == "broad" for step in plan[34:54]))
        self.assertEqual(len({(step["window_start"], step["window_end"]) for step in plan[34:54]}), 5)
        self.assertEqual(sum(step["lane"] == "contemporary_exact" for step in plan), 73)
        self.assertEqual(sum(step["lane"] == "classic_exact" for step in plan), 50)

    def test_adaptive_allocator_favors_verification_when_queue_is_full(self):
        self.assertEqual(backfill.live_check_reserve(250, 61, 60, 0), 40)
        self.assertEqual(backfill.live_check_reserve(0, 61, 60, 0), 12)
        self.assertEqual(backfill.live_check_reserve(250, 61, 60, 6), 6)
        self.assertEqual(backfill.effective_live_check_budget(0, 20, 49, 0), 49)
        self.assertEqual(backfill.effective_live_check_budget(0, 20, 49, 1), 20)
        self.assertEqual(backfill.effective_live_check_budget(6, 6, 49, 0), 6)

    def test_completed_window_is_not_restarted_without_explicit_flag(self):
        state = {"version": 1, "queue": [], "pending_live": {}, "complete": True}
        changed = backfill.initialize_state(
            state,
            self.config,
            {"last_run": "2026-09-01T15:09:30Z"},
            detected_at="2026-09-01T16:00:00Z",
            lookback_days=30,
            slice_days=7,
            new_window=False,
        )
        self.assertFalse(changed)
        self.assertEqual(state["queue"], [])
        self.assertTrue(state["complete"])

    def test_legacy_window_gets_classic_searches_without_restart(self):
        state = {
            "version": 1,
            "window_start": "2026-08-02T12:00:00Z",
            "window_end": "2026-09-01T12:00:00Z",
            "slice_days": 7,
            "initial_plan_size": 103,
            "queue": [one_step()],
            "pending_live": {},
            "complete": False,
        }
        changed = backfill.initialize_state(
            state,
            self.config,
            {"last_run": "2026-09-01T15:09:30Z"},
            detected_at="2026-09-01T16:00:00Z",
            lookback_days=30,
            slice_days=7,
            new_window=False,
        )
        self.assertTrue(changed)
        self.assertEqual(state["plan_version"], backfill.PLAN_VERSION)
        self.assertEqual(sum(step["lane"] == "classic_exact" for step in state["queue"]), 50)
        self.assertEqual(state["queue"][-1], one_step())

    def test_search_page_uses_historical_private_seller_filters(self):
        client = FakeClient([[]])
        items, count = backfill.search_page(client, self.config, one_step())
        self.assertEqual(items, [])
        self.assertEqual(count, 0)
        call = client.search_calls[0]
        self.assertEqual(call["seller_account_type"], "INDIVIDUAL")
        self.assertEqual(call["item_start_date"], "2026-08-01T00:00:00Z")
        self.assertEqual(call["item_end_date"], "2026-09-01T00:00:00Z")
        self.assertEqual(call["limit"], 200)
        self.assertEqual(call["offset"], 0)

    def test_backfill_live_verifies_and_stores_strong_candidate(self):
        item = raw_item(123456789012, "Richard Billingham Rays a Laugh old photography book")
        client = FakeClient(
            [[item]],
            live_detail={
                "seller": {"username": "private-seller", "sellerAccountType": "INDIVIDUAL"},
                "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"],
                "price": {"value": "20.00", "currency": "GBP"},
                "description": "Old photography book from a house clearance",
            },
        )
        state = {"version": 1, "queue": [one_step()], "pending_live": {}, "complete": False}
        findings = {"version": 1, "items": {}}
        result = backfill.run_backfill(
            client,
            self.config,
            state,
            findings,
            {"seen": {}, "pending_live": {}},
            call_budget=2,
            max_live_checks=1,
            detected_at="2026-09-01T16:00:00Z",
        )
        self.assertEqual(result["calls"], 2)
        self.assertEqual(result["live_checks"], 1)
        self.assertEqual(len(result["new_candidates"]), 1)
        stored = findings["items"]["ebay:123456789012"]
        self.assertTrue(stored["live_verified"])
        self.assertEqual(stored["seller_account_type"], "INDIVIDUAL")

    def test_live_business_seller_is_not_retained(self):
        item = raw_item(123456789013, "Richard Billingham Rays a Laugh old photography book")
        client = FakeClient(
            [[item]],
            live_detail={
                "seller": {"username": "book-dealer", "sellerAccountType": "BUSINESS"},
                "buyingOptions": ["FIXED_PRICE"],
                "price": {"value": "20.00", "currency": "GBP"},
            },
        )
        state = {"version": 1, "queue": [one_step()], "pending_live": {}, "complete": False}
        findings = {"version": 1, "items": {}}
        result = backfill.run_backfill(
            client,
            self.config,
            state,
            findings,
            {"seen": {}, "pending_live": {}},
            call_budget=2,
            max_live_checks=1,
            detected_at="2026-09-01T16:00:00Z",
        )
        self.assertEqual(result["new_candidates"], [])
        self.assertEqual(findings["items"], {})
        self.assertEqual(state["pending_live"], {})

    def test_full_page_is_requeued_at_next_offset(self):
        rows = [raw_item(200000000000 + index, f"Ordinary novel {index}") for index in range(200)]
        client = FakeClient([rows])
        state = {"version": 1, "queue": [one_step()], "pending_live": {}, "complete": False}
        result = backfill.run_backfill(
            client,
            self.config,
            state,
            {"version": 1, "items": {}},
            {"seen": {}, "pending_live": {}},
            call_budget=1,
            max_live_checks=0,
            detected_at="2026-09-01T16:00:00Z",
        )
        self.assertEqual(result["truncated_pages_requeued"], 1)
        self.assertEqual(state["queue"][0]["offset"], 200)

    def test_live_monitor_seen_item_is_excluded(self):
        item = raw_item(123456789014, "Richard Billingham Rays a Laugh old photography book")
        client = FakeClient([[item]])
        state = {"version": 1, "queue": [one_step()], "pending_live": {}, "complete": False}
        result = backfill.run_backfill(
            client,
            self.config,
            state,
            {"version": 1, "items": {}},
            {"seen": {"ebay:123456789014": {}}, "pending_live": {}},
            call_budget=2,
            max_live_checks=1,
            detected_at="2026-09-01T16:00:00Z",
        )
        self.assertEqual(result["live_checks"], 0)
        self.assertEqual(state["pending_live"], {})

    def test_retained_findings_are_rescored_and_false_positive_is_removed(self):
        false_item = {
            "key": "ebay:999",
            "title": "A single photography book",
            "description": "An ideal addition to any photography book collection",
            "price_gbp": 20.0,
            "private_seller": True,
            "search_lane": "collection",
            "live_verified": True,
        }
        findings = {"version": 1, "items": {"ebay:999": false_item}}
        state = {"version": 1, "queue": [], "pending_live": {}, "complete": False}
        backfill.run_backfill(
            FakeClient([]),
            self.config,
            state,
            findings,
            {"seen": {}, "pending_live": {}},
            call_budget=0,
            max_live_checks=0,
            detected_at="2026-09-01T16:00:00Z",
        )
        self.assertEqual(findings["items"], {})

    def test_reclassify_helper_needs_no_api_and_removes_false_positive(self):
        findings = {
            "version": 1,
            "items": {
                "ebay:false": {
                    "key": "ebay:false",
                    "title": "Digital Photography for Kids Handbook 2 books in 1",
                    "description": "First Edition with tips and techniques",
                    "price_gbp": 4.10,
                    "private_seller": True,
                    "search_lane": "broad",
                },
                "ebay:strong": {
                    "key": "ebay:strong",
                    "title": "Richard Billingham Ray's a Laugh old photography book",
                    "context": "Used book from house clearance",
                    "price_gbp": 20.0,
                    "private_seller": True,
                    "seller_account_type": "INDIVIDUAL",
                    "buying_options": ["FIXED_PRICE"],
                },
            },
        }
        result = backfill.reclassify_retained_findings(findings, 72)
        self.assertEqual(
            result,
            {"before": 2, "retained": 1, "removed": 1, "changed": 2},
        )
        self.assertNotIn("ebay:false", findings["items"])
        self.assertIn("ebay:strong", findings["items"])


if __name__ == "__main__":
    unittest.main()
