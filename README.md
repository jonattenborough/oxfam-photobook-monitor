# Charity Photobook Monitor

A GitHub Actions monitor for newly listed collectible photography books and related bargains at Oxfam, Shelter, Crisis and selected external charity/used-book outlets.

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

### External public-page radar

`external_monitor.py` checks newly surfaced items on public pages from:

- British Heart Foundation eBay Books.
- British Heart Foundation eBay Vintage & Collectable.
- British Red Cross eBay.
- Scope eBay.
- Marie Curie eBay.
- Sue Ryder Pre-loved eBay Books.
- World of Books Photography.
- World of Books Old & Rare Art, Fashion & Photography.
- Awesome Books Art, Fashion & Photography.

The eBay pages are requested with newest-listing sorting where available. World of Books collection URLs request newest-created sorting. The external monitor stores stable eBay item IDs or product paths in `data/external_state.json` and creates `EXTERNAL_NEW:` issues only for unseen candidates.

Each external source silently baselines the items visible on its first successful fetch, so adding a source does not create alerts for its existing back catalogue. A temporary failure from one external site does not erase its state or make the whole source set look empty. If every external source fails in the same run, the monitor fails visibly rather than treating that as valid inventory.

Because general charity book feeds can be noisy, the eBay and broad Awesome Books feeds use a deliberately broad photobook radar covering photography terms, important photographers, specialist publishers, visual-art context and collectible edition clues. The two World of Books photography-focused feeds surface all newly seen products for AI review.

## Alert pipeline

The GitHub workflow detects new inventory first. New candidate listings create `OXFAM_NEW:`, `CHARITY_NEW:` or `EXTERNAL_NEW:` issues containing the product link, price and available catalogue/page metadata. The separate Oxfam parent-category monitor creates `OXFAM_ART_NEW:` issues for newly listed products outside the dedicated Photography subsection. A separate ChatGPT scheduled task reviews only these new-listing issues, verifies exact editions and market value, and emails only genuinely noteworthy bargains.

## Schedule

The main workflow runs at minutes 3, 13, 23, 33, 43 and 53 of every hour. GitHub scheduled jobs can occasionally start late, so this is approximately every 10 minutes rather than a hard real-time guarantee.

## Baseline behaviour

Oxfam was seeded from the captured live catalogue on 21 August 2026. Shelter and Crisis silently seed all products found on their first successful run. Each external source also silently seeds the items visible on its first successful run. This prevents existing listings from being falsely announced as new. Future additions are then compared against those persistent IDs or product paths.

## Full Oxfam catalogue scan

`full_scan.py` and the separate full-scan workflow remain available for occasional complete Oxfam category sweeps. The live monitor is intentionally much smaller and faster. Full-scan issues are not part of the hourly ChatGPT new-listings watch.

## Manual test

Use **Actions -> Charity photobook monitor -> Run workflow**. A successful run prints counts for Oxfam, Shelter, Crisis and the external public-page sources, then persists any required baseline/state changes. If Oxfam, Shelter or Crisis fails, the job fails rather than silently treating an empty response as valid inventory. External sources are isolated so a temporary block on one site does not suppress the other working sources.
