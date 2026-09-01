from __future__ import annotations

import json
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


class EbayPrivateApiTests(unittest.TestCase):
    @mock.patch("ebay_api.urllib.request.urlopen")
    def test_private_search_uses_individual_description_and_price_filters(self, urlopen):
        urlopen.side_effect = [
            FakeResponse({"access_token": "short-lived-token"}),
            FakeResponse({"itemSummaries": []}),
        ]
        client = ebay_api.EbayBrowseClient("app-id", "cert-id")
        client.search(
            "photography book",
            category_ids="261186",
            fixed_price_only=False,
            buying_options=["FIXED_PRICE", "BEST_OFFER"],
            seller_account_type="INDIVIDUAL",
            delivery_country="GB",
            search_in_description=True,
            price_max=750,
            price_currency="GBP",
        )
        request = urlopen.call_args_list[1].args[0]
        params = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        self.assertEqual(params["searchInDescription"], ["true"])
        self.assertEqual(
            params["filter"],
            [
                "buyingOptions:{FIXED_PRICE|BEST_OFFER},"
                "sellerAccountTypes:{INDIVIDUAL},deliveryCountry:GB,"
                "price:[..750],priceCurrency:GBP"
            ],
        )

    @mock.patch("ebay_api.urllib.request.urlopen")
    def test_get_item_encodes_rest_item_id_and_returns_live_record(self, urlopen):
        urlopen.side_effect = [
            FakeResponse({"access_token": "short-lived-token"}),
            FakeResponse(
                {
                    "itemId": "v1|123456789012|0",
                    "title": "Photography book",
                    "estimatedAvailabilityStatus": "IN_STOCK",
                    "buyingOptions": ["FIXED_PRICE"],
                    "itemEndDate": "2030-01-01T00:00:00Z",
                }
            ),
        ]
        client = ebay_api.EbayBrowseClient("app-id", "cert-id")
        is_live, reason, item = client.live_status("v1|123456789012|0")
        self.assertTrue(is_live)
        self.assertEqual(reason, "live")
        self.assertEqual(item["title"], "Photography book")
        request = urlopen.call_args_list[1].args[0]
        self.assertIn("v1%7C123456789012%7C0", request.full_url)


if __name__ == "__main__":
    unittest.main()
