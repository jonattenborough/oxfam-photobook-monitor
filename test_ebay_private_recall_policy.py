from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

import ebay_private_alert_builder as alerts
import ebay_private_recall_policy as recall
import ebay_private_seller_monitor as monitor


class EbayPrivateRecallPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = dict(recall.DEFAULT_POLICY)

    def test_runtime_config_turns_all_38_calls_into_searches(self):
        base = recall._ORIGINAL_LOAD_CONFIG(Path("data/ebay_private_searches.json"))
        config = recall.apply_runtime_config(base, self.policy)
        state = monitor.load_state(Path("/path/that/does/not/exist.json"))
        plan = monitor.build_search_plan(
            config,
            state,
            datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(config["max_live_checks_per_run"], 0)
        self.assertEqual(config["active_stock_queries_per_run"], 4)
        self.assertEqual(config["max_pending_live_checks"], 1000)
        self.assertEqual(len(plan), 38)
        self.assertEqual(sum(step["lane"] == "active_stock" for step in plan), 4)
        self.assertEqual(monitor.split_run_budget(38, config), (38, 0))

    def test_cheap_unknown_photobook_reaches_human_review(self):
        item = {
            "key": "ebay:unknown",
            "title": "Unknown documentary photobook hardcover",
            "context": "Photography monograph from a private collection",
            "price_gbp": 18.0,
            "landed_price_gbp": 21.0,
            "private_seller": True,
            "seller_account_type": "INDIVIDUAL",
            "search_lane": "active_stock",
            "category_id": monitor.BOOKS_CATEGORY_ID,
            "buying_options": ["FIXED_PRICE"],
        }
        classified = recall.classify_with_policy(item, self.policy)
        self.assertFalse(classified["recognized"])
        self.assertTrue(classified["unknown_bargain"])
        self.assertGreaterEqual(classified["opportunity_score"], 72)
        self.assertEqual(classified["opportunity_kind"], "cheap unrecognised photobook lead")

    def test_cheap_camera_manual_is_still_rejected(self):
        item = {
            "key": "ebay:manual",
            "title": "Digital Photography Handbook and Camera Manual",
            "context": "Tips and techniques for improving photography skills",
            "price_gbp": 5.0,
            "landed_price_gbp": 8.0,
            "private_seller": True,
            "seller_account_type": "INDIVIDUAL",
            "search_lane": "active_stock",
            "category_id": monitor.BOOKS_CATEGORY_ID,
            "buying_options": ["FIXED_PRICE"],
        }
        classified = recall.classify_with_policy(item, self.policy)
        self.assertFalse(classified.get("unknown_bargain", False))
        self.assertLess(classified["opportunity_score"], 72)

    def test_existing_seen_item_is_silently_given_a_price_baseline(self):
        state = {
            "seen": {
                "ebay:1": {
                    "first_seen": "2026-09-01T08:00:00Z",
                    "last_seen": "2026-09-01T08:00:00Z",
                }
            }
        }
        item = {
            "key": "ebay:1",
            "title": "Known photobook",
            "url": "https://www.ebay.co.uk/itm/1",
            "landed_price_gbp": 100.0,
            "buying_options": ["FIXED_PRICE"],
        }
        result = recall.prepare_query_items(
            state,
            [item],
            detected_at="2026-09-04T08:00:00Z",
            policy=self.policy,
        )[0]
        self.assertEqual(result["key"], "ebay:1")
        self.assertEqual(state["seen"]["ebay:1"]["tracking_version"], 1)
        self.assertEqual(state["seen"]["ebay:1"]["observed_price_gbp"], 100.0)

    def test_material_price_drop_gets_a_new_review_key(self):
        state = {
            "seen": {
                "ebay:2": {
                    "first_seen": "2026-09-01T08:00:00Z",
                    "last_seen": "2026-09-01T08:00:00Z",
                    "tracking_version": 1,
                    "observed_price_gbp": 120.0,
                    "observed_buying_options": ["FIXED_PRICE"],
                    "observed_collectible_signals": [],
                    "observed_title": "known photobook",
                }
            }
        }
        item = {
            "key": "ebay:2",
            "title": "Known photobook",
            "url": "https://www.ebay.co.uk/itm/2",
            "landed_price_gbp": 79.0,
            "buying_options": ["FIXED_PRICE"],
        }
        result = recall.prepare_query_items(
            state,
            [item],
            detected_at="2026-09-04T08:00:00Z",
            policy=self.policy,
        )[0]
        self.assertEqual(result["base_key"], "ebay:2")
        self.assertTrue(result["key"].startswith("ebay:2:change:"))
        self.assertTrue(result["material_change"])
        self.assertTrue(
            any(reason.startswith("price reduced") for reason in result["material_change_reasons"])
        )

    def test_new_signed_wording_reopens_a_seen_listing(self):
        previous = {
            "tracking_version": 1,
            "observed_price_gbp": 80.0,
            "observed_buying_options": ["FIXED_PRICE"],
            "observed_collectible_signals": [],
            "observed_title": "documentary photobook",
        }
        current = recall.tracking_snapshot(
            {
                "title": "Documentary photobook signed by photographer",
                "landed_price_gbp": 80.0,
                "buying_options": ["FIXED_PRICE"],
            }
        )
        reasons = recall.material_change_reasons(previous, current, self.policy)
        self.assertIn("new collector wording: signed or inscribed", reasons)

    def test_changed_listing_is_recorded_against_original_item_key(self):
        seen = {}
        item = {
            "key": "ebay:3:change:abcdef",
            "base_key": "ebay:3",
            "title": "Photobook price reduced",
            "url": "https://www.ebay.co.uk/itm/3",
            "landed_price_gbp": 60.0,
            "buying_options": ["FIXED_PRICE", "BEST_OFFER"],
            "material_change": True,
        }
        recall.record_seen_with_tracking(seen, item, "2026-09-04T08:00:00Z")
        self.assertIn("ebay:3", seen)
        self.assertNotIn("ebay:3:change:abcdef", seen)
        self.assertEqual(seen["ebay:3"]["observed_price_gbp"], 60.0)
        self.assertEqual(seen["ebay:3"]["last_alert_type"], "material_change")

    def test_fast_triage_puts_sub_100_special_before_expensive_high_score(self):
        cheap_special = {
            "title": "Signed documentary photobook",
            "opportunity_score": 81,
            "landed_price_gbp": 40.0,
            "best_recognition": {"collectibility_tier": "A"},
        }
        expensive = {
            "title": "Expensive canonical photobook",
            "opportunity_score": 88,
            "landed_price_gbp": 500.0,
            "best_recognition": {"collectibility_tier": "S"},
        }
        ordered = sorted([expensive, cheap_special], key=alerts.priority_key)
        self.assertIs(ordered[0], cheap_special)
        title = alerts._packet_title(ordered, 1, 1)
        self.assertIn("HOT", title)
        self.assertIn("under £100", title)


if __name__ == "__main__":
    unittest.main()
