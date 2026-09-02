from __future__ import annotations

import unittest

import ebay_private_forensic_verify as verify


def candidate(item_id: int, *, status: str = "unseen_by_scored_monitor", priority: int = 100):
    return {
        "key": f"ebay:{item_id}",
        "external_id": str(item_id),
        "rest_item_id": f"v1|{item_id}|0",
        "title": "Alec Soth Sleeping by the Mississippi first edition book",
        "url": f"https://www.ebay.co.uk/itm/{item_id}",
        "price_gbp": 25.0,
        "landed_price_gbp": 25.0,
        "seller_account_type": "INDIVIDUAL",
        "prior_status": status,
        "audit_priority": priority,
        "review": True,
        "obvious_nonbook": False,
        "best_target_match": {
            "target": {
                "record_id": "alec-soth-sleeping",
                "contributor": "Alec Soth",
                "title": "Sleeping by the Mississippi",
                "collectibility_tier": "S",
                "canon_sources": "Parr/Badger V2",
            }
        },
    }


class FakeClient:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = []

    def live_status(self, item_id):
        self.calls.append(item_id)
        return self.statuses.pop(0)


class ForensicVerifyTests(unittest.TestCase):
    def test_queue_excludes_already_surfaced_items(self):
        payload = {"items": [candidate(1, status="surfaced"), candidate(2), candidate(3, priority=90)]}
        self.assertEqual(verify.candidate_queue(payload, limit=10), ["ebay:2", "ebay:3"])

    def test_live_verification_preserves_independent_evaluation(self):
        candidates = {"items": [candidate(4)]}
        state = {"queue": ["ebay:4"], "complete": False}
        findings = {"version": 1, "items": {}}
        detail = {
            "seller": {"username": "private-seller", "sellerAccountType": "INDIVIDUAL"},
            "buyingOptions": ["FIXED_PRICE"],
            "price": {"value": "25.00", "currency": "GBP"},
            "description": "Old book from a house clearance",
        }
        result = verify.run_verification(
            FakeClient([(True, "live", detail)]),
            state,
            candidates,
            findings,
            call_budget=1,
            detected_at="2026-09-03T08:30:00Z",
        )
        self.assertTrue(result["complete"])
        self.assertTrue(result["completed_now"])
        stored = findings["items"]["ebay:4"]
        self.assertTrue(stored["live_verified"])
        self.assertTrue(stored["review"])
        self.assertNotIn("opportunity_score", stored)

    def test_unavailable_listing_is_retained_with_reason(self):
        candidates = {"items": [candidate(5)]}
        state = {"queue": ["ebay:5"], "complete": False}
        findings = {"version": 1, "items": {}}
        result = verify.run_verification(
            FakeClient([(False, "listing ended", {})]),
            state,
            candidates,
            findings,
            call_budget=1,
            detected_at="2026-09-03T08:30:00Z",
        )
        self.assertEqual(result["unavailable"], 1)
        self.assertFalse(findings["items"]["ebay:5"]["live_verified"])
        self.assertEqual(findings["items"]["ebay:5"]["availability_reason"], "listing ended")


if __name__ == "__main__":
    unittest.main()
