from __future__ import annotations

import unittest

import ebay_seller_backfill as backfill


class FakeClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def search(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        return self.pages.pop(0)


def raw_item(item_id: int, title: str) -> dict:
    return {
        "itemId": f"v1|{item_id}|0",
        "title": title,
        "itemWebUrl": f"https://www.ebay.co.uk/itm/{item_id}",
        "price": {"value": "12.00", "currency": "GBP"},
        "seller": {"username": "seller"},
    }


class EbaySellerBackfillTests(unittest.TestCase):
    def test_scan_page_uses_current_books_inventory_filters(self):
        client = FakeClient([[]])
        seller = {"id": "goodwillbks", "marketplace": "EBAY_US", "delivery_country": "GB"}
        items, raw_count, oldest_creation = backfill.scan_page(client, seller, 400)
        self.assertEqual(items, [])
        self.assertEqual(raw_count, 0)
        self.assertIsNone(oldest_creation)
        call = client.calls[0]
        self.assertIsNone(call["query"])
        self.assertEqual(call["offset"], 400)
        self.assertEqual(call["category_ids"], "261186")
        self.assertEqual(call["seller_ids"], ["goodwillbks"])
        self.assertEqual(call["delivery_country"], "GB")

    def test_older_segment_uses_a_closed_creation_date_range(self):
        client = FakeClient([[]])
        seller = {"id": "mtwyouth", "marketplace": "EBAY_US", "delivery_country": "GB"}
        backfill.scan_page(client, seller, 0, "2020-01-01T00:00:00Z")
        call = client.calls[0]
        self.assertEqual(call["item_start_date"], backfill.EBAY_EPOCH)
        self.assertEqual(call["item_end_date"], "2020-01-01T00:00:00Z")

    def test_timestamp_boundary_moves_before_oldest_item(self):
        self.assertEqual(
            backfill.before_timestamp("2020-01-01T00:00:00.000Z"),
            "2019-12-31T23:59:59.999Z",
        )

    def test_round_robin_scan_completes_short_sellers_and_records_candidates(self):
        sellers = [
            {"id": "one", "marketplace": "EBAY_GB"},
            {"id": "two", "marketplace": "EBAY_US", "delivery_country": "GB"},
        ]
        clients = {
            "EBAY_GB": FakeClient([[raw_item(100000000001, "A new photography monograph")]]),
            "EBAY_US": FakeClient([[]]),
        }
        state = {"version": 1, "sellers": {}}
        findings = {"version": 1, "items": {}}
        result = backfill.run_backfill(
            sellers,
            state,
            findings,
            clients,
            call_budget=2,
            detected_at="2026-08-28T15:30:00Z",
        )
        self.assertEqual(result["calls"], 2)
        self.assertEqual(result["books_scanned"], 1)
        self.assertEqual(result["completed_sellers"], 2)
        self.assertEqual(len(result["new_candidates"]), 1)
        self.assertIn("ebay:100000000001", findings["items"])

    def test_canon_shortlist_rejects_unrelated_title(self):
        item = {"title": "A basic French dictionary", "context": "Used book", "vendor": "seller"}
        self.assertFalse(backfill.might_match_canon(item))

    def test_existing_10000_cap_continues_in_an_older_date_segment(self):
        rows = [
            {
                **raw_item(200000000000 + index, f"Ordinary book {index}"),
                "itemCreationDate": "2020-01-01T00:00:00.000Z",
            }
            for index in range(200)
        ]
        seller = {"id": "mtwyouth", "marketplace": "EBAY_US", "delivery_country": "GB"}
        client = FakeClient([rows, []])
        state = {
            "version": 1,
            "sellers": {
                "EBAY_US:mtwyouth": {
                    "seller_id": "mtwyouth",
                    "marketplace": "EBAY_US",
                    "next_offset": 9800,
                    "pages_scanned": 50,
                    "books_scanned": 10000,
                    "complete": True,
                    "capped_at_10000": True,
                }
            },
        }
        result = backfill.run_backfill(
            [seller],
            state,
            {"version": 1, "items": {}},
            {"EBAY_US": client},
            call_budget=2,
            detected_at="2026-08-28T15:30:00Z",
        )
        entry = state["sellers"]["EBAY_US:mtwyouth"]
        self.assertEqual(result["calls"], 2)
        self.assertTrue(entry["complete"])
        self.assertTrue(entry["date_partitions_complete"])
        self.assertEqual(client.calls[0]["offset"], 9800)
        self.assertEqual(client.calls[1]["offset"], 0)
        self.assertEqual(client.calls[1]["item_end_date"], "2019-12-31T23:59:59.999Z")


if __name__ == "__main__":
    unittest.main()
