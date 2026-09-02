from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

import ebay_private_forensic_scan as forensic
import ebay_private_seller_monitor as live_monitor


def raw_item(item_id: int, title: str, *, price: float = 20.0) -> dict:
    return {
        "itemId": f"v1|{item_id}|0",
        "title": title,
        "itemWebUrl": f"https://www.ebay.co.uk/itm/{item_id}",
        "price": {"value": f"{price:.2f}", "currency": "GBP"},
        "seller": {"username": "private-seller", "sellerAccountType": "INDIVIDUAL"},
        "buyingOptions": ["FIXED_PRICE"],
        "itemCreationDate": "2026-09-01T10:00:00.000Z",
    }


class FakeClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.search_calls = []

    def search(self, query, **kwargs):
        self.search_calls.append({"query": query, **kwargs})
        return self.pages.pop(0)


def step(query: str, record_id: str) -> dict:
    return {
        "query": query,
        "target": {
            "record_id": record_id,
            "contributor": "Alec Soth",
            "title": "Sleeping by the Mississippi",
            "collectibility_tier": "S",
        },
        "marketplace": "EBAY_GB",
        "market_country": "GB",
        "seller_filter_mode": "individual",
        "delivery_country": "GB",
        "price_currency": "GBP",
        "price_max": 300.0,
        "lane": "forensic_full_library_exact",
        "category_ids": None,
        "search_in_description": True,
        "buying_options": ["FIXED_PRICE", "BEST_OFFER", "AUCTION"],
        "offset": 0,
        "max_offset": 0,
        "sort": "price",
    }


class ForensicScanTests(unittest.TestCase):
    def setUp(self):
        self.config = live_monitor.load_config(Path("data/ebay_private_searches.json"))

    def test_plan_covers_every_record_without_a_score_gate(self):
        plan = forensic.build_plan()
        self.assertEqual(len(plan), 4318)
        self.assertEqual(len({item["query"].lower() for item in plan}), 4318)
        self.assertTrue(all(item["category_ids"] is None for item in plan))
        self.assertTrue(all(item["search_in_description"] for item in plan))
        self.assertTrue(all(item["price_max"] == 300.0 for item in plan))
        self.assertTrue(all(item["sort"] == "price" for item in plan))
        self.assertTrue(all("target" in item for item in plan))

    def test_capture_preserves_every_summary_without_classifying(self):
        pages = [[
            raw_item(111, "Alec Soth Sleeping by the Mississippi old book", price=25),
            raw_item(222, "Alec Soth postcard", price=3),
        ]]
        state = {
            "version": 1,
            "queue": [step("Alec Soth Sleeping by the Mississippi", "alec-soth-sleeping")],
            "chunks": [],
            "complete": False,
        }
        result, chunk = forensic.run_capture(
            FakeClient(pages),
            self.config,
            state,
            call_budget=1,
            detected_at="2026-09-02T12:00:00Z",
        )
        self.assertTrue(result["complete"])
        self.assertEqual(result["results_captured"], 2)
        self.assertIsNotNone(chunk)
        items = chunk["searches"][0]["items"]
        self.assertEqual({item["key"] for item in items}, {"ebay:111", "ebay:222"})
        self.assertTrue(all("opportunity_score" not in item for item in items))

    def test_duplicate_listing_is_preserved_with_each_query_context(self):
        shared = raw_item(333, "Alec Soth photography book", price=30)
        state = {
            "version": 1,
            "queue": [step("Alec Soth Niagara", "alec-soth-niagara"), step("Alec Soth Songbook", "alec-soth-songbook")],
            "chunks": [],
            "complete": False,
        }
        result, chunk = forensic.run_capture(
            FakeClient([[shared], [shared]]),
            self.config,
            state,
            call_budget=2,
            detected_at="2026-09-02T12:00:00Z",
        )
        self.assertEqual(result["results_captured"], 2)
        self.assertEqual(len(chunk["searches"]), 2)
        self.assertEqual(chunk["searches"][0]["items"][0]["key"], "ebay:333")
        self.assertEqual(chunk["searches"][1]["items"][0]["key"], "ebay:333")

    def test_failed_query_returns_to_queue(self):
        class BrokenClient:
            def search(self, query, **kwargs):
                raise RuntimeError("temporary failure")

        state = {
            "version": 1,
            "queue": [step("Alec Soth Niagara", "alec-soth-niagara")],
            "chunks": [],
            "complete": False,
        }
        with self.assertRaisesRegex(RuntimeError, "no progress"):
            forensic.run_capture(
                BrokenClient(),
                self.config,
                state,
                call_budget=1,
                detected_at="2026-09-02T12:00:00Z",
            )
        self.assertEqual(len(state["queue"]), 1)

    def test_gzip_output_is_reproducible_and_readable(self):
        payload = {"version": 1, "searches": [{"query": "test"}]}
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.json.gz"
            second = Path(tmp) / "second.json.gz"
            forensic.write_gzip_json(first, payload)
            forensic.write_gzip_json(second, payload)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with gzip.open(first, "rt", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), payload)


if __name__ == "__main__":
    unittest.main()
