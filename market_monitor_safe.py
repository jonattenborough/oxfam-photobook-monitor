#!/usr/bin/env python3
"""Run the comprehensive monitor using reliable sources on GitHub Actions.

eBay is accessed through its authenticated production Browse API. If the two
repository secrets are not present yet, the other sources continue to run and
the eBay layer activates automatically as soon as they are added.
"""
from __future__ import annotations

import sys

import ebay_api
import market_monitor as monitor

# Keep only live-tested specialist feeds that returned inventory successfully.
monitor.FEEDS = [
    source for source in monitor.FEEDS
    if source.get("id") in {"tpg_new", "photobookstore", "village", "setanta"}
]

if ebay_api.configured():
    monitor.FEEDS += [
        {
            "id": "ebay_api_photobook",
            "name": "eBay UK newest photobooks via Browse API",
            "kind": "ebay_api",
            "query": "photobook",
            "category_ids": "261186",
            "limit": 200,
            "fixed_price_only": True,
        },
        {
            "id": "ebay_api_photo_book",
            "name": "eBay UK newest photography books via Browse API",
            "kind": "ebay_api",
            "query": "photography book",
            "category_ids": "261186",
            "limit": 200,
            "fixed_price_only": True,
        },
    ]
    monitor.TARGET_MARKETS = ("ebay_api", "abebooks")
else:
    print(
        "WARNING: eBay API disabled until EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are configured",
        file=sys.stderr,
    )
    monitor.TARGET_MARKETS = ("abebooks",)

if __name__ == "__main__":
    raise SystemExit(monitor.main())
