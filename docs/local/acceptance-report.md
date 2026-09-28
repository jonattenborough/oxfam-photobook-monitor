> This report is the pre-cutover shadow snapshot. For the current production state and first live results, see [live-cutover-2026-09-28.md](live-cutover-2026-09-28.md).

# Photobook Radar commissioning report

**Release / commit:** Local preview `0.1.0` on branch `build/photobook-radar-local`; base `692dc69c`
**Actual host and operating system:** Jon's Mac Studio, macOS 14.4.1 arm64, Python 3.13.1
**Report date and test interval:** 28 September 2026 UTC; offline import and tests only
**Production status:** NOT COMMISSIONED

This is an evidence record for a shadow preview. Jon chose to start fresh; retained historical listings are an archive only. The local service is installed, but live-source operation and phone delivery are not commissioned.

## 1. What is installed

Dashboard: `http://127.0.0.1:8765`; Desktop shortcut: `~/Desktop/Photobook Radar.webloc`. Runtime data: `~/Library/Application Support/Photobook Radar`. LaunchAgents: `com.jonattenborough.photobook-radar.web`, `.worker`, `.backup`. Research: lead mode, no AI provider. Phone: Telegram selected; bot valid, private chat selected, setup message ID 3 accepted and received by Jon. Outside heartbeat: not installed. Private backups: runtime `backups/`. Secrets are redacted.

## 2. Source-by-source state

| Source / route group | Adapter implemented | Fixture tests | Authorised live probe | Production enabled | Last successful evidence | Gap / restriction |
|---|---|---|---|---|---|---|
| Oxfam Photography and broad | Partial | Photography page, frontier/full-sweep scheduler fixtures; broad pending | NOT RUN | NO | Historical import | Photography scheduler production-gated, not probed; broad pending |
| Shelter/Crisis and specialist market | NO | Existing legacy tests only | NOT RUN | NO | Historical import | Local adapters pending |
| eBay private, charity, broad and Endgame | Partial | Metered request boundary, page/cursor, exact-check and broad-frontier fixtures | NOT RUN | NO | Historical import | Two broad routes and exact verification are production-gated; private/charity/Endgame routes and live quota telemetry pending |
| Wider Web, Publisher and Prize research | NO | NOT RUN | NOT RUN | NO | None | Legacy ChatGPT task state unverified |

## 3. Import reconciliation and fresh start

See `data-migration-report.md` and the private import manifests. Repository: 111,807 rows, 82,020 mapped, 29,751 quarantined, 36 non-listing. GitHub: 35 Issue pages, 23 comment pages, 36,320 mapped candidate references, 3,451 Issue containers, 44 excluded PRs, 2,219 comments. Total: 94,685 unique listing identities and 118,340 observations; 64 retained windows. Repeat GitHub import left counts unchanged. Artifact availability audit remains open. These identities are retained for reference and rediscovery matching; stale imported rows are excluded from Finds and Urgent. Fresh epoch began 2026-09-28T07:50:25Z; 29,822 historical screening jobs were cancelled without deleting the archive.

## 4. Acceptance evidence

Allowed status: NOT RUN, PASS, FAILED, BLOCKED, or NOT APPLICABLE with a specific reason. PASS requires an actual test result and evidence reference. Do not fill this table from a planned command or a future scheduled test.

| ID | Test | Status | Evidence / actual result |
|---|---|---|---|
| A01 | Import the same snapshot twice | PASS | Repeat GitHub import: listings 94,685, observations 118,340, reviews 1,591, decisions 0, outbox 0 before and after; private repeat reports. |
| A02 | Interrupt import midway | NOT RUN | Required: Resume from checkpoint and reconcile every input row. |
| A03 | More than 1,000 Issues and multiple comment pages | PASS | Complete hashed export consumed 35 Issue pages and 23 comment pages: 3,451 Issues, 44 PRs, 2,219 comments. |
| A04 | Unparseable legacy row or expired artifact | NOT RUN | Partial: 29,751 rows explicitly QUARANTINED; expired artifact scenario not exercised. |
| A05 | Closed Issue without trustworthy per-listing evidence | NOT RUN | Required: Not treated as a fresh completed appraisal. |
| A06 | One listing discovered by several queries/marketplaces | NOT RUN | Required: One listing with preserved aliases and provenance. |
| A07 | Same ISBN, different seller copies or variants | PASS | `test_different_seller_copies_stay_distinct` passes with the same ISBN and two item IDs. |
| A08 | Interrupted 350-result search | NOT RUN | Partial: eBay two-page fixture commits cursor with pages and prevents replay regression; 350-result restart not run. |
| A09 | Dense/unsplittable search window | NOT RUN | Required: Correct split or explicit incompleteness; no false coverage claim. |
| A10 | One source blocks or returns invalid HTML | NOT RUN | Required: Other sources continue; affected source becomes degraded. |
| A11 | HTTP 200 CAPTCHA or invalid zero-result parse | NOT RUN | Required: Not accepted as a successful empty inventory baseline. |
| A12 | Existing stock imported at startup | PASS | `test_imported_stock_is_silent` passes; 118,340 imported observations produced zero outbox events. |
| A13 | Unknown photographer, weak title, meaningful photobook clues | NOT RUN | Required: Retained, visible and eligible for protected exploration work. |
| A14 | Cheap Paul Reas or another current Core name with an old weak score | NOT RUN | Required: Current attribution can upgrade priority without rewriting history. |
| A15 | Oldest affordable/charity work under constant fresh arrivals | NOT RUN | Required: Receives its configured fair share; no indefinite starvation. |
| A16 | Search ceiling GBP 750 and recommendation ceiling GBP 150 | NOT RUN | Partial: GBP 151 recommendation blocked in unit test; GBP 750 search ceiling not exercised. |
| A17 | Ordinary famous/signed book at ordinary price | NOT RUN | Required: No invented bargain valuation or urgent class merely from fame. |
| A18 | Exact live check fails on a plausible cheap lead | NOT RUN | Required: INVESTIGATE with old observation and warning, not fabricated active status or automatic PASS. |
| A19 | Confirmed ended/sold listing | NOT RUN | Partial: past-end auction receives ENDED and no verdict; sold-state check not exercised. |
| A20 | Currency changes or shipping is unknown | NOT RUN | Required: Stale converted totals cleared; no made-up free UK shipping. |
| A21 | Unsigned/reprint evidence compared with signed first edition | NOT RUN | Required: Validator blocks unqualified like-for-like valuation. |
| A22 | HTTP retry consumes additional request | NOT RUN | Partial: offline transport test records timeout and next attempt separately; all scanner routes not yet using gateway. |
| A23 | Two concurrent workers at last available request | NOT RUN | Partial: separate SQLite connections cannot reserve beyond protected limit; concurrent worker race not yet run. |
| A24 | Crash after quota reservation or request timeout | NOT RUN | Partial: timeout retains uncertain usage; process crash scenario not run. |
| A25 | Restart during a provider quota window | NOT RUN | Required: Counters and reset time preserved; no full fresh allowance. |
| A26 | API/auth failure | NOT RUN | Required: Bounded retry and visible pause; no tight loop or unrelated source outage. |
| A27 | Double-click Run now / Refresh | NOT RUN | Required: One eligible bounded job; no duplicated API storm. |
| A28 | Slow legacy network call or AI task | NOT RUN | Required: Scheduler and urgent outbox continue within local objectives. |
| A29 | Worker dies while holding a job lease | NOT RUN | Partial: expired lease reclaimed in unit test; actual worker process death not exercised. |
| A30 | Old worker finishes after lease reassignment | PASS | Same test confirms the old fencing token cannot complete the reassigned job. |
| A31 | Crash after capture transaction | NOT RUN | Partial: bad page rolls back observations and cursor; forced process crash not exercised. |
| A32 | Crash after notification outbox commit, before send | NOT RUN | Required: Event later delivered without being lost. |
| A33 | Provider accepts but response is lost | NOT RUN | Partial: offline `DELIVERY_UNKNOWN` scenario gets one bounded retry; actual provider acceptance with lost response and receipt reconciliation not run. |
| A34 | Restart with an expired urgent auction alert | NOT RUN | Partial: expired event suppressed; restart scenario not exercised. |
| A35 | New promising auction with little time remaining | NOT RUN | Required: Fast lead alert independent of a slow appraisal; exact uncertainty visible. |
| A36 | User dismisses or auction ends during emergency repeats | NOT RUN | Required: Repeats cancelled where provider supports it; no endless reminders. |
| A37 | AI provider missing, exhausted, malformed or timed out | NOT RUN | Required: Leads/scans/alerts continue; research is visibly degraded and uncompleted. |
| A38 | Seller prompt injection or model asks for secrets/commands | NOT RUN | Required: No instruction, credential, filesystem or operational authority escalation. |
| A39 | Malicious HTML, URL, redirect or thumbnail | NOT RUN | Required: Escaped/blocked; no XSS, local-network fetch or credential leakage. |
| A40 | Unauthenticated or cross-origin state-changing request | NOT RUN | Required: Rejected; legitimate app controls work. |
| A41 | Dashboard open in multiple tabs / web workers | NOT RUN | Required: No extra scheduler, duplicate source loop or implicit scan. |
| A42 | Clock jump, London DST change and sleep/restart | NOT RUN | Required: Correct UTC deadlines, no duplicated scheduled events or lost windows. |
| A43 | Service crash, screen lock, logout and reboot | NOT RUN | Required: Actual supported recovery behaviour demonstrated and limitations documented. |
| A44 | Network outage then recovery | NOT RUN | Required: No lost state, no quota catch-up storm, stale notifications re-evaluated. |
| A45 | Disk full, database lock or schema mismatch | NOT RUN | Required: Safe failure/degradation; no silent empty database or corrupted progress. |
| A46 | Backup and isolated restore | NOT RUN | Partial: 28 Sep 07:09 UTC backup passed integrity; isolated restore passed integrity with 94,685 listings, 118,340 observations, 94,685 jobs, zero outbox and decisions, 64 windows; state/decision mutation round-trip still pending. |
| A47 | Home computer/offline heartbeat failure | NOT RUN | Required: Outside monitor notices when enabled; recovery signal tested. |
| A48 | Phone push and direct link over mobile data | NOT RUN | Required: Received by Jon/test device; seller link works independently of local dashboard. |
| A49 | Remote dashboard disabled or private connection unavailable | NOT RUN | Required: Push still has usable seller URL; no localhost-only primary action. |
| A50 | Cutover or rollback rehearsal | NOT RUN | Required: One production scanner owner; no duplicate credentials consumers or notification storm. |
| A51 | Retired manual/backfill jobs discovered during import | NOT RUN | Required: Imported without silently re-enabling recurring discovery. |
| A52 | Source listed but its adapter/provider is disabled | PASS | Dashboard fixture renders unmonitored source lanes, disabled AI provider and shadow-mode warning; historical backfill IDs are kept out of the monitoring table. |

## 5. Performance and recall checks

Actual host and counts above. Full 50k/250k/10k load test, p95 query timing, recall corpus and memory measurements are NOT RUN. The first UI pass exposed query-text attribution false positives; triage now ignores inherited search context and the sample regression cases pass. Fresh observations are screened without repeating identical leads. Jon chose a fresh start, so the historical screening backlog is cancelled rather than processed.

## 6. Live delivery evidence

Telegram `getMe` validated the bot token. Setup message ID 3 was provider accepted and Jon confirmed receipt on his phone. No automatic book alert has been sent. Direct seller-link test on mobile data remains NOT RUN. Emergency repeats remain disabled.

## 7. Costs and quotas

No local eBay Browse/OAuth call was made; local quota telemetry and live physical-attempt accounting are NOT RUN. The local research provider is `none`; paid API spend is zero. Jon chose his Codex allowance for later deeper assessments; the intended starting model is GPT-6 Sol, pending an isolated sample and explicit per-day guard. Telegram setup used a free bot request; no automatic notification was sent.

## 8. Failure and recovery evidence

Unit tests cover expired lease recovery, fencing, atomic page rollback, changed observation screening, quota reservations and expired outbox suppression. A consistent backup passed integrity and an isolated restore passed integrity with 94,685 listings and 118,340 observations at that backup point. Forced process failure, network loss, quota restart, disk fault, reboot, logout and FileVault unlock tests are NOT RUN. Per-user agents cannot operate before FileVault unlock.

## 9. Cutover record

No cutover approval has been requested. Existing GitHub workflows and ChatGPT tasks have not been changed. The old schedules' current enabled state has not been verified. Local mode remains shadow with outbound scanning and automatic phone delivery disabled.

## 10. Soak interval

Status: NOT RUN. Record actual elapsed start and end times, failures, source results and notification outcomes. A configured future 24-hour test is not a completed 24-hour test.

## 11. Outstanding gates and exact next action

Installed: local import/archive, matching worker, dashboard and backups. Implemented but not enabled: Telegram delivery, Oxfam Photography scheduled adapter, two hourly broad eBay routes, metered eBay request boundary and exact eBay listing verification. Built source switches are off by default while Jon selects lanes using `search-menu-for-jon.md`. Pending: remaining source schedules/adapters, live quota telemetry, non-eBay exact checks, research provider, full dashboard controls/filters, complete acceptance suite and soak. The service is a shadow lead-mode preview, **not commissioned**.

## 12. Simple instructions for Jon

See `operations-for-jon.md`. The Desktop shortcut opens the local dashboard. `INVESTIGATE` marks an unverified lead, not a purchase instruction. Run `./scripts/service status` or `./scripts/doctor` for redacted status; `./scripts/service restart` restarts local agents. Backups are private under application data. The Mac user session and FileVault volume must be available after reboot.
