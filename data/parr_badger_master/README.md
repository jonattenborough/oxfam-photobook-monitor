# Parr / Badger operational master

This directory contains the sharded operational lookup database used by the photobook monitors.

## Coverage

- 628 search records across Volumes I, II and III
- 526 CORE records
- 102 BROAD or provisional records

The four CSV files are shards of one logical database. `parr_badger_runner.py` loads every `*.csv` file in this directory at runtime.

## Fields

- `Volumes`: Parr / Badger volume number
- `Contributor`: photographer, author or other credited contributor
- `Title`: photobook title
- `Year`: publication year when established in the working database
- `Publisher`: publisher or publication statement when established
- `PB page / refs`: Parr / Badger page or index references when available
- `Best confidence`: current evidence confidence
- `Search tier`: `CORE` or `BROAD`

## Search behaviour

A Parr / Badger match is a discovery signal, not a purchase verdict. The matcher normalizes punctuation and accents and performs contributor-aware fuzzy title matching. Short generic titles require contributor evidence. BROAD records require stronger matching than CORE records.

`parr_badger_runner.py` layers this matching onto newly detected listings from Oxfam, Shelter, Crisis and the existing external charity / used-book radar.

`market_monitor.py` also uses the same master for the wider hourly search. It scans broad marketplace and specialist-photobook inventory, then rotates direct searches for master records across eBay UK, AbeBooks and Biblio. Per-query baselines prevent existing marketplace stock from being misreported as new.

Neither path reprocesses historical inventory or full-scan candidate pools as new listings.

## Source quality

Volume II is currently the strongest section because it was extracted from the book's technical description text. Volume I is based on the uploaded index and remains partly provisional. Volume III is based on public-source evidence and remains incomplete until the Volume III index or searchable primary text is obtained.

## Maintenance

Update the master when better primary-source information becomes available. Keep uncertain records marked BROAD rather than silently promoting them to CORE. The scheduled workflows run matcher self-tests so an unexpectedly missing or very small master fails visibly.
