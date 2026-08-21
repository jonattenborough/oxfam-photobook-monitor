# Oxfam Art & Photography parent-category expansion

This patch adds two independent jobs alongside the existing Oxfam Photography monitor.

## 1. One-off full parent scan

`parent_full_scan.py` crawls the entire Oxfam **Art & Photography** parent category at 90 products per request. It dynamically resolves the Oracle Commerce search dimension from the public route and traverses its leaf categories to bypass Oracle's roughly 10,000-result ceiling:

`/art-and-photography/category/art-photography`

It ranks broad photobook/collector candidates and stores the top 2,000 in:

`data/oxfam_parent_full_candidates.json`

The workflow creates an `OXFAM_ART_SCAN:` issue containing the strongest 50 candidates for market research.

## 2. Permanent 10-minute broad monitor

`parent_monitor.py` checks the newest 180 parent-category listings at approximately 10-minute intervals. It intentionally does **not** pre-filter by photography keywords. Any genuinely new parent-category SKU not already present in the dedicated Photography monitor's state or current Photography child-category results is sent to an `OXFAM_ART_NEW:` GitHub issue for AI review.

This design is deliberately conservative: a badly catalogued photobook should not be missed just because Oxfam called it an art/design/fashion book or failed to mention "photography".

The first live parent-monitor run silently seeds its newest 180 as a baseline, preventing false alerts for existing stock.
