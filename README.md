# Charity Photobook Monitor

A GitHub Actions monitoring system for newly listed collectible photography books and related bargains. It combines near-real-time charity-shop monitoring, a live-tested specialist-market layer, and a separate wider-web search for sites that block GitHub runners.

## Parr / Badger master

The operational reference database lives in `data/parr_badger_master/` and currently contains 628 search records across *The Photobook: A History* Volumes I, II and III.

`parr_badger_runner.py` loads the master, normalizes punctuation and accents, and performs contributor-aware exact and fuzzy title matching. Short generic titles require contributor evidence, and BROAD records use stricter thresholds than CORE records.

A Parr / Badger match is a discovery signal only. Exact edition, printing, completeness, condition and market value still need verification before purchase.

## Near-real-time monitors

### Oxfam Photography

- Watches Oxfam UK's dedicated Art & Photography Books category.
- Uses Oxfam's public Oracle Commerce storefront search endpoint.
- Verifies newest-first `product.creationDate` ordering.
- Tracks stable `HD_...` SKU IDs in `data/state.json`.
- Creates `OXFAM_NEW:` issues only for genuinely new SKUs.
- Runs through the Parr / Badger matcher before issue creation.

### Oxfam broad Art & Photography

`parent_monitor.py` watches the wider Art & Photography parent category to catch photobooks filed outside the dedicated Photography subsection. It excludes SKUs already handled by the child Photography monitor and creates `OXFAM_ART_NEW:` issues for genuinely new outside-child listings.

### Shelter and Crisis

`charity_monitor.py` checks Shelter Art & Photography, Shelter Antiquarian/Rare/Collectable, Shelter Second Hand Books, and Crisis Books. Products are deduplicated by Shopify product ID. Parr / Badger matches can qualify an otherwise unremarkable listing for review.

### External charity and used-book radar

`external_monitor.py` checks British Heart Foundation, British Red Cross, Scope, Marie Curie and Sue Ryder eBay book feeds, plus World of Books and Awesome Books. Each source silently baselines current visible inventory on its first successful fetch. A temporary source failure does not erase state or make existing stock look new.

## Comprehensive market discovery

The hourly GitHub workflow uses `market_monitor_safe.py`, a live-tested wrapper around `market_monitor.py`.

The first live Actions runs established that general eBay UK and Biblio return HTTP 403 to GitHub-hosted runners, and broad AbeBooks result pages were not reliable enough to parse. The workflow therefore does not waste repeated requests on those blocked paths.

### GitHub-hosted specialist feeds

Every hour it checks current inventory from:

- The Photographers' Gallery new arrivals.
- Photobookstore.
- Village Books.
- Setanta Books.

Only newly seen listings that match the Parr / Badger master are surfaced.

### Rotating exact-title AbeBooks sweep

Every hourly run also selects 12 Parr / Badger records and searches them directly on AbeBooks. Direct AbeBooks title/author searches worked in live GitHub Actions testing even though the broad AbeBooks pages did not parse reliably.

The cursor is stored in `data/market_state.json`, so successive runs rotate through the master. Each title query is silently baselined the first time it is visited. Later newly appearing matching copies can create an `EXTERNAL_NEW:` issue.

### Wider-web search for blocked or awkward sites

The separate hourly ChatGPT task `Photobook Wider Web Search` uses the same GitHub Parr / Badger master and concentrates on sources better handled by web search rather than GitHub scraping, including:

- general eBay UK.
- Biblio.
- viaLibri.
- ZVAB.
- PBFA and independent antiquarian dealers.
- The Saleroom and other auction catalogues.
- Catawiki.
- specialist photobook dealers and newly indexed general-web listings.

This split gives us coverage without repeatedly hammering sites that reject GitHub's IP ranges.

## Alert pipeline

New GitHub candidates use one of these issue prefixes:

- `OXFAM_NEW:`
- `OXFAM_ART_NEW:`
- `CHARITY_NEW:`
- `EXTERNAL_NEW:`

The comprehensive market monitor deliberately uses `EXTERNAL_NEW:` so the existing downstream ChatGPT issue-review task processes it. That task verifies exact edition, printing, completeness, condition, all-in UK price and comparable copies before any email alert.

## Schedules

- **Charity photobook monitor:** minutes 3, 13, 23, 33, 43 and 53 of every hour.
- **Oxfam broad Art & Photography monitor:** minutes 6, 16, 26, 36, 46 and 56 of every hour.
- **Comprehensive photobook market discovery:** minute 27 of every hour.
- **Photobook Wider Web Search:** hourly condition watch.
- **Charity Photobook New Listings:** hourly condition watch for the GitHub issue-review and value-verification stage.

GitHub scheduled jobs can start a few minutes late, so these are approximate rather than hard real-time guarantees.

## Baseline behaviour

All monitors use persistent state and silently baseline existing inventory when a source or targeted query is first introduced. This prevents a new source from flooding the issue queue with its entire existing catalogue.

The live issue-processing pipeline is intentionally restricted to genuinely newly detected listings. Historical full scans and bulk candidate pools are separate tools and are not treated as new-listing alerts.

## Full scans

`full_scan.py`, `parent_full_scan.py`, `charity_full_scan.py` and `bhf_full_scan.py` remain available for occasional catalogue auditing and backfill work. Their outputs are not part of the normal new-listing alert stream.

## Manual tests

In GitHub Actions you can manually run:

- **Charity photobook monitor** for Oxfam Photography, Shelter, Crisis and the existing external radar.
- **Oxfam broad Art and Photography monitor** for the wider Oxfam safety net.
- **Comprehensive photobook market discovery** for specialist photobook shops and the rotating AbeBooks sweep.

Each scheduled workflow validates the Parr / Badger master before running. Source failures are isolated where possible, while an all-source failure makes the job fail visibly rather than treating an empty response as valid inventory.
