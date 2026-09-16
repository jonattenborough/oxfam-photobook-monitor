from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone

import ebay_endgame as endgame


NOW = datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc)


def auction_summary(
    *,
    item_id: str = "v1|123456789012|0",
    title: str = "Old photography book",
    end_minutes: int = 80,
) -> dict:
    return {
        "itemId": item_id,
        "title": title,
        "itemWebUrl": "https://www.ebay.co.uk/itm/123456789012",
        "price": {"value": "18.00", "currency": "GBP"},
        "currentBidPrice": {"value": "18.00", "currency": "GBP"},
        "bidCount": 2,
        "seller": {"username": "house-clearance", "sellerAccountType": "INDIVIDUAL"},
        "buyingOptions": ["AUCTION"],
        "itemEndDate": endgame.utc_stamp(NOW + timedelta(minutes=end_minutes)),
    }


class FakeDetailClient:
    def __init__(self, detail: dict | None = None, error: Exception | None = None):
        self.detail = detail or {}
        self.error = error
        self.browse_calls = 0
        self._access_token = "fake"

    def get_item(self, item_id: str) -> dict:
        self.browse_calls += 1
        if self.error:
            raise self.error
        return copy.deepcopy(self.detail)


class FakePool:
    def __init__(self, client: FakeDetailClient):
        self.client = client

    def get(self, marketplace: str) -> FakeDetailClient:
        return self.client

    def sync_token(self, client: FakeDetailClient) -> None:
        return None

    @property
    def calls(self) -> int:
        return self.client.browse_calls


class EndgameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = endgame.load_config()
        cls.tasks = endgame.build_tasks(cls.config)

    def test_exact_priority_universe_and_market_coverage(self):
        counts = {tier: len(self.config["tiers"][tier]["names"]) for tier in ("1", "2", "3")}
        self.assertEqual(counts, {"1": 50, "2": 70, "3": 55})
        self.assertEqual(sum(counts.values()), 175)
        self.assertEqual(
            {row["marketplace"] for row in self.config["markets"]},
            endgame.EXPECTED_MARKETS,
        )

    def test_compiled_name_queries_are_complete_and_below_limit(self):
        for tier in ("1", "2", "3"):
            terms = endgame.photographer_terms(self.config, tier)
            groups = endgame.compile_or_queries(terms, self.config["query_character_limit"])
            self.assertTrue(groups)
            self.assertTrue(all(len(group["query"]) <= 90 for group in groups))
            packed = [term for group in groups for term in group["terms"]]
            self.assertEqual({endgame.normalized(term) for term in packed}, {endgame.normalized(term) for term in terms})

    def test_task_matrix_is_auction_only_and_within_budget(self):
        self.assertEqual(len(self.tasks), 644)
        self.assertEqual(endgame.projected_primary_calls_per_day(self.tasks), 2400.0)
        self.assertLess(endgame.projected_primary_calls_per_day(self.tasks), self.config["daily_call_cap"])
        self.assertTrue(all("delivery_country" not in task for task in self.tasks))
        self.assertTrue(all("seller_account_type" not in task for task in self.tasks))
        self.assertTrue(any(task["lane"] == "category" and task["query"] is None for task in self.tasks))
        self.assertTrue(any(task["marketplace"] == "EBAY_HK" for task in self.tasks))

    def test_bootstrap_selection_balances_all_four_lanes(self):
        selected = endgame.select_due_tasks(
            self.tasks,
            {},
            NOW,
            self.config,
            self.config["max_primary_searches_per_discovery"],
        )
        lanes = [task["lane"] for task in selected]
        self.assertEqual(len(selected), 26)
        self.assertEqual(lanes.count("known"), 14)
        self.assertEqual(lanes.count("broad"), 8)
        self.assertEqual(lanes.count("title"), 2)
        self.assertEqual(lanes.count("category"), 2)
        self.assertTrue(all(task["tier"] == "1" for task in selected if task["lane"] == "known"))

    def test_target_query_keeps_hidden_description_match_for_recall(self):
        task = next(task for task in self.tasks if task["lane"] == "known" and task["tier"] == "1")
        candidate = endgame.candidate_from_summary(
            auction_summary(title="Old hardback book see photos"),
            task,
            self.config,
            NOW,
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertGreaterEqual(candidate["opportunity_score"], 66)
        self.assertEqual(candidate["query_target_tier"], "1")
        self.assertEqual(candidate["matched_target_terms"], [])

    def test_category_sweep_rejects_obvious_unrelated_noise(self):
        task = next(task for task in self.tasks if task["lane"] == "category")
        candidate = endgame.candidate_from_summary(
            auction_summary(title="GCSE algebra revision guide"),
            task,
            self.config,
            NOW,
        )
        self.assertIsNone(candidate)

    def test_initial_alert_is_overdue_safe_and_idempotent_without_detail_budget(self):
        task = next(task for task in self.tasks if task["lane"] == "known" and task["tier"] == "1")
        candidate = endgame.candidate_from_summary(auction_summary(end_minutes=70), task, self.config, NOW)
        assert candidate is not None
        state = endgame.blank_state()
        state["candidates"][candidate["key"]] = candidate
        pool = FakePool(FakeDetailClient())
        alerts, calls = endgame.collect_deadline_alerts(self.config, state, NOW, pool, 0)
        self.assertEqual(calls, 0)
        self.assertEqual(len(alerts), 1)
        self.assertIn("LIVE STATUS NOT VERIFIED", alerts[0]["live_verification"])
        alerts_again, _ = endgame.collect_deadline_alerts(self.config, state, NOW, pool, 0)
        self.assertEqual(alerts_again, [])

    def test_detail_enrichment_recognises_description_and_auction_fields(self):
        task = next(task for task in self.tasks if task["lane"] == "known" and task["tier"] == "1")
        candidate = endgame.candidate_from_summary(
            auction_summary(title="Old hardback book see photos", end_minutes=10),
            task,
            self.config,
            NOW,
        )
        assert candidate is not None
        target = task["terms"][0]
        detail = auction_summary(title="Old hardback book see photos", end_minutes=10)
        detail.update(
            {
                "description": f"A signed photobook with photographs by {target}.",
                "estimatedAvailabilityStatus": "IN_STOCK",
                "minimumPriceToBid": {"value": "19.00", "currency": "GBP"},
                "bidCount": 3,
            }
        )
        enriched, live, reason = endgame.enrich_candidate(candidate, detail, NOW)
        self.assertTrue(live)
        self.assertEqual(reason, "live auction verified")
        self.assertIn(target, enriched["matched_target_terms"])
        self.assertGreaterEqual(enriched["opportunity_score"], 88)
        self.assertEqual(enriched["minimum_bid_value"], 19.0)
        self.assertEqual(enriched["bid_count"], 3)

    def test_final_alert_marks_initial_and_final_once(self):
        task = next(task for task in self.tasks if task["lane"] == "known" and task["tier"] == "1")
        candidate = endgame.candidate_from_summary(
            auction_summary(title=f"{task['terms'][0]} signed photobook", end_minutes=10),
            task,
            self.config,
            NOW,
        )
        assert candidate is not None
        state = endgame.blank_state()
        state["candidates"][candidate["key"]] = candidate
        pool = FakePool(FakeDetailClient())
        alerts, _ = endgame.collect_deadline_alerts(self.config, state, NOW, pool, 0)
        self.assertEqual(alerts[0]["alert_phase"], "final")
        saved = state["candidates"][candidate["key"]]
        self.assertTrue(saved["initial_alerted_at"])
        self.assertTrue(saved["final_alerted_at"])
        self.assertEqual(endgame.collect_deadline_alerts(self.config, state, NOW, pool, 0)[0], [])


if __name__ == "__main__":
    unittest.main()
