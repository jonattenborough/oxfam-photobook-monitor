from __future__ import annotations

import unittest
from pathlib import Path

import ebay_seller_monitor as monitor


def listing(item_id: str, title: str) -> dict:
    return {
        "key": f"ebay:{item_id}",
        "external_id": item_id,
        "title": title,
        "context": "Used fixed-price book",
        "url": f"https://www.ebay.co.uk/itm/{item_id}",
        "seller_id": "example",
        "marketplace": "EBAY_GB",
        "source_page": "https://www.ebay.co.uk/usr/example",
        "price_value": 10.0,
        "price_currency": "GBP",
    }


class FakeClient:
    def __init__(self, pages: list[list[dict]]):
        self.pages = pages
        self.calls: list[dict] = []

    def search(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        return self.pages[len(self.calls) - 1]


class EbaySellerMonitorTests(unittest.TestCase):
    def test_supplied_config_has_61_uk_and_14_us_unique_sellers(self):
        sellers = monitor.load_config(Path("data/ebay_sellers.json"))
        uk = [seller for seller in sellers if seller["marketplace"] == "EBAY_GB"]
        us = [seller for seller in sellers if seller["marketplace"] == "EBAY_US"]
        self.assertEqual(len(uk), 61)
        self.assertEqual(len(us), 14)
        self.assertEqual(len({monitor.seller_key(s["marketplace"], s["id"]) for s in sellers}), 75)
        self.assertTrue(all(seller.get("delivery_country") == "GB" for seller in us))

    def test_first_success_silently_baselines_current_items(self):
        state, candidates, baseline = monitor.update_seller_state(
            None,
            [listing("100", "Robert Frank The Americans photography book")],
            "2026-08-28T12:00:00Z",
        )
        self.assertTrue(baseline)
        self.assertEqual(candidates, [])
        self.assertIn("ebay:100", state["seen"])

    def test_only_unseen_plausible_items_become_candidates(self):
        previous, _, _ = monitor.update_seller_state(
            None,
            [listing("100", "Ordinary dictionary")],
            "2026-08-28T12:00:00Z",
        )
        updated, candidates, baseline = monitor.update_seller_state(
            previous,
            [
                listing("100", "Ordinary dictionary"),
                listing("101", "A new photography monograph"),
                listing("102", "Another ordinary dictionary"),
            ],
            "2026-08-28T12:30:00Z",
        )
        self.assertFalse(baseline)
        self.assertEqual([item["external_id"] for item in candidates], ["101"])
        self.assertIn("ebay:102", updated["seen"])

    def test_us_scan_uses_books_seller_delivery_and_incremental_filters(self):
        client = FakeClient([[]])
        seller = {"id": "goodwillbks", "marketplace": "EBAY_US", "delivery_country": "GB"}
        previous = {"initialized": True, "last_successful_fetch": "2026-08-28T12:30:00Z"}
        items = monitor.scan_seller(client, seller, previous)
        self.assertEqual(items, [])
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertIsNone(call["query"])
        self.assertEqual(call["category_ids"], "261186")
        self.assertEqual(call["seller_ids"], ["goodwillbks"])
        self.assertEqual(call["delivery_country"], "GB")
        self.assertEqual(call["item_start_date"], "2026-08-28T12:20:00Z")

    def test_baseline_never_paginates_beyond_newest_200(self):
        rows = [
            {"itemId": f"v1|{100000000000 + index}|0", "title": f"Book {index}"}
            for index in range(200)
        ]
        client = FakeClient([rows])
        seller = {"id": "example", "marketplace": "EBAY_GB"}
        items = monitor.scan_seller(client, seller, None)
        self.assertEqual(len(items), 200)
        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
