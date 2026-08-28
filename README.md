# Charity Photobook Monitor

A GitHub Actions monitoring system for newly listed collectible photography books and related bargains. It combines near-real-time charity-shop monitoring, an authenticated eBay API layer, specialist-market feeds, and a separate wider-web search for awkward sites.

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

The first live Actions runs established that eBay's public HTML pages and Biblio return HTTP 403 to GitHub-hosted runners, and broad AbeBooks result pages were not reliable enough to parse. eBay is now queried through its official production Browse API instead of scraping those blocked pages.

### Authenticated eBay UK discovery

Every hour, the workflow asks the eBay Browse API for the newest fixed-price UK listings matching `photobook` and `photography book`. It reads up to 200 results per query, restricts the broad searches to eBay's Books category, and silently baselines the currently visible stock when the API source first activates.

The same run searches eBay for 24 exact Parr / Badger contributor and title combinations. Together with the two broad searches, this uses about 624 API calls per day, comfortably below the production keyset's 5,000-call daily limit. Authentication uses short-lived application tokens generated at run time from the encrypted `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET` repository secrets.

### Selected eBay charity sellers

`ebay_seller_monitor.py` separately checks 75 selected charity and library sellers every 30 minutes: 61 on eBay UK and 14 on eBay US. Each seller gets an independent Books-category query, so a large seller cannot consume a shared 200-result page and hide stock from smaller shops. The US searches also require delivery availability to Great Britain.

The first successful search for each seller silently records its newest 200 fixed-price books. Later runs use that seller's last successful timestamp with a ten-minute overlap, then alert only on previously unseen listings that contain photography-book signals or match the combined Parr/Badger and Roth canon. Seller state is isolated, so one temporary seller failure does not turn existing stock into new alerts.

At the normal cadence the 75 seller queries use about 3,600 Browse API calls per day. With the existing 624 daily broad and exact-title calls, expected usage is about 4,224 of the 5,000-call daily allowance. Extra pages are requested only when one seller has more than 200 books inside an incremental search window.

### GitHub-hosted specialist feeds

Every hour it checks current inventory from:

- The Photographers' Gallery new arrivals.
- Photobookstore.
- Village Books.
- Setanta Books.

Only newly seen listings that match the Parr / Badger master are surfaced.

### Rotating exact-title eBay and AbeBooks sweep

Every hourly run also selects 24 Parr / Badger records and searches them directly on both eBay and AbeBooks. Direct AbeBooks title/author searches worked in live GitHub Actions testing even though the broad AbeBooks pages did not parse reliably.

The cursor is stored in `data/market_state.json`, so successive runs rotate through the master. At 24 records per hour, one complete 628-record rotation takes about 27 hours if runs complete normally. Each title query is silently baselined the first time it is visited. Later newly appearing matching copies can create an `EXTERNAL_NEW:` issue.

### Wider-web search for blocked or awkward sites

The separate hourly ChatGPT task `Photobook Wider Web Search` uses the same GitHub Parr / Badger master and concentrates on sources better handled by web search rather than GitHub scraping, including:

- eBay UK as an independent web-search safety net.
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
- **Selected eBay charity sellers:** minutes 9 and 39 of every hour.
- **Photobook Wider Web Search:** hourly condition watch.
- **Charity Photobook New Listings:** hourly condition watch for the GitHub issue-review and value-verification stage.

GitHub scheduled jobs can start a few minutes late, so these are approximate rather than hard real-time guarantees.

## Baseline behaviour

All monitors use persistent state and silently baseline existing inventory when a source or targeted query is first introduced. This prevents a new source from flooding the issue queue with its entire existing catalogue.

The live issue-processing pipeline is intentionally restricted to genuinely newly detected listings. Historical full scans and bulk candidate pools are separate tools and are not treated as new-listing alerts.

## Full scans

`full_scan.py`, `parent_full_scan.py`, `charity_full_scan.py` and `bhf_full_scan.py` remain available for occasional catalogue auditing and backfill work. Their outputs are not part of the normal new-listing alert stream.

### Full catalogue gem audit

`.github/workflows/catalogue-audit.yml` is a separate, resumable one-off audit of the complete Oxfam Art & Photography parent category. It runs the proven segmented crawl, evaluates every live product and creates two ranked review tracks:

- `collection` for canonical, collectible, historically important, scarce or strongly relevant photobooks;
- `cheap` for worthwhile books at £20 or below, including useful additions to qualifying promotional baskets.

The deterministic scores are high-recall triage rather than buy recommendations. The workflow stores a compact queue in `data/oxfam_catalogue_audit_queue.json`, creates review batches with the `OXFAM_CATALOGUE_AUDIT:` prefix and opens a master `OXFAM_CATALOGUE_AUDIT_REPORT:` issue. These prefixes are intentionally excluded from the live new-listing reviewer so historical stock never contaminates `OXFAM_NEW:` alerts.

## Manual tests

In GitHub Actions you can manually run:

- **Charity photobook monitor** for Oxfam Photography, Shelter, Crisis and the existing external radar.
- **Oxfam broad Art and Photography monitor** for the wider Oxfam safety net.
- **Comprehensive photobook market discovery** for the authenticated eBay search, specialist photobook shops, and the rotating eBay and AbeBooks sweep.
- **eBay charity seller photobook monitor** for the 61 UK and 14 US seller-specific searches.

Each scheduled workflow validates the Parr / Badger master before running. Source failures are isolated where possible, while an all-source failure makes the job fail visibly rather than treating an empty response as valid inventory.
