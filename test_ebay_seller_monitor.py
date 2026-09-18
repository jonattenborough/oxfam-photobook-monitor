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


def summary(item_id: int, title: str = "Photography book") -> dict:
    return {
        "itemId": f"v1|{item_id}|0",
        "title": title,
        "itemWebUrl": f"https://www.ebay.co.uk/itm/{item_id}",
        "price": {"value": "10.00", "currency": "GBP"},
        "buyingOptions": ["FIXED_PRICE"],
        "categories": [{"categoryId": monitor.BOOKS_CATEGORY_ID}],
        "categoryPath": "Books",
        "seller": {"username": "example"},
    }


def next_url(offset: int) -> str:
    return (
        "https://api.ebay.com/buy/browse/v1/item_summary/search"
        f"?q=books&limit=200&offset={offset}"
    )


def envelope(
    rows: list[dict],
    *,
    offset: int = 0,
    total: int | None = None,
    next_offset: int | None = None,
) -> dict:
    result = {
        "itemSummaries": rows,
        "offset": offset,
        "total": len(rows) if total is None else total,
    }
    if next_offset is not None:
        result["next"] = next_url(next_offset)
    return result


class FakeClient:
    def __init__(
        self,
        *,
        baseline_pages: list[list[dict]] | None = None,
        search_pages: list[dict] | None = None,
        next_pages: list[dict | Exception] | None = None,
    ):
        self.baseline_pages = list(baseline_pages or [])
        self.search_pages = list(search_pages or [])
        self.next_pages = list(next_pages or [])
        self.calls: list[dict] = []

    def search(self, query, **kwargs):
        self.calls.append({"method": "search", "query": query, **kwargs})
        return self.baseline_pages.pop(0) if self.baseline_pages else []

    def search_page(self, query, **kwargs):
        self.calls.append({"method": "search_page", "query": query, **kwargs})
        return self.search_pages.pop(0) if self.search_pages else envelope([])

    def search_next(self, url):
        self.calls.append({"method": "search_next", "url": url})
        if not self.next_pages:
            return envelope([])
        value = self.next_pages.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class EbaySellerMonitorTests(unittest.TestCase):
    def test_supplied_config_has_89_uk_and_14_us_unique_sellers(self):
        sellers = monitor.load_config(Path("data/ebay_sellers.json"))
        uk = [seller for seller in sellers if seller["marketplace"] == "EBAY_GB"]
        us = [seller for seller in sellers if seller["marketplace"] == "EBAY_US"]
        self.assertEqual(len(uk), 89)
        self.assertEqual(len(us), 14)
        self.assertEqual(len({monitor.seller_key(s["marketplace"], s["id"]) for s in sellers}), 103)
        self.assertTrue(all(seller.get("delivery_country") == "GB" for seller in us))

    def test_hourly_batches_cover_every_seller_in_nine_runs(self):
        sellers = monitor.load_config(Path("data/ebay_sellers.json"))
        cursor = 0
        batches = []
        for _ in range(9):
            batch, cursor = monitor.select_sellers(sellers, cursor, monitor.DEFAULT_SELLERS_PER_RUN)
            batches.append(batch)
        covered = {
            monitor.seller_key(seller["marketplace"], seller["id"])
            for batch in batches for seller in batch
        }
        self.assertEqual([len(batch) for batch in batches], [12] * 9)
        self.assertEqual(len(covered), 103)
        self.assertEqual(cursor, 5)

    def test_pending_sellers_get_bounded_priority_without_starving_rotation(self):
        sellers = [
            {"id": f"s{index:02d}", "marketplace": "EBAY_GB"}
            for index in range(12)
        ]
        state = {"sellers": {}}
        for index in (8, 9, 10, 11):
            key = monitor.seller_key("EBAY_GB", f"s{index:02d}")
            state["sellers"][key] = {
                "initialized": True,
                "pending_window": {
                    "end": "2026-09-18T06:00:00Z",
                    "pending": [{"start": "2026-09-17T00:00:00Z", "end": "2026-09-18T06:00:00Z", "offset": 200}],
                    "last_attempt_at": f"2026-09-18T0{index - 8}:00:00Z",
                },
            }
        batch, next_cursor = monitor.select_sellers_with_pending(sellers, state, 0, 6)
        ids = [row["id"] for row in batch]
        self.assertEqual(ids[:2], ["s08", "s09"])
        self.assertEqual(ids[2:], ["s00", "s01", "s02", "s03"])
        self.assertEqual(next_cursor, 4)

    def test_nominal_daily_demand_is_inside_reduced_budget(self):
        nominal_daily_calls = monitor.DEFAULT_SELLERS_PER_RUN * 24
        self.assertGreaterEqual(nominal_daily_calls, 250)
        self.assertLessEqual(nominal_daily_calls, 350)

    def test_scheduled_seller_allocation_matches_default(self):
        workflow = Path(".github/workflows/ebay-seller-monitor.yml").read_text(encoding="utf-8")
        self.assertIn(f"--sellers-per-run {monitor.DEFAULT_SELLERS_PER_RUN}", workflow)

    def test_quota_batch_allows_for_incremental_page_spillover(self):
        self.assertEqual(monitor.quota_safe_seller_count(260, 52), 52)
        self.assertEqual(monitor.quota_safe_seller_count(99, 52), 19)
        self.assertEqual(monitor.quota_safe_seller_count(4, 52), 0)

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

    def test_core_photographer_bypasses_generic_filter_and_keeps_tier(self):
        previous, _, _ = monitor.update_seller_state(
            None,
            [listing("200", "Ordinary dictionary")],
            "2026-08-28T12:00:00Z",
        )
        target = listing("201", "Sian Davey Looking for Alice hardback")
        target["context"] = "Used book"
        target["category_id"] = monitor.BOOKS_CATEGORY_ID
        target["category_path"] = "Books"
        _, candidates, baseline = monitor.update_seller_state(
            previous,
            [target],
            "2026-08-28T13:00:00Z",
        )
        self.assertFalse(baseline)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["core_target_tier"], "1")
        self.assertIn("Siân Davey", candidates[0]["matched_core_photographers"])

    def test_us_scan_uses_frozen_books_seller_delivery_and_incremental_filters(self):
        client = FakeClient(search_pages=[envelope([])])
        seller = {"id": "goodwillbks", "marketplace": "EBAY_US", "delivery_country": "GB"}
        previous = {"initialized": True, "last_successful_fetch": "2026-08-28T12:30:00Z"}
        items, checkpoint, complete, window_end = monitor.scan_seller(
            client,
            seller,
            previous,
            detected_at="2026-08-28T13:00:00Z",
        )
        self.assertEqual(items, [])
        self.assertTrue(complete)
        self.assertEqual(window_end, "2026-08-28T13:00:00Z")
        self.assertIsNotNone(checkpoint)
        self.assertTrue(checkpoint["completed"])
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual(call["method"], "search_page")
        self.assertIsNone(call["query"])
        self.assertEqual(call["category_ids"], "261186")
        self.assertEqual(call["seller_ids"], ["goodwillbks"])
        self.assertEqual(call["delivery_country"], "GB")
        self.assertEqual(call["item_start_date"], "2026-08-28T12:20:00Z")
        self.assertEqual(call["item_end_date"], "2026-08-28T13:00:00Z")

    def test_completed_scan_advances_watermark_to_frozen_window_end(self):
        previous = {
            "initialized": True,
            "last_successful_fetch": "2026-09-17T12:00:00Z",
            "seen": {},
        }
        client = FakeClient(search_pages=[envelope([summary(300)])])
        items, checkpoint, complete, window_end = monitor.scan_seller(
            client, {"id": "example", "marketplace": "EBAY_GB"}, previous,
            detected_at="2026-09-18T06:00:00Z",
        )
        updated, candidates, _ = monitor.update_seller_state(
            previous,
            items,
            "2026-09-18T06:00:00Z",
            scan_complete=complete,
            checkpoint=checkpoint,
            window_end=window_end,
        )
        self.assertTrue(complete)
        self.assertEqual(updated["last_successful_fetch"], "2026-09-18T06:00:00Z")
        self.assertNotIn("pending_window", updated)
        self.assertEqual([row["external_id"] for row in candidates], ["300"])

    def test_dense_scan_persists_resume_cursor_without_advancing_watermark(self):
        first = envelope([summary(400)], offset=0, total=1200, next_offset=200)
        next_pages = [
            envelope([summary(401 + index)], offset=200 * (index + 1), total=1200,
                     next_offset=200 * (index + 2))
            for index in range(4)
        ]
        previous = {
            "initialized": True,
            "last_successful_fetch": "2026-09-01T12:00:00Z",
            "seen": {},
        }
        client = FakeClient(search_pages=[first], next_pages=next_pages)
        items, checkpoint, complete, window_end = monitor.scan_seller(
            client, {"id": "example", "marketplace": "EBAY_GB"}, previous,
            detected_at="2026-09-18T06:00:00Z",
            max_calls=5,
        )
        self.assertFalse(complete)
        self.assertEqual(len(client.calls), 5)
        self.assertEqual(len(items), 5)
        self.assertTrue(checkpoint["pending"])
        updated, candidates, _ = monitor.update_seller_state(
            previous,
            items,
            "2026-09-18T06:00:00Z",
            scan_complete=complete,
            checkpoint=checkpoint,
            window_end=window_end,
        )
        self.assertEqual(updated["last_successful_fetch"], "2026-09-01T12:00:00Z")
        self.assertIn("pending_window", updated)
        self.assertEqual(len(candidates), 5)
        self.assertTrue(all(item["key"] in updated["seen"] for item in items))

    def test_next_run_resumes_with_next_link_and_completed_resume_clears_checkpoint(self):
        prior_checkpoint = {
            "version": 1,
            "start": "2026-09-01T11:50:00Z",
            "end": "2026-09-18T06:00:00Z",
            "pending": [{
                "start": "2026-09-01T11:50:00Z",
                "end": "2026-09-18T06:00:00Z",
                "offset": 1000,
                "next": next_url(1000),
            }],
            "completed": False,
            "successful_pages": 5,
            "last_error": "",
            "last_attempt_at": "2026-09-18T06:00:00Z",
        }
        previous = {
            "initialized": True,
            "last_successful_fetch": "2026-09-01T12:00:00Z",
            "pending_window": prior_checkpoint,
            "seen": {},
        }
        client = FakeClient(next_pages=[envelope([summary(500)], offset=1000, total=1001)])
        items, checkpoint, complete, window_end = monitor.scan_seller(
            client, {"id": "example", "marketplace": "EBAY_GB"}, previous,
            detected_at="2026-09-18T07:00:00Z",
        )
        self.assertTrue(complete)
        self.assertEqual(client.calls[0]["method"], "search_next")
        self.assertEqual(window_end, "2026-09-18T06:00:00Z")
        updated, _, _ = monitor.update_seller_state(
            previous,
            items,
            "2026-09-18T07:00:00Z",
            scan_complete=complete,
            checkpoint=checkpoint,
            window_end=window_end,
        )
        self.assertEqual(updated["last_successful_fetch"], "2026-09-18T06:00:00Z")
        self.assertNotIn("pending_window", updated)

    def test_failed_resume_keeps_checkpoint_and_watermark(self):
        prior_checkpoint = {
            "version": 1,
            "start": "2026-09-01T11:50:00Z",
            "end": "2026-09-18T06:00:00Z",
            "pending": [{
                "start": "2026-09-01T11:50:00Z",
                "end": "2026-09-18T06:00:00Z",
                "offset": 200,
                "next": next_url(200),
            }],
            "completed": False,
            "successful_pages": 1,
            "last_error": "",
        }
        previous = {
            "initialized": True,
            "last_successful_fetch": "2026-09-01T12:00:00Z",
            "pending_window": prior_checkpoint,
            "seen": {},
        }
        client = FakeClient(next_pages=[ValueError("bad next page")])
        items, checkpoint, complete, window_end = monitor.scan_seller(
            client, {"id": "example", "marketplace": "EBAY_GB"}, previous,
            detected_at="2026-09-18T07:00:00Z",
        )
        self.assertEqual(items, [])
        self.assertFalse(complete)
        self.assertIn("bad next page", checkpoint["last_error"])
        updated, _, _ = monitor.update_seller_state(
            previous,
            items,
            "2026-09-18T07:00:00Z",
            scan_complete=complete,
            checkpoint=checkpoint,
            window_end=window_end,
        )
        self.assertEqual(updated["last_successful_fetch"], "2026-09-01T12:00:00Z")
        self.assertTrue(updated["pending_window"]["pending"])

    def test_baseline_never_paginates_beyond_newest_200(self):
        rows = [summary(100000000000 + index, f"Book {index}") for index in range(200)]
        client = FakeClient(baseline_pages=[rows])
        seller = {"id": "example", "marketplace": "EBAY_GB"}
        items, checkpoint, complete, window_end = monitor.scan_seller(
            client, seller, None, detected_at="2026-09-18T06:00:00Z"
        )
        self.assertEqual(len(items), 200)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["method"], "search")
        self.assertIsNone(checkpoint)
        self.assertTrue(complete)
        self.assertEqual(window_end, "2026-09-18T06:00:00Z")


if __name__ == "__main__":
    unittest.main()
