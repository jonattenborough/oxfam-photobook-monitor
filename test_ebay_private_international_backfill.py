from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import ebay_private_international_backfill as international
import ebay_private_seller_backfill as backfill


def raw_item(
    item_id: int,
    title: str,
    *,
    currency: str = "EUR",
    price: float = 20.0,
    feedback: int = 40,
) -> dict:
    return {
        "itemId": f"v1|{item_id}|0",
        "title": title,
        "price": {"value": f"{price:.2f}", "currency": currency},
        "shippingOptions": [
            {"shippingCost": {"value": "5.00", "currency": currency}}
        ],
        "seller": {"username": "mixed-household-seller", "feedbackScore": feedback},
        "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"],
        "itemCreationDate": "2026-06-20T10:00:00.000Z",
    }


class FakeClient:
    def __init__(self, pages, live_detail=None):
        self.pages = list(pages)
        self.live_detail = live_detail or {
            "seller": {"username": "mixed-household-seller", "sellerAccountType": "INDIVIDUAL", "feedbackScore": 40},
            "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"],
            "price": {"value": "20.00", "currency": "EUR"},
            "shippingOptions": [{"shippingCost": {"value": "5.00", "currency": "EUR"}}],
            "description": "Altes Fotobuch aus einer Sammlung",
        }
        self.search_calls = []
        self.live_calls = []

    def search(self, query, **kwargs):
        self.search_calls.append({"query": query, **kwargs})
        return self.pages.pop(0)

    def live_status(self, item_id):
        self.live_calls.append(item_id)
        return True, "live", self.live_detail


class InternationalPrivateBackfillTests(unittest.TestCase):
    def setUp(self):
        self.config = international.load_config(Path("data/ebay_private_international_markets.json"))

    def test_config_covers_filterable_europe_and_heuristic_overseas_markets(self):
        markets = {market["marketplace"]: market for market in self.config["markets"]}
        self.assertEqual(len(markets), 12)
        self.assertEqual(markets["EBAY_DE"]["seller_filter_mode"], "individual")
        self.assertEqual(markets["EBAY_IE"]["seller_filter_mode"], "individual")
        self.assertEqual(markets["EBAY_US"]["seller_filter_mode"], "heuristic")
        self.assertEqual(markets["EBAY_US"]["issue_threshold"], 80)

    def test_plan_is_deep_resumable_and_does_not_broad_search_heuristic_markets(self):
        start = datetime(2025, 9, 1, 12, 0, tzinfo=timezone.utc)
        end = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        plan = international.build_plan(self.config, start, end, slice_days=21)
        self.assertGreaterEqual(len(plan), 1600)
        identities = {
            (
                step["marketplace"],
                step["lane"],
                step["query"].lower(),
                step["window_start"],
                step["window_end"],
                step["offset"],
            )
            for step in plan
        }
        self.assertEqual(len(identities), len(plan))
        broad_markets = {
            step["marketplace"] for step in plan if step["lane"] == "international_broad"
        }
        self.assertIn("EBAY_DE", broad_markets)
        self.assertNotIn("EBAY_US", broad_markets)
        self.assertTrue(
            any(
                step["marketplace"] == "EBAY_US"
                and step["lane"] == "international_priority_exact"
                for step in plan
            )
        )

    def test_european_search_uses_individual_filter_and_gbp_landed_estimate(self):
        market = next(value for value in self.config["markets"] if value["marketplace"] == "EBAY_DE")
        step = {
            "marketplace": "EBAY_DE",
            "market_country": "DE",
            "seller_filter_mode": "individual",
            "delivery_country": "GB",
            "price_currency": "EUR",
            "price_max": market["price_max"],
            "gbp_rate": market["gbp_rate"],
            "issue_threshold": 72,
            "lane": "international_priority_exact",
            "query": "Richard Billingham Ray's a Laugh",
            "window_start": "2025-09-01T00:00:00Z",
            "window_end": "2026-09-01T00:00:00Z",
            "category_ids": None,
            "search_in_description": True,
            "buying_options": ["FIXED_PRICE", "BEST_OFFER"],
            "offset": 0,
        }
        client = FakeClient([[raw_item(123, "Richard Billingham Ray's a Laugh")]])
        items, count = backfill.search_page({"EBAY_DE": client}, self.config, step)
        self.assertEqual(count, 1)
        call = client.search_calls[0]
        self.assertEqual(call["seller_account_type"], "INDIVIDUAL")
        self.assertEqual(call["price_currency"], "EUR")
        self.assertEqual(items[0]["price_gbp"], 17.2)
        self.assertEqual(items[0]["landed_price_gbp"], 21.5)
        self.assertEqual(items[0]["key"], "ebay:123")

    def test_us_search_uses_strict_local_heuristic_without_unsupported_filter(self):
        market = next(value for value in self.config["markets"] if value["marketplace"] == "EBAY_US")
        step = {
            "marketplace": "EBAY_US",
            "market_country": "US",
            "seller_filter_mode": "heuristic",
            "delivery_country": "GB",
            "price_currency": "USD",
            "price_max": market["price_max"],
            "gbp_rate": market["gbp_rate"],
            "issue_threshold": 80,
            "lane": "international_priority_exact",
            "query": "Mary Ellen Mark Falkland Road",
            "window_start": "2025-09-01T00:00:00Z",
            "window_end": "2026-09-01T00:00:00Z",
            "category_ids": None,
            "search_in_description": True,
            "buying_options": ["FIXED_PRICE", "BEST_OFFER"],
            "offset": 0,
        }
        client = FakeClient([[]])
        backfill.search_page({"EBAY_US": client}, self.config, step)
        self.assertIsNone(client.search_calls[0]["seller_account_type"])

    def test_same_listing_seen_on_two_marketplaces_is_live_checked_once(self):
        plan = international.build_plan(
            self.config,
            datetime(2025, 9, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        de_step = next(step for step in plan if step["marketplace"] == "EBAY_DE")
        fr_step = dict(de_step, marketplace="EBAY_FR", market_country="FR")
        item_id = 123456789012
        title = "Richard Billingham Rays a Laugh old photography book"
        de_client = FakeClient([[raw_item(item_id, title)]])
        fr_client = FakeClient([[raw_item(item_id, title)]])
        state = {
            "version": 1,
            "queue": [de_step, fr_step],
            "pending_live": {},
            "reviewed": {},
            "complete": False,
        }
        findings = {"version": 1, "items": {}}
        result = backfill.run_backfill(
            {"EBAY_DE": de_client, "EBAY_FR": fr_client},
            self.config,
            state,
            findings,
            {"seen": {}},
            call_budget=3,
            max_live_checks=1,
            detected_at="2026-09-01T20:00:00Z",
        )
        self.assertEqual(result["unique_unseen_results"], 1)
        self.assertEqual(result["live_checks"], 1)
        self.assertEqual(len(de_client.live_calls) + len(fr_client.live_calls), 1)
        self.assertIn(f"ebay:{item_id}", state["reviewed"])

    def test_completed_state_is_not_restarted(self):
        state = {"version": 1, "queue": [], "pending_live": {}, "complete": True}
        changed = international.initialize_state(
            state,
            self.config,
            detected_at="2026-09-01T20:00:00Z",
            lookback_days=365,
            slice_days=21,
            new_window=False,
        )
        self.assertFalse(changed)
        self.assertEqual(state["queue"], [])
        self.assertTrue(state["complete"])

    def test_known_state_combines_uk_live_and_backfill_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live_path = root / "live.json"
            findings_path = root / "findings.json"
            live_path.write_text(json.dumps({"seen": {"ebay:1": {}}}), encoding="utf-8")
            findings_path.write_text(json.dumps({"items": {"ebay:2": {}}}), encoding="utf-8")
            combined = international.combined_known_state([live_path, findings_path])
        self.assertEqual(set(combined["seen"]), {"ebay:1", "ebay:2"})

    def test_review_history_excludes_every_previously_issued_listing(self):
        path = Path("data/ebay_private_seller_review_history.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["seen"]), 52)
        combined = international.combined_known_state([path])
        self.assertEqual(set(combined["seen"]), set(payload["seen"]))
        self.assertIn("ebay:267766751444", combined["seen"])
        self.assertIn("ebay:137624916954", combined["seen"])


if __name__ == "__main__":
    unittest.main()
