# Charity Photobook Monitor

A GitHub Actions monitoring system for newly listed collectible photography books and related bargains. It combines near-real-time charity-shop monitoring, an authenticated eBay API layer, specialist-market feeds, and a separate wider-web search for awkward sites.

## Parr / Badger master

The operational reference database lives in `data/parr_badger_master/` and currently contains 628 search records across *The Photobook: A History* Volumes I, II and III.

`parr_badger_runner.py` loads the master, normalizes punctuation and accents, and performs contributor-aware exact and fuzzy title matching. Short generic titles require contributor evidence, and BROAD records use stricter thresholds than CORE records.

A Parr / Badger match is a discovery signal only. Exact edition, printing, completeness, condition and market value still need verification before purchase.

The private-seller discovery engine layers this canon with Roth 101, high-priority collector targets, a curated contemporary-documentary layer and a source-backed specialist-publisher snapshot. The combined recognition library currently contains about 4,300 unique books. Lower-confidence publisher-backlist records rotate more slowly and cannot overwrite or downgrade canonical tiers or supply reissue metadata to an original-edition record.

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

The 24 exact Parr / Badger target searches still run on AbeBooks, but their eBay allocation now belongs to private-seller discovery. The market monitor retains its two broad all-seller eBay searches (48 Browse calls/day). Authentication uses short-lived application tokens generated at run time from the encrypted `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET` repository secrets.

### eBay Endgame Auction Radar

`.github/workflows/ebay-endgame.yml` is the primary eBay auction system. A single worker wakes approximately every five minutes. Deadline checks run on every wake-up, while discovery becomes due every 15 minutes. This avoids relying on a last-minute rediscovery query and still tolerates delayed GitHub cron starts.

The radar searches auctions only and uses three independent discovery nets:

- 175 named photographers split into exact 50/70/55 priority tiers, including configured spelling and accent variants;
- broad local-language unknown-unicorn searches;
- keyword-free Books-category sweeps as a backstop for badly titled listings.

Tier 1 is revisited across all 16 Browse marketplaces within six hours and in the four major marketplaces within three hours, with a 30-hour ending horizon. Tier 2 uses 12-hour and six-hour revisits with a 36-hour horizon. Tier 3 uses 24-hour and 12-hour revisits with a 48-hour horizon. Selected high-priority photobook titles from the local recognition library receive an additional major-market lane with a 30-hour horizon.

Every keyword lane searches the listing title and seller description using `filter=searchInDescription:true`. Discovery does not require UK delivery and does not restrict seller account type, so business, private and unknown-account auctions can all enter the pool. Results are deduplicated by the underlying eBay item ID across queries and marketplaces. Dense searches are divided by ending time before additional pagination follows eBay's returned `next` URL.

Candidates are stored in `data/ebay_endgame_state.json`. The worker selectively calls `getItem` when an alert deadline arrives, keeps auction bid fields, and creates an early GitHub issue within roughly 24 hours plus a stronger-candidate update around four hours before the end. A late discovery is surfaced immediately. A failed live refresh does not silently discard a promising auction; the issue is marked `LIVE STATUS NOT VERIFIED - CHECK BEFORE BIDDING`.

The configured primary matrix is about 2,400 first-page searches/day. Endgame has a hard 3,600-call daily state cap, so roughly 1,200 calls remain for pagination and live item refreshes. It also stops before the shared 650-call reserve.

### Private-seller mispricing discovery

The scheduled entry point is `ebay_private_recall_monitor.py --config data/ebay_private_recall_searches.json`. It reuses `ebay_private_seller_monitor.py` to search eBay UK individual accounts through the official `sellerAccountTypes:{INDIVIDUAL}` filter. It combines broad and job-lot searches, collectible-format searches, wrong-category searches, hot canonical targets and a rotating slice of the full recognition library. Every scheduled query is restricted to fixed-price and Best Offer listings. Auction discovery belongs exclusively to the Endgame Radar. Search-in-description is used on high-recall lanes. Incremental queries inherit the previous-run timestamp; library, hot-title, priority-photographer and active-stock searches deliberately examine existing inventory.

Results are matched and scored locally for collectibility and possible seller under-description. The recall-first workflow spends no Browse calls on mandatory live-detail checks: search-result-only leads, cheap unknown photobooks and materially improved seen listings can reach AI review. These are leads, not purchase recommendations; downstream review must freshly verify seller type, availability, exact edition, completeness and checkout cost. The legacy monitor's live-detail path remains available but is not the scheduled entry point.

The hand-curated contemporary layer favours respected documentary practice, important first monographs, independently recognised recent books and verifiable scarce physical editions. A plain expensive copy of a recent award winner is not enough on its own: it needs a genuine price, edition or high-recall discovery signal before it can cross the alert threshold.

The private schedule runs once per hour at minute 4. Each run is capped at 17 Browse searches, for a maximum of 408 calls/day. Five of those searches rotate grouped title-and-description queries for the shared 175 core photographers, split two Tier 1, two Tier 2 and one Tier 3 group per hour. This covers every current group in about ten hours without increasing the API ceiling. The remaining calls preserve broad, collectible, wrong-category, active-stock and library-title discovery. Quota pacing protects a 650-call safety reserve and budgets for 164 shared calls/hour: 150 for Endgame, 12 charity seller searches and two market searches. Quota lookup failure cannot raise the 17-call ceiling.

A normal 17-call plan uses five long-tail library searches, four active-stock pages and one search from each of the broad, contemporary-hot, classic-hot, contemporary-contributor, classic-contributor, collectible-format, collection and wrong-category lanes. Lane cursors advance only over selected prefixes, so quota trimming cannot silently skip library records or inventory pages.

This monitor is now a bounded fixed-price safety net rather than the primary eBay discovery engine. Endgame receives priority over it whenever the shared quota becomes constrained.

The scheduled workflow files for the private historical backfill, international historical backfill, seller inventory back-search and forensic audit have been deleted. Their completed data and implementation files remain as historical evidence, but they have no GitHub Actions schedule or manual dispatch entry point. The full-library scan, international accelerator and BHF full scan remain available only through manual workflow dispatch.

### Selected eBay charity sellers

`ebay_seller_monitor.py` separately checks 103 selected charity and library sellers: 89 on eBay UK and 14 on eBay US. The hourly workflow processes 12 sellers in a persistent round-robin batch, so every seller is normally revisited within nine runs. At one page per seller this uses 288 calls/day. Each seller gets an independent Books-category query, so a large seller cannot consume a shared 200-result page and hide stock from smaller shops. Every returned item is also matched locally against the same 175 core photographers. A core match is promoted and sorted by tier even if the generic photobook filter would not have qualified it, with no extra Browse call. Incremental scans can use up to five pages per seller; a 650-call quota reserve and worst-case page headroom checks remain in place. The US searches also require delivery availability to Great Britain.

The first successful search for each seller silently records its newest 200 fixed-price books. Later runs use that seller's last successful timestamp with a ten-minute overlap, then alert only on previously unseen listings that contain photography-book signals or match the combined Parr/Badger and Roth canon. Seller state is isolated, so one temporary seller failure does not turn existing stock into new alerts.

The planned steady-state allocation is 3,600 calls/day for Endgame, up to 408 for the private fixed-price monitor, about 288 for charity sellers and 48 for the wider market feeds. That totals about 4,344 calls/day before unusual charity pagination, leaving roughly 656 calls of headroom against the standard 5,000-call allowance.

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
- `ENDGAME_EARLY:`
- `ENDGAME_4H:`

The comprehensive market monitor deliberately uses `EXTERNAL_NEW:` so the existing downstream ChatGPT issue-review task processes it. That task verifies exact edition, printing, completeness, condition, all-in UK price and comparable copies before any email alert.

## Schedules

- **Charity photobook monitor:** minutes 3, 13, 23, 33, 43 and 53 of every hour.
- **Oxfam broad Art & Photography monitor:** minutes 6, 16, 26, 36, 46 and 56 of every hour.
- **Comprehensive photobook market discovery:** minute 27 of every hour.
- **Selected eBay charity sellers:** minute 9 of every hour.
- **eBay private-seller fixed-price discovery:** minute 4 of every hour.
- **eBay Endgame Auction Radar:** approximately every five minutes; discovery every 15 minutes and deadline checks every run.
- **Full-library scan, international accelerator and BHF full scan:** manual dispatch only.
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
- **eBay charity seller photobook monitor** for the 89 UK and 14 US seller-specific searches.
- **eBay Endgame Auction Radar** for a forced discovery pass plus all currently due auction deadlines.
- **eBay private full-library scan**, **international backfill accelerator** and **BHF full book inventory scan** only when a deliberate high-quota maintenance run is required.

Each scheduled workflow validates the Parr / Badger master before running. Source failures are isolated where possible, while an all-source failure makes the job fail visibly rather than treating an empty response as valid inventory.
