# Data migration and reconciliation

Source checkout: `692dc69c2a1352c750e1d69b394abd425d15a9b5`. Private GitHub export: 35 complete Issue pages and 23 complete comment pages, each verified against a saved hash. Import occurred on 28 September 2026 UTC. These are historical observations, not a statement that any seller still has stock.

| Input | Raw / references | Mapped observations | Explicit gap | Non-listing |
|---|---:|---:|---:|---:|
| 101 checked-out repository JSON/JSON.gz files | 111,807 | 82,020 | 29,751 | 36 |
| GitHub Issue candidate sections, including supplemental Issue types | 36,320 | 36,320 | 0 | n/a |
| GitHub Issue containers, pull requests and comments | 5,714 | 0 | 0 | 5,714 |
| **Total import objects** | **153,841** | **118,340** | **29,751** | **5,750** |

The database contains **94,685 unique listings**, **118,340 immutable observations**, **4,343 normalised book records**, **64 retained search windows**, **3,451 exported Issues**, **44 excluded pull requests**, and **2,219 exported comments**. The Core 175 target file has exactly 50/70/55 names in tiers 1/2/3. Of 1,412 comments bearing a review marker, 1,591 per-listing historical claims could be linked; 1,138 marked comments could not be safely linked to a specific imported listing. These are claims from old comments, never fresh appraisals.

Most gaps are **29,257 ID-only records in `data/market_state.json`**. They lacked a title or other full listing payload. Of 14,372 ID-only eBay references, 682 were linked to an already imported listing identity, but retain quarantine status because they still lack their own observation. Other gaps include old queue summaries and malformed or missing listing data. No raw rows were silently dropped.

A repeat import of the complete GitHub snapshot, uncovered Issue sections and historical reviews left listings, observations, review claims, decisions and outbox counts unchanged: 94,685 / 118,340 / 1,591 / 0 / 0. Repository file import uses per-file origin hashes and per-row checkpoints; an interrupted-file recovery test is still required. The original import report is private at `~/Library/Application Support/Photobook Radar/last-import-report.json`.
