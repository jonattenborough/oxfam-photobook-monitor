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
        self.assertEqual(len(plan), 35)
        lanes = {step["lane"] for step in plan}
        self.assertTrue(
            {"broad", "collectible_format", "collection", "wrong_category",
             "contemporary_hot", "classic_hot", "library_rotation",
             "contemporary_contributor", "classic_contributor",
             "contemporary_auction", "classic_auction", "active_stock"}.issubset(lanes)
        )
        self.assertEqual(sum(step["lane"] == "contemporary_hot" for step in plan), 4)
        self.assertEqual(sum(step["lane"] == "classic_hot" for step in plan), 4)
        self.assertEqual(config["query_result_limit"], 200)
        self.assertEqual(sum(step["lane"] == "active_stock" for step in plan), 1)
        self.assertEqual(config["max_live_checks_per_run"], 3)
        self.assertEqual(sum(step["lane"] == "library_rotation" for step in plan), 8)
        self.assertEqual(config["max_api_calls_per_run"], 38)
        self.assertTrue(
            all(
                step["incremental"] is False
                for step in plan
                if step["lane"] in {"contemporary_hot", "classic_hot", "library_rotation", "active_stock"}
            )
        )

    def test_daily_schedule_uses_allowance_without_crossing_reserve(self):
        config = monitor.load_config(Path("data/ebay_private_searches.json"))
        private_workflow = Path(".github/workflows/ebay-private-seller-monitor.yml").read_text(encoding="utf-8")
        charity_workflow = Path(".github/workflows/ebay-seller-monitor.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "2,17,32,47 * * * *"', private_workflow)
        self.assertEqual(charity_workflow.count('cron: "9 * * * *"'), 1)
        self.assertIn("--sellers-per-run 51", charity_workflow)
        daily_private_calls = 96 * int(config["max_api_calls_per_run"])
        daily_charity_calls = 24 * 51
        daily_market_calls = 24 * 2
        daily_total = daily_private_calls + daily_charity_calls + daily_market_calls
        self.assertEqual(daily_total, 4920)
        self.assertLessEqual(daily_total, 5000 - int(config["quota_reserve"]))

    def test_depleted_allowance_is_paced_across_remaining_runs(self):
        class QuotaClient:
            def browse_quota(self):
                return {
                    "remaining": 450,
                    "limit": 5000,
                    "reset": "2026-09-03T07:00:00Z",
                }

        config = monitor.load_config(Path("data/ebay_private_searches.json"))
        now = datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc)
        budget, quota, warning = monitor.api_call_budget(QuotaClient(), config, now)
        self.assertEqual(budget, 4)
        self.assertEqual(quota["remaining"], 450)
        self.assertIsNone(warning)
        self.assertEqual(monitor.split_run_budget(budget, config), (4, 0))
        self.assertEqual(monitor.split_run_budget(38, config), (35, 3))

    def test_low_quota_rotates_the_highest_priority_broad_queries(self):
        config = monitor.load_config(Path("data/ebay_private_searches.json"))
        state = monitor.load_state(Path("/path/that/does/not/exist.json"))
        now = datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc)
        first = monitor.trim_search_plan(monitor.build_search_plan(config, state, now), 3)
        second = monitor.trim_search_plan(monitor.build_search_plan(config, state, now), 3)
        self.assertEqual(first[0]["query"], "photography book")
        self.assertEqual(second[0]["query"], "photo book")
        self.assertNotEqual(
            {step["query"] for step in first},
            {step["query"] for step in second},
        )

    def test_active_stock_rotation_walks_queries_then_pages(self):
        config = monitor.load_config(Path("data/ebay_private_searches.json"))
        config.update(
            {
                "broad_queries": [],
                "collectible_queries": [],
                "collection_queries": [],
                "wrong_category_queries": [],
                "active_stock_queries": ["first", "second"],
                "active_stock_queries_per_run": 1,
                "active_stock_max_offset": 400,
                "contemporary_records_per_run": 0,
                "classic_records_per_run": 0,
                "rotating_records_per_run": 0,
                "contemporary_contributor_queries_per_run": 0,
                "classic_contributor_queries_per_run": 0,
                "contemporary_auction_queries_per_run": 0,
                "classic_auction_queries_per_run": 0,
            }
        )
        state = monitor.load_state(Path("/path/that/does/not/exist.json"))
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        first = monitor.build_search_plan(config, state, now)[0]
        second = monitor.build_search_plan(config, state, now)[0]
        third = monitor.build_search_plan(config, state, now)[0]
        self.assertEqual((first["query"], first["offset"]), ("first", 0))
        self.assertEqual((second["query"], second["offset"]), ("second", 0))
        self.assertEqual((third["query"], third["offset"]), ("first", 200))

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
        self.assertEqual(call["offset"], 0)
        self.assertEqual(call["item_start_date"], "2026-09-01T10:48:00Z")

    def test_new_query_starts_from_previous_monitor_run(self):
        client = FakeClient()
        state = {
            "query_last_checked": {},
            "last_run": "2026-09-01T11:30:00Z",
        }
        monitor.run_query(
            client,
            state,
            lane="collectible_format",
            query="photography book with print",
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
        self.assertEqual(client.calls[0]["item_start_date"], "2026-09-01T11:18:00Z")

    def test_brand_new_state_uses_overlap_only_not_active_inventory(self):
        client = FakeClient()
        state = {"query_last_checked": {}}
        monitor.run_query(
            client,
            state,
            lane="collectible_format",
            query="limited edition photobook",
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
        self.assertEqual(client.calls[0]["item_start_date"], "2026-09-01T11:48:00Z")

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

    def test_collection_word_in_single_book_title_gets_no_job_lot_bonus(self):
        item = {
            "key": "ebay:2",
            "title": "The Gourmand's Egg: A Collection of Stories and Recipes",
            "context": "single hardback photography book",
            "price_gbp": 20.0,
            "private_seller": True,
            "search_lane": "collection",
        }
        classified = monitor.classify(item)
        self.assertFalse(classified["recognized"])
        self.assertNotIn("collection or job-lot wording", classified["opportunity_reasons"])
        self.assertLess(classified["opportunity_score"], 72)

    def test_instructional_book_bundle_is_not_a_collectible_alert(self):
        item = {
            "key": "ebay:3",
            "title": "Bundle of 3 photography books",
            "description": "Photography the smart way, photos that sell, and a Photofinish manual",
            "price_gbp": 6.51,
            "private_seller": True,
            "search_lane": "collection",
        }
        classified = monitor.classify(item)
        self.assertFalse(classified["recognized"])
        self.assertLess(classified["opportunity_score"], 72)
        self.assertIn(
            "instructional, technical, celebrity or local-history wording",
            classified["opportunity_reasons"],
        )

    def test_two_books_in_one_handbook_is_not_treated_as_a_job_lot(self):
        item = {
            "key": "ebay:handbook",
            "title": "Digital Photography for Kids Handbook 2 books in 1",
            "description": "First Edition Special Edition with tips and techniques",
            "price_gbp": 4.10,
            "private_seller": True,
            "search_lane": "broad",
        }
        classified = monitor.classify(item)
        self.assertFalse(classified["recognized"])
        self.assertNotIn("collection or job-lot wording", classified["opportunity_reasons"])
        self.assertLess(classified["opportunity_score"], 55)

    def test_not_signed_does_not_become_collectible_format_evidence(self):
        item = {
            "key": "ebay:unsigned",
            "title": "Elvis hardback with photographs",
            "description": "The book is not signed and is in very good condition.",
            "price_gbp": 19.53,
            "private_seller": True,
            "search_lane": "broad",
        }
        classified = monitor.classify(item)
        self.assertFalse(classified["recognized"])
        self.assertFalse(
            any(reason.startswith("evidenced collectible object:") for reason in classified["opportunity_reasons"])
        )
        self.assertLess(classified["opportunity_score"], 55)

    def test_respected_publisher_and_routine_first_edition_do_not_make_a_gem(self):
        item = {
            "key": "ebay:celebrity",
            "title": "John Wayne The Legend and the Man photography book",
            "description": "First edition trade book for fans of the performing arts.",
            "publisher": "powerHouse Books",
            "price_gbp": 11.73,
            "private_seller": True,
            "search_lane": "broad",
        }
        classified = monitor.classify(item)
        self.assertFalse(classified["recognized"])
        self.assertLess(classified["opportunity_score"], 55)

    def test_book_collection_boilerplate_is_not_seller_ignorance(self):
        item = {
            "key": "ebay:boilerplate",
            "title": "White Women Helmut Newton third printing",
            "description": "A valuable addition to any book collection.",
            "price_gbp": 20.0,
            "private_seller": True,
            "search_lane": "broad",
        }
        classified = monitor.classify(item)
        self.assertNotIn("casual seller wording", classified["opportunity_reasons"])

    def test_incomplete_component_is_penalized(self):
        item = {
            "key": "ebay:component",
            "title": "A Work in Progress Snapshots photography book",
            "description": "ISBN 9780714867007, part of ISBN 9780714866918.",
            "publisher": "Phaidon",
            "price_gbp": 13.45,
            "private_seller": True,
            "search_lane": "broad",
        }
        classified = monitor.classify(item)
        self.assertFalse(classified["recognized"])
        self.assertLess(classified["opportunity_score"], 55)
        self.assertTrue(
            any(reason.startswith("condition risk: incomplete") for reason in classified["opportunity_reasons"])
        )

    def test_unrecognized_print_edition_can_still_surface(self):
        item = {
            "key": "ebay:print-edition",
            "title": "Unknown documentary photobook limited edition with original print",
            "description": "Signed and numbered 3/30 with original pigment print.",
            "publisher": "MACK",
            "price_gbp": 50.0,
            "private_seller": True,
            "search_lane": "wrong_category",
        }
        classified = monitor.classify(item)
        self.assertFalse(classified["recognized"])
        self.assertGreaterEqual(classified["opportunity_score"], 72)
        self.assertTrue(
            any(reason.startswith("evidenced collectible object:") for reason in classified["opportunity_reasons"])
        )

    def test_state_has_pending_live_queue(self):
        state = monitor.load_state(Path("/path/that/does/not/exist.json"))
        self.assertEqual(state["pending_live"], {})

    def test_quota_budget_preserves_reserve(self):
        class QuotaClient:
            def browse_quota(self):
                return {"remaining": 470, "limit": 5000}

        config = monitor.load_config(Path("data/ebay_private_searches.json"))
        config["quota_reserve"] = 450
        budget, quota, warning = monitor.api_call_budget(QuotaClient(), config)
        self.assertEqual(budget, 20)
        self.assertEqual(quota["remaining"], 470)
        self.assertIsNone(warning)

    def test_trim_search_plan_keeps_broad_and_balanced_hot_lanes_first(self):
        plan = [
            {"lane": "library_rotation", "query": "cold"},
            {"lane": "contemporary_hot", "query": "recent"},
            {"lane": "classic_hot", "query": "classic"},
            {"lane": "broad", "query": "broad"},
            {"lane": "collection", "query": "collection"},
        ]
        selected = monitor.trim_search_plan(plan, 3)
        self.assertEqual(
            {step["lane"] for step in selected},
            {"broad", "contemporary_hot", "classic_hot"},
        )

    def test_live_detail_extracts_bibliographic_aspects(self):
        merged = monitor._merge_live_detail(
            {"title": "Example", "price_gbp": 10.0},
            {
                "localizedAspects": [
                    {"name": "Author", "value": "Diane Arbus"},
                    {"name": "Publisher", "value": "Aperture"},
                    {"name": "Publication Year", "value": "2012"},
                    {"name": "Edition", "value": "40th Anniversary Edition"},
                    {"name": "ISBN-13", "value": "9781597111751"},
                ],
                "buyingOptions": ["FIXED_PRICE"],
                "price": {"value": "20.00", "currency": "GBP"},
            },
        )
        self.assertEqual(merged["author"], "Diane Arbus")
        self.assertEqual(merged["publisher"], "Aperture")
        self.assertEqual(merged["publication_year"], "2012")
        self.assertEqual(merged["edition"], "40th Anniversary Edition")
        self.assertEqual(merged["isbn"], "9781597111751")

    def test_live_business_account_overrides_search_assumption(self):
        merged = monitor._merge_live_detail(
            {
                "title": "Example",
                "private_seller": True,
                "seller_account_type": "INDIVIDUAL",
            },
            {
                "seller": {
                    "username": "bookdealer",
                    "sellerAccountType": "BUSINESS",
                }
            },
        )
        self.assertEqual(merged["seller_account_type"], "BUSINESS")
        self.assertFalse(merged["private_seller"])


if __name__ == "__main__":
    unittest.main()
