from __future__ import annotations

import unittest

import ebay_private_alert_builder as alerts


class RecallFirstAlertBuilderTests(unittest.TestCase):
    def test_search_only_candidate_is_alerted_without_live_verification(self):
        snapshot = {
            "checked_at": "2026-09-04T08:00:00Z",
            "new_candidates": [],
        }
        state = {
            "seen": {},
            "pending_live": {
                "ebay:1": {
                    "key": "ebay:1",
                    "title": "Cheap collectible photobook",
                    "opportunity_score": 80,
                    "private_seller": True,
                    "seller_account_type": "INDIVIDUAL",
                    "pending_since": "2026-09-04T07:59:00Z",
                    "url": "https://www.ebay.co.uk/itm/1",
                }
            },
        }
        candidates, search_only = alerts.collect_candidates(snapshot, state, 72)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(search_only, {"ebay:1"})
        self.assertEqual(candidates[0]["alert_verification"], "SEARCH RESULT ONLY")

    def test_search_only_candidate_moves_from_pending_to_seen_after_alert(self):
        candidate = {
            "key": "ebay:2",
            "title": "Another bargain",
            "opportunity_score": 78,
            "private_seller": True,
            "seller_account_type": "INDIVIDUAL",
            "alert_verification": "SEARCH RESULT ONLY",
            "price_gbp": 42.0,
            "buying_options": ["FIXED_PRICE", "BEST_OFFER"],
        }
        state = {"seen": {}, "pending_live": {"ebay:2": dict(candidate)}}
        alerts.mark_search_only_alerted(
            state,
            [candidate],
            {"ebay:2"},
            "2026-09-04T08:00:00Z",
        )
        self.assertNotIn("ebay:2", state["pending_live"])
        self.assertIn("ebay:2", state["seen"])
        self.assertEqual(state["seen"]["ebay:2"]["observed_price_gbp"], 42.0)
        self.assertIn("BEST_OFFER", state["seen"]["ebay:2"]["buying_options"])

    def test_live_verified_and_search_only_candidates_are_both_kept(self):
        snapshot = {
            "checked_at": "2026-09-04T08:00:00Z",
            "new_candidates": [
                {
                    "key": "ebay:live",
                    "title": "Live checked",
                    "opportunity_score": 85,
                    "private_seller": True,
                    "seller_account_type": "INDIVIDUAL",
                    "live_verified": True,
                }
            ],
        }
        state = {
            "pending_live": {
                "ebay:search": {
                    "key": "ebay:search",
                    "title": "Search only",
                    "opportunity_score": 82,
                    "private_seller": True,
                    "seller_account_type": "INDIVIDUAL",
                }
            }
        }
        candidates, search_only = alerts.collect_candidates(snapshot, state, 72)
        self.assertEqual({item["key"] for item in candidates}, {"ebay:live", "ebay:search"})
        self.assertEqual(search_only, {"ebay:search"})

    def test_business_seller_is_not_promoted_from_pending(self):
        snapshot = {"new_candidates": []}
        state = {
            "pending_live": {
                "ebay:business": {
                    "key": "ebay:business",
                    "opportunity_score": 95,
                    "private_seller": True,
                    "seller_account_type": "BUSINESS",
                }
            }
        }
        candidates, search_only = alerts.collect_candidates(snapshot, state, 72)
        self.assertEqual(candidates, [])
        self.assertEqual(search_only, set())

    def test_issue_body_clearly_labels_search_only_status(self):
        body = alerts.make_issue_body(
            [
                {
                    "key": "ebay:3",
                    "title": "Possible gem",
                    "opportunity_score": 80,
                    "private_seller": True,
                    "seller_account_type": "INDIVIDUAL",
                    "alert_verification": "SEARCH RESULT ONLY",
                    "search_observed_at": "2026-09-04T07:59:00Z",
                    "url": "https://www.ebay.co.uk/itm/3",
                }
            ],
            detected_at="2026-09-04T08:00:00Z",
            stats={"records": 4318},
            failures=[],
            urgent_threshold=90,
        )
        self.assertIn("SEARCH RESULT ONLY", body)
        self.assertIn("availability was not rechecked", body)
        self.assertNotIn("Every surfaced listing has been re-fetched", body)

    def test_issue_body_explains_material_change_and_unknown_lane(self):
        body = alerts.make_issue_body(
            [
                {
                    "key": "ebay:4",
                    "title": "Unknown signed photobook",
                    "opportunity_score": 80,
                    "private_seller": True,
                    "seller_account_type": "INDIVIDUAL",
                    "alert_verification": "SEARCH RESULT ONLY",
                    "material_change": True,
                    "material_change_reasons": ["price dropped from £120.00 to £40.00"],
                    "recall_first_unknown": True,
                    "url": "https://www.ebay.co.uk/itm/4",
                }
            ],
            detected_at="2026-09-04T08:00:00Z",
            stats={"records": 4318},
            failures=[],
            urgent_threshold=90,
        )
        self.assertIn("Material change", body)
        self.assertIn("£120.00 to £40.00", body)
        self.assertIn("Unknown-book lane", body)

    def test_urgent_candidate_always_leads_fast_triage(self):
        urgent = {
            "title": "Urgent bargain",
            "opportunity_score": 95,
            "landed_price_gbp": 120.0,
            "best_recognition": {"collectibility_tier": "A"},
        }
        changed = {
            "title": "Changed listing",
            "opportunity_score": 72,
            "landed_price_gbp": 40.0,
            "material_change": True,
        }
        ordered = sorted([changed, urgent], key=alerts.priority_key)
        self.assertIs(ordered[0], urgent)

    def test_sub_100_special_candidate_leads_expensive_higher_score(self):
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
        self.assertIn("special", title)


if __name__ == "__main__":
    unittest.main()
