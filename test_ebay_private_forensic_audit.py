from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import ebay_private_forensic_audit as audit
import ebay_private_forensic_scan as scan


def target(**overrides):
    value = {
        "record_id": "alec-soth-sleeping",
        "contributor": "Alec Soth",
        "title": "Sleeping by the Mississippi",
        "collectibility_tier": "S",
        "canon_sources": "Parr/Badger V2",
    }
    value.update(overrides)
    return value


def item(item_id: int, title: str, price: float = 20.0, **overrides):
    value = {
        "key": f"ebay:{item_id}",
        "external_id": str(item_id),
        "rest_item_id": f"v1|{item_id}|0",
        "title": title,
        "url": f"https://www.ebay.co.uk/itm/{item_id}",
        "price_gbp": price,
        "landed_price_gbp": price,
        "seller_account_type": "INDIVIDUAL",
        "category_id": "261186",
    }
    value.update(overrides)
    return value


class ForensicAuditTests(unittest.TestCase):
    def test_exact_cheap_canonical_book_is_reviewed_without_bot_score(self):
        evaluation = audit.evaluate(
            item(1, "Alec Soth Sleeping by the Mississippi photography book", 25, opportunity_score=0),
            target(),
        )
        self.assertTrue(evaluation["review"])
        self.assertFalse(evaluation["obvious_nonbook"])
        self.assertEqual(evaluation["match"]["strength"], "exact")

    def test_clear_clipping_is_flagged_as_nonbook(self):
        evaluation = audit.evaluate(
            item(2, "CLIPPINGS Alec Soth magazine 8 pages", 8, category_id=""),
            target(title="Songbook"),
        )
        self.assertTrue(evaluation["obvious_nonbook"])
        self.assertFalse(evaluation["review"])

    def test_real_book_whose_title_contains_postcards_is_retained(self):
        pollard = target(
            record_id="ingrid-pollard-postcards-home",
            contributor="Ingrid Pollard",
            title="Postcards Home",
            collectibility_tier="B",
        )
        evaluation = audit.evaluate(
            item(3, "Postcards Home: Ingrid Pollard, Chris Boot 2004", 40),
            pollard,
        )
        self.assertFalse(evaluation["obvious_nonbook"])
        self.assertTrue(evaluation["review"])

    def test_explicit_reissue_is_penalised_but_preserved(self):
        original = audit.evaluate(
            item(4, "Larry Sultan Mike Mandel Evidence first edition", 50),
            target(contributor="Larry Sultan and Mike Mandel", title="Evidence"),
        )
        reissue = audit.evaluate(
            item(5, "Larry Sultan Mike Mandel Evidence 2017 reissue edition", 50),
            target(contributor="Larry Sultan and Mike Mandel", title="Evidence"),
        )
        self.assertTrue(reissue["explicit_reissue_wording"])
        self.assertLess(reissue["audit_priority"], original["audit_priority"])

    def test_chunks_are_deduplicated_but_keep_all_target_matches_and_prior_status(self):
        shared = item(6, "Alec Soth Sleeping by the Mississippi book", 30)
        chunk = {
            "version": 1,
            "query_count": 2,
            "result_count": 2,
            "truncated_queries": [],
            "searches": [
                {"query": "Alec Soth Sleeping", "target": target(), "result_count": 1, "items": [shared]},
                {"query": "Alec Soth Songbook", "target": target(record_id="alec-soth-songbook", title="Songbook"), "result_count": 1, "items": [shared]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "batch-001.json.gz"
            scan.write_gzip_json(path, chunk)
            summary, candidates = audit.audit_chunks(
                [path],
                existing_state={"reviewed": {"ebay:6": {}}},
                existing_findings={"items": {}},
            )
        self.assertEqual(summary["raw_hits"], 2)
        self.assertEqual(summary["unique_items"], 1)
        self.assertEqual(candidates[0]["matched_query_count"], 2)
        self.assertEqual(candidates[0]["prior_status"], "reviewed_not_surfaced")


if __name__ == "__main__":
    unittest.main()
