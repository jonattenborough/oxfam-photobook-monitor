from __future__ import annotations

import json
import os
import unittest
import urllib.parse
from unittest import mock

import ebay_api


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


class EbayApiTests(unittest.TestCase):
    def setUp(self):
        ebay_api._DEFAULT_CLIENT = None

    def test_configured_requires_both_repository_secrets(self):
        with mock.patch.dict(os.environ, {"EBAY_CLIENT_ID": "app-id", "EBAY_CLIENT_SECRET": ""}, clear=True):
            self.assertFalse(ebay_api.configured())
        with mock.patch.dict(os.environ, {"EBAY_CLIENT_ID": "app-id", "EBAY_CLIENT_SECRET": "cert-id"}, clear=True):
            self.assertTrue(ebay_api.configured())

    def test_missing_credentials_are_rejected(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ebay_api.EbayApiError, "both required"):
                ebay_api.EbayBrowseClient()

    @mock.patch("ebay_api.urllib.request.urlopen")
    def test_token_is_cached_and_search_is_newest_fixed_price(self, urlopen):
        urlopen.side_effect = [
            FakeResponse({"access_token": "short-lived-token", "expires_in": 7200}),
            FakeResponse({"itemSummaries": []}),
            FakeResponse({"itemSummaries": []}),
        ]
        client = ebay_api.EbayBrowseClient("app-id", "cert-id")
        client.search("photobook", limit=500, category_ids="261186")
        client.search("Robert Frank The Americans")
        self.assertEqual(urlopen.call_count, 3)

        token_request = urlopen.call_args_list[0].args[0]
        self.assertEqual(token_request.full_url, ebay_api.TOKEN_URL)
        self.assertEqual(token_request.get_method(), "POST")
        self.assertTrue(token_request.get_header("Authorization").startswith("Basic "))

        search_request = urlopen.call_args_list[1].args[0]
        params = urllib.parse.parse_qs(urllib.parse.urlparse(search_request.full_url).query)
        self.assertEqual(params["sort"], ["newlyListed"])
        self.assertEqual(params["limit"], ["200"])
        self.assertEqual(params["category_ids"], ["261186"])
        self.assertEqual(params["filter"], ["buyingOptions:{FIXED_PRICE}"])
        self.assertEqual(params["offset"], ["0"])
        self.assertEqual(search_request.get_header("X-ebay-c-marketplace-id"), "EBAY_GB")

    @mock.patch("ebay_api.urllib.request.urlopen")
    def test_category_only_seller_search_combines_filters(self, urlopen):
        urlopen.side_effect = [
            FakeResponse({"access_token": "short-lived-token"}),
            FakeResponse({"itemSummaries": []}),
        ]
        client = ebay_api.EbayBrowseClient("app-id", "cert-id", marketplace="EBAY_US")
        client.search(
            None,
            category_ids="261186",
            seller_ids=["goodwillbks"],
            delivery_country="gb",
            item_start_date="2026-08-28T12:00:00Z",
            offset=200,
            limit=200,
        )

        search_request = urlopen.call_args_list[1].args[0]
        params = urllib.parse.parse_qs(urllib.parse.urlparse(search_request.full_url).query)
        self.assertNotIn("q", params)
        self.assertEqual(params["category_ids"], ["261186"])
        self.assertEqual(params["offset"], ["200"])
        self.assertEqual(
            params["filter"],
            [
                "buyingOptions:{FIXED_PRICE},sellers:{goodwillbks},"
                "deliveryCountry:GB,itemStartDate:[2026-08-28T12:00:00Z..]"
            ],
        )
        self.assertEqual(search_request.get_header("X-ebay-c-marketplace-id"), "EBAY_US")

    @mock.patch("ebay_api.urllib.request.urlopen")
    def test_browse_quota_returns_most_restrictive_daily_rate(self, urlopen):
        urlopen.side_effect = [
            FakeResponse({"access_token": "short-lived-token"}),
            FakeResponse(
                {
                    "rateLimits": [
                        {
                            "apiContext": "buy",
                            "apiName": "browse",
                            "resources": [
                                {
                                    "name": "item_summary",
                                    "rates": [
                                        {
                                            "limit": 5000,
                                            "remaining": 731,
                                            "count": 4269,
                                            "reset": "2026-09-02T00:00:00.000Z",
                                            "timeWindow": 86400,
                                        }
                                    ],
                                },
                                {
                                    "name": "burst",
                                    "rates": [
                                        {"limit": 50, "remaining": 10, "count": 40, "timeWindow": 5}
                                    ],
                                },
                            ],
                        }
                    ]
                }
            ),
        ]
        quota = ebay_api.EbayBrowseClient("app-id", "cert-id").browse_quota()
        self.assertEqual(quota["resource"], "item_summary")
        self.assertEqual(quota["remaining"], 731)
        request = urlopen.call_args_list[1].args[0]
        self.assertTrue(request.full_url.startswith(ebay_api.RATE_LIMIT_URL))
        params = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        self.assertEqual(params, {"api_context": ["buy"], "api_name": ["browse"]})

    def test_search_requires_query_or_category(self):
        client = ebay_api.EbayBrowseClient("app-id", "cert-id")
        with self.assertRaisesRegex(ValueError, "query or category_ids"):
            client.search(None)

    @mock.patch("ebay_api.urllib.request.urlopen")
    def test_closed_item_start_date_range(self, urlopen):
        urlopen.side_effect = [
            FakeResponse({"access_token": "short-lived-token"}),
            FakeResponse({"itemSummaries": []}),
        ]
        client = ebay_api.EbayBrowseClient("app-id", "cert-id")
        client.search(
            None,
            category_ids="261186",
            item_start_date="1995-01-01T00:00:00Z",
            item_end_date="2020-01-01T00:00:00Z",
        )
        request = urlopen.call_args_list[1].args[0]
        params = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        self.assertEqual(
            params["filter"],
            ["buyingOptions:{FIXED_PRICE},itemStartDate:[1995-01-01T00:00:00Z..2020-01-01T00:00:00Z]"],
        )

    def test_summary_is_converted_to_monitor_listing(self):
        source = {"id": "ebay_api_photobook", "name": "eBay UK newest photobooks"}
        listing = ebay_api.listing_from_summary(
            {
                "itemId": "v1|168644925408|0",
                "title": "Robert Frank The Americans",
                "itemWebUrl": "https://www.ebay.co.uk/itm/168644925408",
                "price": {"value": "25.50", "currency": "GBP"},
                "seller": {"username": "bookseller"},
                "condition": "Used",
                "itemCreationDate": "2026-08-28T12:00:00.000Z",
                "buyingOptions": ["FIXED_PRICE"],
            },
            source,
        )
        self.assertIsNotNone(listing)
        assert listing is not None
        self.assertEqual(listing["key"], "ebay:168644925408")
        self.assertEqual(listing["price_gbp"], 25.50)
        self.assertEqual(listing["vendor"], "bookseller")
        self.assertIn("Used", listing["context"])

    def test_us_summary_retains_original_currency(self):
        source = {"id": "seller", "name": "US seller", "marketplace": "EBAY_US"}
        listing = ebay_api.listing_from_summary(
            {
                "itemId": "v1|123456789012|0",
                "title": "Photography book",
                "price": {"value": "19.95", "currency": "USD"},
            },
            source,
        )
        self.assertIsNotNone(listing)
        assert listing is not None
        self.assertEqual(listing["price_value"], 19.95)
        self.assertEqual(listing["price_currency"], "USD")
        self.assertIsNone(listing["price_gbp"])
        self.assertEqual(listing["url"], "https://www.ebay.com/itm/123456789012")


if __name__ == "__main__":
    unittest.main()
