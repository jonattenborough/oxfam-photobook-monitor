# Fixed-price Chat review catch-up policy

User-authorised on 24 September 2026 to increase review throughput and resume the existing eBay Fixed Price Review task after missed bargains in the backlog.

## Scope and precedence

This document applies only to the task named `eBay Fixed Price Review`. Read it together with `docs/ebay-gem-review-policy.md`. It replaces that document's six-issue / 60-listing non-Endgame allowance for this task only. It does not change any other review task, eBay API quota, discovery schedule, spending limit, comparable-evidence requirement, or permission requirement.

Process only open `EBAY_PRIVATE_NEW:` and `CHARITY_NEW:` issues without an owner-authored `CHATGPT_GEM_REVIEWED:` completion comment. Exclude Endgame, EXTERNAL_NEW, historical scans, backfills, catalogue dumps and audit issues. Keep the task in normal Chat, not Work, with no email. Do not buy books, contact sellers, or place offers automatically.

## Higher capacity without false completion

- The ceiling is **30 fully reviewed issues or 300 unique listings per run**, whichever is reached first. This is a permitted maximum, not a guaranteed throughput or a quota to fill with superficial reviews.
- Work in small blocks of at most three issues. Finish and persist each individual issue before expanding the next block. Continue beyond six issues when capacity allows.
- Spend little time on unequivocal unrelated objects, namesakes, instructional manuals, ordinary over-budget stock and unchanged exact-ID duplicates. Preserve underdescribed and unfamiliar books with credible photographic, collecting or value potential.
- Research every plausible candidate sufficiently to support its verdict. If the next issue cannot be completed properly, leave it open. Use the remaining execution capacity to persist completed work and the FINISH ledger, not to begin more research.
- A supported INVESTIGATE verdict may be a completed appraisal under the base policy when the attempts, evidence and unresolved questions are recorded. An unexamined listing is never a completed appraisal.
- Previewing candidate summaries is queue selection, not a completed review. Do not count skimmed titles as reviewed or bulk-close issues to make the backlog look smaller.

## Selection: fresh bargains and old overlooked books

Read `data/photobook_review_health.json` and `data/photobook_review_handoff.json`. Treat their timestamps as evidence: a stale snapshot is not today's queue count. Use fresh, complete handoff pages for bounded selection. If the handoff is unavailable, incomplete or older than 90 minutes, fall back to direct paginated GitHub issue reads.

Before selecting review blocks, inspect compact summaries across fresh cheap batches, older waiting batches and a rotating portion of the intervening queue. A stale handoff may identify where to start looking, but must not establish current eligibility or claim complete coverage. Re-fetch every selected issue and its comments regardless of the selection source.

Selection requirements:

1. Examine fresh affordable candidates early, normally reserving at least two issue slots when suitable fresh batches exist. A newly created high-numbered REVIEW packet is not necessarily the most useful fresh packet; the same scan may have created cheaper HOT packets first.
2. Reserve at least one slot for the oldest still-unreviewed affordable private batch. Include an affordable charity batch whenever one is waiting.
3. Re-rank older candidates using the current `data/ebay_endgame_targets.json` Core 175 names and tiers, independent of old recognition scores and exact-title library membership. A visibly credited Paul Reas book, for example, remains a Core photographer lead even if its old packet calls it unrecognised. A grouped search term alone is not proof of attribution.
4. Select strong cheap Core leads from across the waiting queue, not only from its oldest page. Use bounded title/description searches or rotating compact pages for this purpose. Preserve room for genuinely promising unknown photographers and badly catalogued books.
5. Prioritise worthwhile item prices under GBP50, then under GBP100, then under GBP150, with value, book significance, condition, evidence and urgency deciding between candidates. Cheapness or a famous name alone is not sufficient.
6. An unchanged repeat detection of the same listing ID is not a fresh listing or new evidence of value. Deduplicate it against recent completed comments and within the run. Preserve its original detection date where known. Re-report only a material price, evidence or urgency change.
7. Record which selection pages or search ranges were actually inspected and a next selection cursor, when applicable, in optional FINISH JSON fields. Never imply the entire backlog was screened unless it was. Subsequent runs should advance that cursor rather than repeatedly skimming the same middle pages; fresh and oldest fairness checks still apply every run.

## Availability and evidence

Apply all exact-edition, comparable-evidence, Core flag, GBP150 item-price, separate-postage and value-first presentation rules in the base policy.

- BUY NOW and MAKE OFFER require fresh evidence that the exact listing is currently purchasable. Do not use 'BUY NOW IF STILL LIVE'.
- A listing confirmed sold, ended, removed or unavailable is PASS, not a buying lead.
- If a technical problem prevents live verification but a plausible under-budget opportunity remains, use INVESTIGATE and prominently state `LIVE STATUS NOT VERIFIED - CHECK BEFORE BUYING`. Record the observed price as historical, not current.
- For every surfaced listing, show its detection or observation date when known and the actual time of the new availability check or failed check. Distinguish first detection, repeat detection and verification time.
- Postage, binding, printing, signature, inserts, dust jacket and other completeness details remain unresolved unless supported. No extrapolation from an ISBN alone to first printing or from an expensive signed/deluxe comparable to an ordinary copy.
- Retain strong concise leads beyond the three main cards when justified. Do not pad reports with speculative objects whose only attraction is missing information.

## API and execution safeguards

Increasing this Chat review allowance does not authorise more eBay Browse discovery or verification calls. Work from existing GitHub packets and normal web research. Do not launch additional eBay API scans, manual backfills, parallel discovery workers, paid API services or API-billed AI fallbacks merely to fill the higher review ceiling. Keep the existing eBay API caps and 650-call reserve unchanged.

For context only, the configured ordinary daily Browse plan at the time of this change is 3600 Endgame + 408 private discovery + approximately 288 charity searches at one page each + 48 wider-market searches = approximately 4344 calls. Against the standard 5000 allowance, the nominal 656 remainder already includes the 650-call reserve. Charity pagination and other real usage can change the balance. These are planning figures, not a live account quota measurement. Never treat the 656 as an additional freely spendable budget.

No numeric maximum on reliably completed Chat reviews has been established by this configuration change. Runtime, available tools and evidence complexity can make actual completion lower than 30 issues. Use receipts to measure completed work, not the task's last-run timestamp or this ceiling.

## GitHub persistence and ledger

Use the supported GitHub connector actions. `add_comment_to_issue` uses a parameter named `pr_number` even for issue comments. Supply the actual issue number. This is the normal issue-comment action, not permission to bypass a tool rejection.

Before reading candidate issue bodies, write `CHATGPT_REVIEW_RUN_START:` followed by one-line valid JSON on issue #2145. Include `run_id`, `task`, `started_at` and `scope`. Set task to exactly `eBay Fixed Price Review`, started_at to actual current UTC ISO time, and run_id to `eBay Fixed Price Review|<started_at>`. Scope should identify the 30-issue / 300-unique-listing fixed-price catch-up ceiling. Keep the same run_id throughout.

After fully reviewing an issue, add one owner-authored `CHATGPT_GEM_REVIEWED:` comment with a concise supported verdict for every listing and this run_id. Close the issue, then re-fetch the issue and comment to verify both writes. Count it complete only after verification. If a write response is ambiguous, read first before retrying so markers are not duplicated. Leave partially reviewed issues open without a completion marker.

Always attempt the FINISH ledger even after partial failure. Write `CHATGPT_REVIEW_RUN_FINISH:` followed by one-line valid JSON to #2145 using the same run_id, task and started_at. Required fields are `finished_at` with actual UTC ISO time, `status` as COMPLETED, PARTIAL or BLOCKED, `completed_issue_numbers`, `unique_listings_reviewed`, `reportable_listing_count`, and `errors`. Count only work actually done and verified. Optional fields may include `issues_selected`, `selection_pages_read`, `next_selection_cursor`, `duplicate_listing_count` and `live_verification_failures`. Re-fetch the FINISH comment to verify it when the connector allows.

If START cannot be written, do not read candidate issues or claim completion. Attempt a truthful BLOCKED FINISH if possible; if both writes fail, report that in Chat. Respect safety denials. Do not disable the recurring task merely because a single run is blocked or partial; report the specific problem and leave any scheduling change to an explicit user request.

`data/photobook_reviewer_receipt.json` remains independently generated by the health workflow. Do not fabricate or directly maintain it. A stale health receipt must be reported as stale; a successful configuration write does not prove a future review run completed.

## Notifications

Notify Jon in normal Chat only for BUY NOW, MAKE OFFER, an important WATCH, a plausible under-budget INVESTIGATE, or a blocked run, under the base policy. Successful all-PASS work remains silent after GitHub housekeeping and the ledger. Keep processing counts in a short footer after any buying conclusions. State a lower actual count plainly when the run finishes below the ceiling.
