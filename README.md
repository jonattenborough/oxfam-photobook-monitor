# Charity Photobook Monitor

A GitHub Actions monitor for newly listed collectible photography books and related bargains at Oxfam, Shelter and Crisis.

## What it monitors

### Oxfam

- Oxfam UK's **Art & Photography Books** category.
- Uses Oxfam's public Oracle Commerce storefront search endpoint.
- Verifies results are sorted newest first by `product.creationDate`.
- Checks the newest 30 listings every 10 minutes.
- Tracks stable `HD_...` SKU IDs in `data/state.json`.
- Creates an `OXFAM_NEW:` GitHub issue only when genuinely new SKUs appear.

### Shelter

The Shopify monitor checks:

- **Art & Photography**: every newly listed available product is surfaced for analysis.
- **Antiquarian, Rare & Collectable Books**: new books are surfaced when photography, rarity, edition or target-title signals match.
- **Second Hand Books**: a broad safety-net scan catches miscategorised photobooks using photographer/title and edition keywords.

Shelter products are deduplicated across collections using their Shopify product IDs.

### Crisis

- Checks the complete **Books** collection.
- Every newly listed available book is surfaced for AI analysis because the collection is small enough to review cheaply and this avoids missing badly categorised photobooks.

Shelter and Crisis state is stored separately in `data/charity_state.json`.

## Alert pipeline

The GitHub workflow detects new inventory first. New candidate listings create either an `OXFAM_NEW:` or `CHARITY_NEW:` issue containing the product link, price, description and available catalogue metadata. A separate ChatGPT scheduled task reviews those issues, verifies exact editions and market value, and emails only genuinely noteworthy bargains.

## Schedule

The workflow runs at minutes 3, 13, 23, 33, 43 and 53 of every hour. GitHub scheduled jobs can occasionally start late, so this is approximately every 10 minutes rather than a hard real-time guarantee.

## Baseline behaviour

Oxfam was seeded from the captured live catalogue on 21 August 2026. Shelter and Crisis silently seed all products found on their first successful run. This prevents hundreds of existing listings from being falsely announced as new. Future additions are then compared against those persistent product IDs.

## Full Oxfam catalogue scan

`full_scan.py` and the separate full-scan workflow remain available for occasional complete Oxfam category sweeps. The live 10-minute monitor is intentionally much smaller and faster.

## Manual test

Use **Actions → Charity photobook monitor → Run workflow**. A successful run prints counts for Oxfam, Shelter and Crisis and persists any required baseline/state changes. If a source request fails, the job fails before silently treating an empty response as valid inventory.
