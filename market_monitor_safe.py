#!/usr/bin/env python3
"""Run the comprehensive market monitor using sources proven to work on GitHub.

The first live Actions runs established that general eBay UK and Biblio return
HTTP 403 to GitHub-hosted runners, while broad AbeBooks HTML did not parse
reliably. Those sources are covered by the separate hourly wider-web search.

This wrapper keeps the GitHub job focused on working specialist Shopify feeds
and rotating exact Parr/Badger searches on AbeBooks.
"""
from __future__ import annotations

import market_monitor as monitor

# Keep only live-tested specialist feeds that returned inventory successfully.
monitor.FEEDS = [
    source for source in monitor.FEEDS
    if source.get("id") in {"tpg_new", "photobookstore", "village", "setanta"}
]

# Direct title searches on AbeBooks succeeded in the live Actions runs.
# General eBay and Biblio are deliberately handled by the wider-web automation.
monitor.TARGET_MARKETS = ("abebooks",)

if __name__ == "__main__":
    raise SystemExit(monitor.main())
