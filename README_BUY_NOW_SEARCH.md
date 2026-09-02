# Buy-Now Wider-Web Photobook Search

This layer is the persistent source universe for the ChatGPT `Photobook Wider Web Search` task.

## Priority target watches

Every run must load `data/external_buy_now_priority_targets.json` before general discovery and search every active target first.

For each active target:

- search all eight always-on discovery sources on every run;
- search the current rotating specialist and antiquarian source batch;
- verify the live product page, exact target edition, current availability, landed Great Britain price, condition and completeness;
- alert only when the listing satisfies the target's stated budget and condition rules;
- reject sold, ended, cached, snippet-only, out-of-stock, auction-format and stale listings;
- include a lawful official look-inside or page-through link when available;
- deduplicate alerts by stable listing ID or canonical product URL.

A search result, marketplace index or aggregator record is discovery evidence only. It cannot trigger an alert unless the underlying live listing is currently purchasable.

## Policy

- Purchase opportunities must be available now at a fixed price, through a dealer inquiry, or through Best Offer.
- Auction-format, timed-auction, bid-only and pre-auction listings are excluded.
- Auction and sold records may still be used as valuation comparables.
- eBay is Buy It Now / Best Offer only.
- The wider-web task should not duplicate sources already covered reliably by the GitHub near-real-time monitors unless a clearly new listing escaped those monitors.

The hard auction-domain denylist and the complete source universe live in `data/external_buy_now_sources.json`.

## Source rotation

`data/external_buy_now_sources.json` currently contains 51 fixed-price sources across aggregators, dealer marketplaces, specialist photobook shops and antiquarian dealers.

Eight broad discovery sources are checked on every wider-web run:

- Alibris UK
- AbeBooks UK
- viaLibri
- Biblio
- ZVAB
- PBFA
- Boekwinkeltjes
- Maremagnum

The default wider-web batch is 24 sources. After the eight always-on sources, the remaining slots rotate through specialist and antiquarian dealers. Priority 1 sources target roughly daily coverage, priority 2 roughly every three days, and priority 3 roughly weekly coverage.

`data/external_buy_now_rotation.json` stores `last_checked` and `check_count` for sources actually searched. Failed or skipped sources should not be marked checked.

`external_source_rotation.py` can print the next deterministic source batch or mark source IDs as checked.

## Existing GitHub coverage excluded from this pool

Routine Oxfam, Shelter, Crisis, World of Books, Awesome Books, selected charity eBay sellers, The Photographers' Gallery, Photobookstore, Village Books and Setanta Books are already handled elsewhere in the repository and should not be redundantly searched in the wider-web layer unless there is evidence a listing escaped the normal monitor.
