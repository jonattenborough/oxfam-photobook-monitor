from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

import ebay_private_recall_monitor as recall
import ebay_private_seller_monitor as legacy


class RecallFirstPrivateMonitorTests(unittest.TestCase):
    def test_repurposes_live_checks_into_three_extra_searches(self):
        config = recall.recall_config(legacy.load_config(Path("data/ebay_private_searches.json")))
        state = legacy.load_state(Path("/path/that/does/not/exist.json"))
        plan = legacy.build_search_plan(
            config,
            state,
            datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(config["max_live_checks_per_run"], 0)
        self.assertEqual(config["active_stock_queries_per_run"], 4)
        self.assertEqual(len(plan), 38)
        self.assertEqual(sum(step["lane"] == "active_stock" for step in plan), 4)

    def test_balanced_recall_config_uses_full_38_search_budget(self):
        config = recall.recall_config(
            legacy.load_config(Path("data/ebay_private_recall_searches.json"))
        )
        state = legacy.load_state(Path("/path/that/does/not/exist.json"))
        plan = legacy.build_search_plan(
            config,
            state,
            datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(len(plan), 38)
        self.assertEqual(sum(step["lane"] == "active_stock" for step in plan), 4)
        self.assertEqual(sum(step["lane"] == "wrong_category" for step in plan), 4)
        self.assertEqual(
            sum(step["lane"] in {"contemporary_auction", "classic_auction"} for step in plan),
            4,
        )
        self.assertEqual(
            sum(step["lane"] in {"contemporary_contributor", "classic_contributor"} for step in plan),
            0,
        )
        self.assertEqual(sum(step["lane"] == "collection" for step in plan), 2)
        self.assertEqual(config["max_live_checks_per_run"], 0)

    def test_cheap_unknown_photobook_can_cross_alert_threshold(self):
        item = {
            "key": "ebay:cheap-unknown",
            "title": "Unknown photographer photobook",
            "context": "photography book monograph",
            "price_gbp": 18.0,
            "price_value": 18.0,
            "price_currency": "GBP",
            "private_seller": True,
            "seller_account_type": "INDIVIDUAL",
            "search_lane": "broad",
        }
        classified = recall.recall_classify(item, 72)
        self.assertFalse(classified["recognized"])
        self.assertTrue(classified["recall_first_unknown"])
        self.assertGreaterEqual(classified["opportunity_score"], 72)

    def test_generic_cheap_picture_book_is_not_promoted(self):
        item = {
            "key": "ebay:picture-book",
            "title": "Henry's Freedom Box Hardcover Picture Book Ages 4-8",
            "context": "illustrated children's book",
            "price_gbp": 8.0,
            "price_value": 8.0,
            "price_currency": "GBP",
            "private_seller": True,
            "seller_account_type": "INDIVIDUAL",
            "search_lane": "active_stock",
        }
        classified = recall.recall_classify(item, 72)
        self.assertFalse(classified.get("recall_first_unknown", False))
        self.assertLess(classified["opportunity_score"], 72)

    def test_obvious_instructional_unknown_is_not_promoted(self):
        item = {
            "key": "ebay:manual",
            "title": "Digital photography handbook for kids",
            "context": "tips and techniques photography book",
            "price_gbp": 5.0,
            "price_value": 5.0,
            "price_currency": "GBP",
            "private_seller": True,
            "seller_account_type": "INDIVIDUAL",
            "search_lane": "broad",
        }
        classified = recall.recall_classify(item, 72)
        self.assertFalse(classified.get("recall_first_unknown", False))
        self.assertLess(classified["opportunity_score"], 72)

    def test_old_seen_schema_establishes_baseline_without_false_change(self):
        previous = {
            "first_seen": "2026-09-01T08:00:00Z",
            "last_seen": "2026-09-01T08:00:00Z",
            "title": "Signed photobook",
            "url": "https://www.ebay.co.uk/itm/1",
            "score": 70,
        }
        item = {
            "key": "ebay:1",
            "title": "Signed photobook",
            "price_gbp": 90.0,
            "buying_options": ["FIXED_PRICE"],
        }
        changed, reasons = recall.material_change(previous, item)
        self.assertFalse(changed)
        self.assertEqual(reasons, [])

    def test_large_price_drop_on_seen_listing_realerts(self):
        previous = {
            "observed_price_gbp": 180.0,
            "collectible_signals": [],
            "buying_options": ["FIXED_PRICE"],
        }
        item = {
            "key": "ebay:2",
            "title": "Photobook",
            "price_gbp": 95.0,
            "buying_options": ["FIXED_PRICE"],
        }
        changed, reasons = recall.material_change(previous, item)
        self.assertTrue(changed)
        self.assertTrue(any("price dropped" in reason for reason in reasons))
        self.assertTrue(any("£100" in reason for reason in reasons))

    def test_new_collectible_signal_on_seen_listing_realerts(self):
        previous = {
            "observed_price_gbp": 80.0,
            "collectible_signals": [],
            "buying_options": ["FIXED_PRICE"],
        }
        item = {
            "key": "ebay:3",
            "title": "Photobook signed first edition",
            "price_gbp": 80.0,
            "buying_options": ["FIXED_PRICE", "BEST_OFFER"],
        }
        changed, reasons = recall.material_change(previous, item)
        self.assertTrue(changed)
        combined = " | ".join(reasons).lower()
        self.assertIn("signed", combined)
        self.assertIn("first edition", combined)
        self.assertIn("best offer", combined)

    def test_material_change_cannot_be_vetoed_by_old_score_threshold(self):
        classified = {
            "key": "ebay:change",
            "opportunity_score": 48,
            "opportunity_reasons": ["ordinary prior score"],
        }
        source = {
            "material_change_reasons": ["price dropped from £140.00 to £45.00"]
        }
        promoted = recall.apply_material_change_policy(classified, source, 72)
        self.assertEqual(promoted["opportunity_score"], 72)
        self.assertTrue(promoted["recall_first_change"])
        self.assertIn("materially improved seen listing", promoted["opportunity_kind"])

    def test_recall_seen_record_retains_price_and_signals(self):
        seen = {}
        item = {
            "key": "ebay:4",
            "title": "Signed first edition photobook",
            "url": "https://www.ebay.co.uk/itm/4",
            "price_gbp": 42.0,
            "buying_options": ["FIXED_PRICE", "BEST_OFFER"],
            "opportunity_score": 80,
        }
        recall.record_seen_recall(seen, item, "2026-09-04T08:00:00Z")
        record = seen["ebay:4"]
        self.assertEqual(record["observed_price_gbp"], 42.0)
        self.assertIn("signed", record["collectible_signals"])
        self.assertIn("first edition", record["collectible_signals"])
        self.assertIn("BEST_OFFER", record["buying_options"])


if __name__ == "__main__":
    unittest.main()
