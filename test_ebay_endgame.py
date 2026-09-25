from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
        self.item_ids: list[str] = []

    def get_item(self, item_id: str) -> dict:
        self.browse_calls += 1
        self.item_ids.append(item_id)
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


class FakeSearchClient(FakeDetailClient):
    def __init__(self, dense=False):
        super().__init__()
        self.dense = dense

    def search_page(self, query, **kwargs):
        self.browse_calls += 1
        return {"itemSummaries": [], "next": "https://api.ebay.com/next" if self.dense else None}

    def search_next(self, url):
        self.browse_calls += 1
        return {"itemSummaries": [], "next": "https://api.ebay.com/next" if self.dense else None}


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
        self.assertEqual(self.config["initial_alert_minutes"], 48 * 60)
        self.assertEqual(self.config["final_alert_minutes"], 4 * 60)
        self.assertEqual(
            {tier: self.config["tiers"][tier]["horizon_hours"] for tier in ("1", "2", "3")},
            {"1": 72, "2": 72, "3": 72},
        )
        self.assertEqual(self.config["title_search"]["horizon_hours"], 72)
        self.assertEqual(self.config["unicorn_search"]["horizon_hours"], 72)
        self.assertEqual(sum(self.config["lane_slots"].values()), 26)
        unicorn_tasks = [task for task in self.tasks if task["lane"] == "unicorn"]
        self.assertEqual(len(unicorn_tasks), 25 * len(self.config["markets"]))
        self.assertEqual({task["unicorn_tier"] for task in unicorn_tasks}, {"A"})

    def test_two_day_warning_boundary_and_no_duplicate_early_alert(self):
        self.assertTrue(all(task["horizon_hours"] == 72 for task in self.tasks))
        candidate = {"item_end_date": endgame.utc_stamp(NOW + timedelta(hours=48, minutes=1))}
        self.assertIsNone(endgame.due_phase(candidate, NOW, self.config))
        candidate["item_end_date"] = endgame.utc_stamp(NOW + timedelta(hours=48))
        self.assertEqual(endgame.due_phase(candidate, NOW, self.config)[0], "initial")
        candidate["initial_alerted_at"] = endgame.utc_stamp(NOW)
        self.assertIsNone(endgame.due_phase(candidate, NOW + timedelta(hours=40), self.config))
        self.assertEqual(endgame.due_phase(candidate, NOW + timedelta(hours=44), self.config)[0], "final")

    def test_compiled_name_queries_are_complete_and_below_limit(self):
        for tier in ("1", "2", "3"):
            terms = endgame.photographer_terms(self.config, tier)
            groups = endgame.compile_or_queries(terms, self.config["query_character_limit"])
            self.assertTrue(groups)
            self.assertTrue(all(len(group["query"]) <= 90 for group in groups))
            packed = [term for group in groups for term in group["terms"]]
            self.assertEqual({endgame.normalized(term) for term in packed}, {endgame.normalized(term) for term in terms})

    def test_task_matrix_is_auction_only_and_within_budget(self):
        self.assertEqual(len({task["key"] for task in self.tasks}), len(self.tasks))
        self.assertGreaterEqual(len(self.tasks), 1000)
        self.assertLess(endgame.projected_primary_calls_per_day(self.tasks), self.config["daily_call_cap"])
        self.assertTrue(all("delivery_country" not in task for task in self.tasks))
        self.assertTrue(all("seller_account_type" not in task for task in self.tasks))
        self.assertTrue(any(task["lane"] == "category" and task["query"] is None for task in self.tasks))
        self.assertTrue(any(task["marketplace"] == "EBAY_HK" for task in self.tasks))
        self.assertFalse(any(task["lane"] == "category" and task["marketplace"] == "EBAY_BE" for task in self.tasks))

    def test_bootstrap_selection_balances_all_five_lanes(self):
        selected = endgame.select_due_tasks(
            self.tasks,
            {},
            NOW,
            self.config,
            self.config["max_primary_searches_per_discovery"],
        )
        lanes = [task["lane"] for task in selected]
        self.assertEqual(len(selected), 26)
        self.assertEqual(lanes.count("known"), 12)
        self.assertEqual(lanes.count("broad"), 5)
        self.assertEqual(lanes.count("unicorn"), 5)
        self.assertEqual(lanes.count("title"), 2)
        self.assertEqual(lanes.count("category"), 2)
        known = [task for task in selected if task["lane"] == "known"]
        self.assertEqual([sum(task["tier"] == tier for task in known) for tier in ("1", "2", "3")], [6, 4, 2])
        major = {row["marketplace"] for row in self.config["markets"] if row.get("major")}
        self.assertTrue(all(task["marketplace"] in major for task in known))

    def test_catchup_preserves_lane_and_tier_allocation_without_duplicates(self):
        selected = endgame.select_due_tasks(self.tasks, {}, NOW, self.config, 52)
        self.assertEqual(len({task["key"] for task in selected}), 52)
        self.assertEqual([sum(task["lane"] == lane for task in selected)
                          for lane in ("known", "broad", "unicorn", "title", "category")],
                         [24, 10, 10, 4, 4])
        self.assertEqual([sum(task["lane"] == "known" and task["tier"] == tier for task in selected)
                          for tier in ("1", "2", "3")], [12, 8, 4])

    def test_delayed_discovery_scales_but_normal_cadence_does_not(self):
        state = endgame.blank_state()
        state["schedule"] = {task["key"]: endgame.utc_stamp(NOW - timedelta(hours=30)) for task in self.tasks}
        state["last_discovery_at"] = endgame.utc_stamp(NOW - timedelta(hours=5))
        self.assertEqual(endgame.discovery_limits(self.config, state, NOW), (520, 80))
        state["last_discovery_at"] = endgame.utc_stamp(NOW - timedelta(minutes=15))
        self.assertEqual(endgame.discovery_limits(self.config, state, NOW), (26, 4))
        state["last_discovery_at"] = endgame.utc_stamp(NOW - timedelta(days=3))
        self.assertEqual(endgame.discovery_limits(self.config, state, NOW), (624, 96))

    def test_bootstrap_can_search_all_tiers_within_two_cycles(self):
        state = endgame.blank_state()
        pool = FakePool(FakeSearchClient())
        endgame.run_cycle(self.config, state, NOW, pool, 3600)
        coverage = endgame.coverage_status(self.tasks, state, NOW)
        primary_cap, _ = endgame.discovery_limits(self.config, endgame.blank_state(), NOW)
        self.assertLessEqual(coverage["never_searched"], max(0, len(self.tasks) - primary_cap))
        self.assertTrue(all(count < 130 for count in coverage["never_searched_by_tier"].values()))
        endgame.run_cycle(self.config, state, NOW + timedelta(minutes=15), pool, 3600 - pool.calls)
        self.assertEqual(endgame.coverage_status(self.tasks, state, NOW)["never_searched"], 0)

    def test_catchup_cannot_exceed_available_quota_even_with_dense_pages(self):
        for budget in (0, 1, 5, 35, 720):
            with self.subTest(budget=budget):
                state = endgame.blank_state()
                pool = FakePool(FakeSearchClient(dense=True))
                stats, alerts, used, details = endgame.run_cycle(self.config, state, NOW, pool, budget)
                self.assertLessEqual(used, budget)
                self.assertEqual(used, pool.calls)

    def test_overdue_tier_three_gets_turn_despite_tier_one_bootstrap(self):
        schedule = {task["key"]: endgame.utc_stamp(NOW - timedelta(hours=30))
                    for task in self.tasks if task.get("tier") == "3"}
        selected = endgame.select_due_tasks(self.tasks, schedule, NOW, self.config, 26)
        self.assertEqual(sum(task["lane"] == "known" and task["tier"] == "3" for task in selected), 2)

    def test_unicorn_lane_promotes_exact_pair_but_not_hidden_match_to_alert(self):
        task = next(
            task for task in self.tasks
            if task["lane"] == "unicorn"
            and task.get("unicorn_target", {}).get("Title") == "Naked City"
            and task["marketplace"] == "EBAY_GB"
        )
        exact = endgame.candidate_from_summary(
            auction_summary(title="Weegee Naked City 1945 first edition photography book"),
            task,
            self.config,
            NOW,
        )
        self.assertIsNotNone(exact)
        assert exact is not None
        self.assertEqual(exact["target_match_quality"], "exact_pair")
        self.assertGreaterEqual(exact["opportunity_score"], 92)
        self.assertEqual(exact["unicorn_target_tier"], "A")

        hidden = endgame.candidate_from_summary(
            auction_summary(title="Old hardback photography book see description"),
            task,
            self.config,
            NOW,
        )
        self.assertIsNotNone(hidden)
        assert hidden is not None
        self.assertEqual(hidden["target_match_quality"], "hidden_description")
        self.assertEqual(hidden["opportunity_score"], 66)
        self.assertLess(hidden["opportunity_score"], self.config["final_alert_score"])

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

    def test_visible_sports_namesake_is_retained_below_alert_threshold(self):
        task = next(
            task for task in self.tasks
            if task["lane"] == "known" and "Paul Graham" in task.get("terms", [])
        )
        summary = auction_summary(title="1993-94 NBA Topps #217 Paul Graham Hawks", end_minutes=180)
        summary["categories"] = [{"categoryId": "212"}]
        summary["categoryPath"] = "Sports Trading Cards"
        candidate = endgame.candidate_from_summary(summary, task, self.config, NOW)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["target_match_quality"], "name_only")
        self.assertLess(candidate["opportunity_score"], self.config["initial_alert_score"])

        state = endgame.blank_state()
        state["candidates"][candidate["key"]] = candidate
        alerts, calls = endgame.collect_deadline_alerts(
            self.config, state, NOW, FakePool(FakeDetailClient()), 0
        )
        self.assertEqual(calls, 0)
        self.assertEqual(alerts, [])

    def test_visible_matt_black_colour_phrase_is_not_a_photographer_alert(self):
        task = next(
            task for task in self.tasks
            if task["lane"] == "known" and "Matt Black" in task.get("terms", [])
        )
        summary = auction_summary(title="Kitchen tap in matt black finish", end_minutes=180)
        summary["categories"] = [{"categoryId": "205"}]
        summary["categoryPath"] = "Home Plumbing Taps"
        candidate = endgame.candidate_from_summary(summary, task, self.config, NOW)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["target_match_quality"], "name_only")
        self.assertLess(candidate["opportunity_score"], self.config["initial_alert_score"])

    def test_visible_photobook_target_keeps_full_priority(self):
        task = next(
            task for task in self.tasks
            if task["lane"] == "known" and "Richard Billingham" in task.get("terms", [])
        )
        summary = auction_summary(
            title="Richard Billingham Ikon Gallery 2000 photography book 1st edition",
            end_minutes=180,
        )
        summary["categories"] = [{"categoryId": "261186"}]
        summary["categoryPath"] = "Books"
        candidate = endgame.candidate_from_summary(summary, task, self.config, NOW)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["target_match_quality"], "supported")
        self.assertGreaterEqual(candidate["opportunity_score"], 88)

    def test_visible_celebrity_autobiography_is_demoted_even_in_book_category(self):
        task = next(
            task for task in self.tasks
            if task["lane"] == "known" and "Guy Martin" in task.get("terms", [])
        )
        summary = auction_summary(title="Guy Martin My Autobiography hardcover", end_minutes=180)
        summary["categories"] = [{"categoryId": "261186"}]
        summary["categoryPath"] = "Books"
        candidate = endgame.candidate_from_summary(summary, task, self.config, NOW)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["target_match_quality"], "name_only")
        self.assertLess(candidate["opportunity_score"], self.config["initial_alert_score"])

    def test_high_collision_name_in_generic_book_is_not_enough(self):
        task = next(
            task for task in self.tasks
            if task["lane"] == "known" and "Guy Martin" in task.get("terms", [])
        )
        summary = auction_summary(
            title="Guy Martin When You Dead You Dead Hardback Book",
            end_minutes=180,
        )
        summary["categories"] = [{"categoryId": "261186"}]
        summary["categoryPath"] = "Books"
        candidate = endgame.candidate_from_summary(summary, task, self.config, NOW)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["target_match_quality"], "book_context")
        self.assertLess(candidate["opportunity_score"], self.config["initial_alert_score"])

    def test_generic_title_match_stays_below_threshold_before_and_after_detail(self):
        self.assertFalse(any(task["lane"] == "title" and "Small World" in task.get("terms", [])
                             for task in self.tasks))
        # Old persisted title routes still need the namesake guard.
        task = dict(next(task for task in self.tasks if task["lane"] == "title"))
        task["terms"] = ["Small World"]
        summary = auction_summary(title="Disney Small World Library hardback book", end_minutes=180)
        summary["categories"] = [{"categoryId": "261186"}]
        summary["categoryPath"] = "Books"
        candidate = endgame.candidate_from_summary(summary, task, self.config, NOW)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["target_match_quality"], "book_context")
        self.assertLess(candidate["opportunity_score"], self.config["initial_alert_score"])

        detail = copy.deepcopy(summary)
        detail["estimatedAvailabilityStatus"] = "IN_STOCK"
        enriched, live, _ = endgame.enrich_candidate(candidate, detail, NOW)
        self.assertTrue(live)
        self.assertEqual(enriched["target_match_quality"], "book_context")
        self.assertLess(enriched["opportunity_score"], self.config["initial_alert_score"])

    def test_curated_first_issue_auction_is_distinguished_from_later_reissue(self):
        task = next(task for task in self.tasks if task["lane"] == "title"
                    and "Ray's a Laugh" in task.get("terms", []))
        original = endgame.candidate_from_summary(
            auction_summary(title="Ray's a Laugh Scalo 1996 photobook"), task, self.config, NOW)
        self.assertIsNotNone(original)
        self.assertTrue(original["book_judgment"]["target_book"])
        self.assertGreaterEqual(original["opportunity_score"], self.config["final_alert_score"])
        later_summary = auction_summary(title="Ray's a Laugh MACK 2024 photobook")
        later = endgame.candidate_from_summary(later_summary, task, self.config, NOW)
        self.assertTrue(later["book_judgment"]["known_later_edition"])
        self.assertLess(later["opportunity_score"], self.config["initial_alert_score"])
        later_detail = dict(later_summary, estimatedAvailabilityStatus="IN_STOCK")
        enriched, live, _ = endgame.enrich_candidate(later, later_detail, NOW)
        self.assertTrue(live)
        self.assertLess(enriched["opportunity_score"], self.config["initial_alert_score"])

    def test_persisted_old_title_collision_is_demoted_before_deadline_alert(self):
        task = next(
            task for task in self.tasks
            if task["lane"] == "title" and "The British Isles" in task.get("terms", [])
        )
        summary = auction_summary(title="Birds of The British Isles hardback", end_minutes=180)
        summary["categories"] = [{"categoryId": "261186"}]
        summary["categoryPath"] = "Books"
        candidate = endgame.candidate_from_summary(summary, task, self.config, NOW)
        self.assertIsNotNone(candidate)
        assert candidate is not None

        # Simulate stale pre-fix state that had already been promoted.
        candidate["opportunity_score"] = 88
        candidate["score_band"] = "alert"
        candidate["target_match_quality"] = "book_context"
        state = endgame.blank_state()
        state["candidates"][candidate["key"]] = candidate

        alerts, calls = endgame.collect_deadline_alerts(
            self.config, state, NOW, FakePool(FakeDetailClient()), 0
        )
        self.assertEqual(calls, 0)
        self.assertEqual(alerts, [])
        saved = state["candidates"][candidate["key"]]
        self.assertLess(saved["opportunity_score"], self.config["initial_alert_score"])

    def test_persisted_old_known_namesake_is_demoted_before_deadline_alert(self):
        task = next(
            task for task in self.tasks
            if task["lane"] == "known" and "Matt Black" in task.get("terms", [])
        )
        summary = auction_summary(title="Matt Black kitchen tap", end_minutes=180)
        summary["categories"] = [{"categoryId": "205"}]
        summary["categoryPath"] = "Home Plumbing Taps"
        candidate = endgame.candidate_from_summary(summary, task, self.config, NOW)
        self.assertIsNotNone(candidate)
        assert candidate is not None

        candidate["opportunity_score"] = 82
        candidate["score_band"] = "alert"
        state = endgame.blank_state()
        state["candidates"][candidate["key"]] = candidate
        alerts, _ = endgame.collect_deadline_alerts(
            self.config, state, NOW, FakePool(FakeDetailClient()), 0
        )
        self.assertEqual(alerts, [])
        self.assertLess(
            state["candidates"][candidate["key"]]["opportunity_score"],
            self.config["initial_alert_score"],
        )

    def test_broad_rediscovery_cannot_erase_known_target_lane(self):
        known_task = next(task for task in self.tasks if task["lane"] == "known" and task["tier"] == "1")
        broad_task = next(task for task in self.tasks if task["lane"] == "broad")
        summary = auction_summary(title=f"{known_task['terms'][0]} photography book")
        known = endgame.candidate_from_summary(summary, known_task, self.config, NOW)
        broad = endgame.candidate_from_summary(summary, broad_task, self.config, NOW)
        assert known is not None and broad is not None
        merged = endgame.merge_candidate(known, broad)
        self.assertEqual(merged["discovery_lane"], "known")
        self.assertEqual(merged["query_target_tier"], "1")
        self.assertGreaterEqual(merged["opportunity_score"], 88)

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
        candidate["discovery_lane"] = "broad"
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

    def test_alert_packets_use_early_and_four_hour_prefixes(self):
        initial = auction_summary(title="Early target", end_minutes=12 * 60)
        final = auction_summary(item_id="v1|222222222222|0", title="Four hour target", end_minutes=180)
        alerts = [
            {
                **endgame.candidate_from_summary(initial, self.tasks[0], self.config, NOW),
                "alert_phase": "initial",
                "minutes_remaining": 12 * 60,
            },
            {
                **endgame.candidate_from_summary(final, self.tasks[0], self.config, NOW),
                "alert_phase": "final",
                "minutes_remaining": 180,
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            self.assertEqual(endgame.write_alert_packets(alerts, runtime, self.config, NOW), 2)
            titles = sorted(path.read_text().strip() for path in (runtime / "alerts").glob("*.title"))
            bodies = sorted(path.read_text() for path in (runtime / "alerts").glob("*.md"))
        self.assertTrue(any(title.startswith("ENDGAME_EARLY:") for title in titles))
        self.assertTrue(any(title.startswith("ENDGAME_4H:") for title in titles))
        self.assertTrue(all("@jonattenborough" not in body for body in bodies))

    def test_detail_budget_prioritises_alertable_final_candidate(self):
        task = next(task for task in self.tasks if task["lane"] == "known" and task["tier"] == "1")
        high = endgame.candidate_from_summary(
            auction_summary(title=f"{task['terms'][0]} signed photobook", end_minutes=10),
            task,
            self.config,
            NOW,
        )
        assert high is not None
        low = dict(high)
        low.update(
            {
                "key": "ebay:999999999999",
                "external_id": "999999999999",
                "rest_item_id": "v1|999999999999|0",
                "title": "Unrelated school textbook",
                "opportunity_score": 0,
                "query_target_tier": "",
                "target_query_terms": [],
                "matched_target_terms": [],
                "discovery_lane": "category",
            }
        )
        state = endgame.blank_state()
        state["candidates"] = {high["key"]: high, low["key"]: low}
        detail = auction_summary(title=f"{task['terms'][0]} signed photobook", end_minutes=10)
        detail["estimatedAvailabilityStatus"] = "IN_STOCK"
        client = FakeDetailClient(detail)
        alerts, calls = endgame.collect_deadline_alerts(self.config, state, NOW, FakePool(client), 1)
        self.assertEqual(calls, 1)
        self.assertEqual(client.item_ids, [high["rest_item_id"]])
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["live_verification"], "live auction verified")


if __name__ == "__main__":
    unittest.main()
