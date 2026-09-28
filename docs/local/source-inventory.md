# Source inventory

Checked-out commit: `692dc69c2a1352c750e1d69b394abd425d15a9b5`. Generated 2026-09-28T06:36:04Z.

The workflow files below show configured schedules, not verified GitHub activation. Existing GitHub scanning was not changed. Local status is shadow import only.

| Workflow | Configured schedule (UTC) | Responsibility | Entry point | State and output |
|---|---|---|---|---|
| `bhf-full-scan.yml` | Manual only | Historical/manual workflow | bhf_full_scan.py | see workflow; manual outputs |
| `catalogue-audit.yml` | Manual only | Historical/manual workflow | catalogue_audit.py; parent_full_scan.py | see workflow; manual outputs |
| `charity-full-scan.yml` | Manual only | Historical/manual workflow | charity_full_scan.py | see workflow; manual outputs |
| `ebay-endgame.yml` | 2/5 * * * * | eBay Endgame auctions | ebay_endgame.py; ebay_search_checkpoint.py | data/ebay_endgame_state.json; data/ebay_endgame_targets.json; ENDGAME_EARLY; ENDGAME_4H |
| `ebay-private-full-library-scan.yml` | Manual only | Historical/manual workflow | ebay_private_full_library_scan.py | see workflow; manual outputs |
| `ebay-private-international-accelerator.yml` | Manual only | Historical/manual workflow | ebay_private_international_backfill.py | see workflow; manual outputs |
| `ebay-private-seller-monitor.yml` | 4 * * * * | eBay private fixed price | ebay_private_recall_monitor.py; ebay_private_alert_builder.py; ebay_private_issue_sync.py | data/ebay_private_seller_state.json; data/ebay_private_recall_searches.json; EBAY_PRIVATE_NEW |
| `ebay-seller-monitor.yml` | 9 * * * * | eBay charity/library sellers | ebay_seller_monitor.py; ebay_charity_issue_sync.py | data/ebay_seller_state.json; data/ebay_sellers.json; CHARITY_NEW |
| `full-scan.yml` | Manual only | Historical/manual workflow | full_scan.py | see workflow; manual outputs |
| `initial-market-sweep.yml` | Manual only | Historical/manual workflow | initial_market_sweep.py; parr_badger_runner.py | see workflow; manual outputs |
| `market-monitor.yml` | 27 * * * * | Four specialist feeds, two broad eBay feeds, AbeBooks targets | market_monitor_safe.py; market_monitor.py; market_issue_batches.py | data/market_state.json; EXTERNAL_NEW |
| `monitor.yml` | 3,13,23,33,43,53 * * * * | Oxfam Photography; Shelter/Crisis | monitor.py; canon_runner.py; charity_monitor.py | data/state.json; data/charity_state.json; OXFAM_NEW; CHARITY_NEW |
| `parent-full-scan.yml` | Manual only | Historical/manual workflow | parent_full_scan.py | see workflow; manual outputs |
| `parent-monitor.yml` | 6,16,26,36,46,56 * * * * | Oxfam broad Art and Photography | parent_monitor.py; oxfam_parent_common.py | data/parent_state.json; OXFAM_ART_NEW |
| `photobook-review-health.yml` | 13,43 * * * * | Review health and handoff | photobook_review_health.py; photobook_review_handoff.py | data/photobook_review_index.json; data/photobook_review_handoff.json; reporting only |

## Last timestamps in retained state

These are file contents at the checked-out commit, not independent proof of present live health.

| File | Field | Value |
|---|---|---|
| `data/state.json` | `last_successful_fetch` | 2026-09-27T17:03:17Z |
| `data/parent_state.json` | `last_successful_fetch` | 2026-09-27T21:38:46Z |
| `data/charity_state.json` | `last_checked` | 2026-09-27T17:03:27Z |
| `data/market_state.json` | `last_successful_run` | 2026-09-28T00:54:43Z |
| `data/ebay_private_seller_state.json` | `last_run` | 2026-09-28T00:27:07Z |
| `data/ebay_seller_state.json` | `last_run` | 2026-09-28T00:36:29Z |
| `data/ebay_endgame_state.json` | `last_discovery_at` | 2026-09-28T01:35:28Z |
| `data/external_state.json` | `last_run` | none |

## Important configuration

- Core tiers: {'1': 50, '2': 70, '3': 55}; recognition library is loaded separately from checked-in sources.
- Endgame: 15-minute code interval, 3600 daily cap, 650 shared reserve; alert thresholds [2880, 240] minutes.
- Private eBay: 17 logical calls per run; GBP 750 discovery ceiling. This is not the GBP 150 recommendation cap.
- Market wrapper retains four specialist feeds and two broad eBay feeds; exact-target market is AbeBooks only (see `market_monitor_safe.py`).
- `external_monitor.py` exists, but `data/external_state.json` contains no active source result and no recurring workflow invokes it.

## Migration boundaries

- Manual full scans, forensic runs and backfills remain historical import sources only.
- GitHub Issues and comments were exported separately; workflow artifacts still require an availability audit.
- Wider Web, Publisher/Future Canon and Prize Watch were ChatGPT tasks in the handover. Their current task states are not verified here and remain unmigrated.
- Every data path and manual code file appears in `source-inventory.json`.
