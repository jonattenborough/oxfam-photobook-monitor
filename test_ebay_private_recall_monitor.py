from __future__ import annotations

import unittest
import json
import runpy
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import ebay_private_recall_monitor as recall
import ebay_private_seller_monitor as legacy
import ebay_seller_monitor as charity
import market_monitor


class QuotaClient:
    def __init__(self, remaining=5000, reset="2026-09-05T07:00:00Z"):
        self.remaining = remaining
        self.reset = reset

    def browse_quota(self):
        return {"remaining": self.remaining, "limit": 5000, "reset": self.reset}


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

    def test_expanded_recall_config_fills_opportunistic_70_search_ceiling(self):
        config = recall.recall_config(
            legacy.load_config(Path("data/ebay_private_recall_searches.json"))
        )
        state = legacy.load_state(Path("/path/that/does/not/exist.json"))
        plan = legacy.build_search_plan(
            config,
            state,
            datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(len(plan), config["max_api_calls_per_run"])
        self.assertEqual(len(plan), 70)
        self.assertEqual(sum(step["lane"] == "active_stock" for step in plan), 12)
        self.assertEqual(sum(step["lane"] == "wrong_category" for step in plan), 10)
        self.assertEqual(
            sum(step["lane"] in {"contemporary_auction", "classic_auction"} for step in plan),
            4,
        )
        self.assertEqual(
            sum(step["lane"] in {"contemporary_contributor", "classic_contributor"} for step in plan),
            4,
        )
        self.assertEqual(sum(step["lane"] == "collection" for step in plan), 4)
        self.assertEqual(sum(step["lane"] == "library_rotation" for step in plan), 16)
        self.assertEqual(sum(step["lane"] == "contemporary_hot" for step in plan), 6)
        self.assertEqual(sum(step["lane"] == "classic_hot" for step in plan), 6)
        self.assertEqual(config["max_live_checks_per_run"], 0)

    def config(self):
        return recall.recall_config(legacy.load_config(Path("data/ebay_private_recall_searches.json")))

    def test_pacing_accounts_for_both_shared_monitors(self):
        config = self.config()
        now = datetime(2026, 9, 4, 7, 0, tzinfo=timezone.utc)
        budget, _, warning = legacy.api_call_budget(QuotaClient(), config, now)
        self.assertEqual(config["projected_shared_calls_per_hour"], charity.DEFAULT_SELLERS_PER_RUN + 2)
        self.assertEqual(budget, 45)  # ceil((5000 - 80 - 24 * 28) / 96)
        self.assertIsNone(warning)
        for remaining in (0, 79, 80, 81, 450, 752):
            with self.subTest(remaining=remaining):
                budget, _, _ = legacy.api_call_budget(QuotaClient(remaining), config, now)
                self.assertEqual(budget, 0)

    def test_spare_quota_is_available_near_reset_but_reserve_is_protected(self):
        config = self.config()
        now = datetime(2026, 9, 5, 6, 50, tzinfo=timezone.utc)
        self.assertEqual(legacy.api_call_budget(QuotaClient(300), config, now)[0], 70)
        self.assertEqual(legacy.api_call_budget(QuotaClient(120), config, now)[0], 12)
        self.assertEqual(legacy.api_call_budget(QuotaClient(80), config, now)[0], 0)

    def test_unknown_or_stale_quota_cannot_unlock_large_ceiling(self):
        config = self.config()
        now = datetime(2026, 9, 4, 7, 0, tzinfo=timezone.utc)
        client = QuotaClient()
        with patch.object(client, "browse_quota", side_effect=RuntimeError("offline")):
            budget, quota, warning = legacy.api_call_budget(client, config, now)
        self.assertEqual(budget, 20)
        self.assertIsNone(quota)
        self.assertIn("lookup failed", warning)
        for reset in (None, "invalid", "2026-09-04T07:00:00Z", "2026-09-03T07:00:00Z"):
            with self.subTest(reset=reset):
                self.assertEqual(legacy.api_call_budget(QuotaClient(reset=reset), config, now)[0], 20)
                self.assertEqual(legacy.api_call_budget(QuotaClient(85, reset), config, now)[0], 5)
        config["max_api_calls_per_run"] = 7
        self.assertEqual(legacy.api_call_budget(QuotaClient(reset=None), config, now)[0], 7)

    def test_full_day_simulation_keeps_small_reserve_and_prioritises_private(self):
        config = self.config()
        start = datetime(2026, 9, 4, 7, 0, tzinfo=timezone.utc)
        client = QuotaClient()
        private_calls = 0
        budgets = []
        for minute in range(24 * 60):
            if minute % 60 in (2, 17, 32, 47):
                budget, _, _ = legacy.api_call_budget(client, config, start + timedelta(minutes=minute))
                self.assertLessEqual(budget, config["max_api_calls_per_run"])
                budgets.append(budget)
                private_calls += budget
                client.remaining -= budget
            elif minute % 60 == 9:
                client.remaining -= charity.DEFAULT_SELLERS_PER_RUN
            elif minute % 60 == 27:
                client.remaining -= 2
            self.assertGreaterEqual(client.remaining, config["quota_reserve"])
        self.assertEqual(len(budgets), 96)
        self.assertGreaterEqual(private_calls, 4100)
        self.assertLessEqual(private_calls, 4300)
        self.assertLessEqual(client.remaining, 150)

    def test_market_keeps_two_broad_feeds_and_non_ebay_targets(self):
        # Run the wrapper without its CLI or persistent mutations to the
        # imported market monitor, and without any network requests.
        with patch.object(legacy.ebay_api, "configured", return_value=True), \
             patch.object(market_monitor, "FEEDS", list(market_monitor.FEEDS)), \
             patch.object(market_monitor, "TARGET_MARKETS", market_monitor.TARGET_MARKETS):
            runpy.run_path("market_monitor_safe.py", run_name="market_config_test")
            feeds = [feed for feed in market_monitor.FEEDS if feed.get("kind") == "ebay_api"]
            self.assertEqual(len(feeds), 2)
            self.assertEqual(market_monitor.TARGET_MARKETS, ("abebooks",))

    def test_normal_paced_plan_protects_library_stock_and_contributors(self):
        config = self.config()
        state = {"cursors": {}}
        plan = recall.build_budgeted_search_plan(config, state, datetime.now(timezone.utc), 44)
        counts = Counter(step["lane"] for step in plan)
        self.assertEqual(len(plan), 44)
        self.assertEqual(counts["active_stock"], 10)
        self.assertEqual(counts["library_rotation"], 16)
        self.assertEqual(counts["contemporary_contributor"], 2)
        self.assertEqual(counts["classic_contributor"], 2)
        self.assertEqual(set(counts), set(recall.PACED_LANE_CALLS))
        self.assertEqual(state["cursors"]["active_stock"], 10)
        self.assertEqual(state["cursors"]["library_records"], 16)
        self.assertEqual(state["cursors"]["classic_hot_records"], counts["classic_hot"])

    def test_budget_bounds_and_zero_budget_do_not_skip_unsearched_work(self):
        config = self.config()
        now = datetime.now(timezone.utc)
        for budget in (-1, 0, 1, 3, 10, 20, 43, 44, 45, 70, 100):
            with self.subTest(budget=budget):
                state = {"cursors": {}}
                plan = recall.build_budgeted_search_plan(config, state, now, budget)
                self.assertEqual(len(plan), min(70, max(0, budget)))
                if budget <= 0:
                    self.assertEqual(state, {"cursors": {}})
        state = {"cursors": {}}
        first = recall.build_budgeted_search_plan(config, state, now, 1)
        self.assertNotIn("library_records", state["cursors"])
        second = recall.build_budgeted_search_plan(config, state, now, 1)
        self.assertEqual(first[0]["query"], "photography book")
        self.assertEqual(second[0]["query"], "photo book")

    def test_trimmed_stock_and_library_prefixes_continue_without_gaps(self):
        config = self.config()
        now = datetime.now(timezone.utc)
        state = {"cursors": {}}
        observed = {"library_rotation": [], "active_stock": []}
        # The quota permits only a prefix of each full per-run allocation.
        for _ in range(5):
            plan = recall.build_budgeted_search_plan(config, state, now, 20)
            for lane in observed:
                observed[lane].extend((step["query"], step["offset"]) for step in plan if step["lane"] == lane)
        for lane, config_key in (("library_rotation", "rotating_records_per_run"),
                                 ("active_stock", "active_stock_queries_per_run")):
            expanded = {**config, config_key: len(observed[lane])}
            expected = legacy.build_search_plan(expanded, {"cursors": {}}, now)
            self.assertEqual(observed[lane], [(step["query"], step["offset"]) for step in expected if step["lane"] == lane])

    def test_active_stock_reaches_deepest_page_and_wraps(self):
        config = self.config()
        positions = len(config["active_stock_queries"]) * 50
        state = {"cursors": {"active_stock": positions - 2}}
        plan = recall.build_budgeted_search_plan(config, state, datetime.now(timezone.utc), 44)
        stock = [step for step in plan if step["lane"] == "active_stock"]
        self.assertEqual([step["offset"] for step in stock[:3]], [9800, 9800, 0])
        self.assertTrue(all(not step["incremental"] for step in stock))

    def test_library_rotation_wraps_without_losing_tail_records(self):
        rows = list(legacy.recognition.load_library())
        hot = legacy._priority_records([row for row in rows if legacy._is_contemporary_record(row)])
        hot += legacy._priority_records([row for row in rows if not legacy._is_contemporary_record(row)])
        hot_keys = {legacy._record_identity(row) for row in hot}
        cold = [row for row in rows if legacy._record_identity(row) not in hot_keys]
        state = {"cursors": {"library_records": len(cold) - 3}}
        plan = recall.build_budgeted_search_plan(self.config(), state, datetime.now(timezone.utc), 44)
        queries = [step["query"] for step in plan if step["lane"] == "library_rotation"]
        expected = [legacy.recognition.search_query_for_record(row) for row in cold[-3:] + cold[:13]]
        self.assertEqual(queries, expected)

    def test_wrong_category_queries_all_rotate_under_normal_budget(self):
        config = self.config()
        state = {"cursors": {}}
        queries = set()
        for _ in range(5):
            plan = recall.build_budgeted_search_plan(config, state, datetime.now(timezone.utc), 44)
            for step in plan:
                if step["lane"] == "wrong_category":
                    queries.add(step["query"])
                    self.assertIsNone(step["category_ids"])
                    self.assertTrue(step["search_in_description"])
        self.assertEqual(queries, set(config["wrong_category_queries"]))
        self.assertTrue({"photo book", "photobook", "photography collection", "signed photographer book",
                         "photo books job lot", "old photo books"}.issubset(queries))

    def test_priority_photographers_get_short_title_free_active_inventory_cycle(self):
        config = self.config()
        state = {"cursors": {}}
        queries = set()
        for _ in range(7):
            plan = recall.build_budgeted_search_plan(config, state, datetime.now(timezone.utc), 44)
            for step in plan:
                if step["lane"] == "classic_contributor":
                    queries.add(step["query"])
                    self.assertFalse(step["incremental"])
                    self.assertTrue(step["search_in_description"])
        self.assertEqual(queries, set(config["priority_contributors"]))

    def test_main_uses_budgeted_recall_defaults_without_live_calls(self):
        config = self.config()
        now = datetime(2026, 9, 4, 7, 2, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            state_file = Path(tmp) / "state.json"
            with patch("sys.argv", ["recall", "--state", str(state_file), "--runtime-dir", str(runtime)]), \
                 patch.object(legacy, "utc_now", return_value=legacy.utc_stamp(now)), \
                 patch.object(legacy.ebay_api, "EbayBrowseClient", return_value=QuotaClient()), \
                 patch.object(legacy, "run_query", return_value=[]) as query, \
                 patch.object(legacy, "set_output"):
                self.assertEqual(recall.main(), 0)
            proposed = json.loads((runtime / "proposed-state.json").read_text())
            self.assertEqual(query.call_count, 45)
            self.assertEqual(proposed["last_live_checks"], 0)
            self.assertEqual(proposed["cursors"]["library_records"], config["rotating_records_per_run"])
            self.assertEqual(proposed["cursors"]["active_stock"], 10)

    def test_failed_library_query_is_retried_without_skipping_a_cursor_gap(self):
        library_attempts = 0

        def query(*args, **kwargs):
            nonlocal library_attempts
            if kwargs["lane"] == "library_rotation":
                library_attempts += 1
                if library_attempts == 2:
                    raise RuntimeError("temporary failure")
            return []

        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            with patch("sys.argv", ["recall", "--state", str(Path(tmp) / "state.json"),
                                    "--runtime-dir", str(runtime)]), \
                 patch.object(legacy, "utc_now", return_value="2026-09-04T07:02:00Z"), \
                 patch.object(legacy.ebay_api, "EbayBrowseClient", return_value=QuotaClient()), \
                 patch.object(legacy, "run_query", side_effect=query), \
                 patch.object(legacy, "set_output"):
                self.assertEqual(recall.main(), 0)
            proposed = json.loads((runtime / "proposed-state.json").read_text())
            self.assertEqual(library_attempts, 16)
            self.assertEqual(proposed["cursors"]["library_records"], 1)
            self.assertEqual(proposed["last_successful_queries"], 44)

    def test_zero_quota_main_does_not_advance_state_or_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            with patch("sys.argv", ["recall", "--state", str(Path(tmp) / "state.json"),
                                    "--runtime-dir", str(runtime)]), \
                 patch.object(legacy, "utc_now", return_value="2026-09-04T07:02:00Z"), \
                 patch.object(legacy.ebay_api, "EbayBrowseClient", return_value=QuotaClient(80)), \
                 patch.object(legacy, "run_query") as query, \
                 patch.object(legacy, "set_output") as output:
                self.assertEqual(recall.main(), 0)
            query.assert_not_called()
            output.assert_any_call("state_changed", "false")
            proposed = json.loads((runtime / "proposed-state.json").read_text())
            self.assertEqual(proposed, legacy.load_state(Path(tmp) / "state.json"))

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
