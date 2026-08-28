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
        self.assertEqual(search_request.get_header("X-ebay-c-marketplace-id"), "EBAY_GB")

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


if __name__ == "__main__":
    unittest.main()
