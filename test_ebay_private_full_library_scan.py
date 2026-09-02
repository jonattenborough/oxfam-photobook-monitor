import tempfile
import unittest
from pathlib import Path

import ebay_private_full_library_scan as scan
import ebay_private_seller_backfill as backfill
import photobook_recognition as recognition


class FullLibraryScanTests(unittest.TestCase):
    def test_plan_searches_every_library_record_once(self):
        plan = scan.build_plan()
        stats = recognition.library_stats()
        self.assertEqual(len(plan), stats["records"])
        self.assertEqual(len({step["query"].lower() for step in plan}), stats["records"])
        self.assertTrue(all(step["price_max"] == 300 for step in plan))
        self.assertTrue(all(step["category_ids"] is None for step in plan))
        self.assertTrue(all(step["search_in_description"] for step in plan))
        self.assertTrue(all(step["max_offset"] == 0 for step in plan))
        self.assertTrue(
            all(
                step["buying_options"] == ["FIXED_PRICE", "BEST_OFFER", "AUCTION"]
                for step in plan
            )
        )

    def test_price_aware_threshold_prioritises_hidden_gems(self):
        base = {
            "price_review_profile": "jon_hidden_gem",
            "market_issue_threshold": 60,
            "best_recognition": {"collectibility_tier": "S"},
        }
        self.assertEqual(backfill.issue_score_threshold({**base, "landed_price_gbp": 99}, 60), 60)
        self.assertEqual(backfill.issue_score_threshold({**base, "landed_price_gbp": 150}, 60), 72)
        self.assertEqual(backfill.issue_score_threshold({**base, "landed_price_gbp": 250}, 60), 84)
        self.assertEqual(backfill.issue_score_threshold({**base, "landed_price_gbp": 301}, 60), 101)
        self.assertEqual(backfill.live_score_threshold({**base, "landed_price_gbp": 99}, 60), 55)

    def test_hidden_gem_gate_tightens_for_routine_lower_tiers(self):
        base = {"price_review_profile": "jon_hidden_gem", "market_issue_threshold": 60}
        tier_b = {**base, "best_recognition": {"collectibility_tier": "B"}}
        tier_c = {**base, "best_recognition": {"collectibility_tier": "C"}}
        self.assertEqual(backfill.issue_score_threshold({**tier_b, "landed_price_gbp": 45}, 60), 68)
        self.assertEqual(backfill.live_score_threshold({**tier_b, "landed_price_gbp": 45}, 60), 67)
        self.assertEqual(backfill.issue_score_threshold({**tier_c, "landed_price_gbp": 45}, 60), 72)
        self.assertEqual(backfill.live_score_threshold({**tier_c, "landed_price_gbp": 45}, 60), 72)
        self.assertEqual(backfill.issue_score_threshold({**tier_b, "landed_price_gbp": 150}, 60), 80)
        self.assertEqual(backfill.issue_score_threshold({**tier_c, "landed_price_gbp": 250}, 60), 94)

    def test_special_and_documentary_signals_keep_low_price_discovery_open(self):
        base = {"price_review_profile": "jon_hidden_gem", "market_issue_threshold": 60}
        special = {
            **base,
            "landed_price_gbp": 80,
            "best_recognition": {
                "collectibility_tier": "B",
                "collectible_format_evidence": ["signed by the photographer"],
            },
        }
        documentary = {
            **base,
            "landed_price_gbp": 80,
            "best_recognition": {
                "collectibility_tier": "B",
                "documentary_relevance": "HIGH",
            },
        }
        self.assertEqual(backfill.issue_score_threshold(special, 60), 64)
        self.assertEqual(backfill.live_score_threshold(special, 60), 61)
        self.assertEqual(backfill.issue_score_threshold(documentary, 60), 64)
        self.assertEqual(backfill.live_score_threshold(documentary, 60), 61)

    def test_existing_backfills_keep_their_original_threshold(self):
        item = {"market_issue_threshold": 72, "landed_price_gbp": 20}
        self.assertEqual(backfill.issue_score_threshold(item, 72), 72)
        self.assertEqual(backfill.live_score_threshold(item, 72), 60)

    def test_issue_payloads_are_bounded_to_ten_candidates(self):
        candidates = []
        for index in range(23):
            candidates.append(
                {
                    "key": f"ebay:{index}",
                    "title": f"Book {index}",
                    "url": f"https://www.ebay.co.uk/itm/{index}",
                    "price_gbp": float(index + 1),
                    "price_value": float(index + 1),
                    "price_currency": "GBP",
                    "landed_price_gbp": float(index + 1),
                    "opportunity_score": 70,
                    "private_seller": True,
                    "seller_account_type": "INDIVIDUAL",
                    "buying_options": ["FIXED_PRICE"],
                    "live_verified": True,
                    "live_verified_at": "2026-09-02T08:00:00Z",
                }
            )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            count = scan.write_issue_payloads(
                runtime,
                candidates,
                detected_at="2026-09-02T08:00:00Z",
                stats={"records": 4318},
                result={},
            )
            self.assertEqual(count, 3)
            bodies = sorted((runtime / "issues").glob("*.md"))
            self.assertEqual(len(bodies), 3)
            self.assertEqual(bodies[0].read_text(encoding="utf-8").count("### REVIEW"), 10)
            self.assertEqual(bodies[2].read_text(encoding="utf-8").count("### REVIEW"), 3)


if __name__ == "__main__":
    unittest.main()
