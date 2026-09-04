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


if __name__ == "__main__":
    unittest.main()
