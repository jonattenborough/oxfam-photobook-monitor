# Reliability hardening, 18 September 2026

## Deployed first tranche

PR #2125 was merged as d27bcaabd6c9521830b49eddb120fe08f6517309 after all three PR validation jobs passed. The complete offline suite contains 243 passing tests, including 34 new regression tests.

- Private searches retain a frozen start/end window and pagination cursor. A query's completed-through watermark advances only when that window drains. Up to four of the existing 17 logical calls are reserved for old continuations; no extra per-run or daily allowance is introduced.
- Endgame retains unfinished windows between runs and follows returned next links rather than restarting every dense query. Searches near the 10,000-result ceiling split time windows. An unsplittable dense window remains explicitly incomplete.
- Partial results survive a later page failure. A failed discovery attempt does not become a successful-discovery timestamp. Deadline-only runs retain the previous discovery statistics.
- Unknown postage stays unknown. A currency change cannot retain an old GBP delivered total.
- The new half-hourly health workflow indexes every open supported issue through the paginated list endpoint, including more than 1,000 results, and checks paginated comments for an owner-authored completion marker. It never closes, comments on, buys, bids, or discards anything.
- Health distinguishes successful discovery, recorded review completion, waiting batches, elapsed auction deadlines, and pending search windows. An automation trigger is not proof of work.

## Reader handoff

The existing hourly normal-Chat reviewer first fetches the complete approved policy in `docs/ebay-gem-review-policy.md`. Its spending cap, evidence standards, presentation, source fairness and review limits remain unchanged.

Read `data/photobook_review_health.json` and `data/photobook_review_handoff.json`. The handoff manifest points to bounded pages in `data/photobook_review_queue/`, including oldest-affordable and fresh issue references. Do not load the multi-megabyte `data/photobook_review_index.json` merely to select batches; it is the full internal index and comment cache.

A handoff older than 90 minutes, an incomplete scan, or a technical read failure requires fresh paginated issue reads. Check for Endgame issues created since the index timestamp even when the index is fresh. Refetch selected issues and comments before reviewing or closing them.

The index contains discovery evidence only. Its observed prices, title matches and end times are not fresh eBay verification or exact-edition evidence. Keep the Core 175, unknown-photographer recall, GBP150 item-price cap excluding postage, market-comparison and cheap-batch fairness rules.

After a real review, persist `CHATGPT_GEM_REVIEWED:` with a supported verdict for every listing, close that issue, and verify both writes before moving on. Partial issues remain open without completed markers. Connection failure must never be summarized as all listings passing or the queue being clear.

At run end, the reviewer writes `data/photobook_reviewer_receipt.json` with actual UTC start and finish, COMPLETED/PARTIAL/BLOCKED status, verified issue/comment references, partial issue references, listing counts and errors. COMPLETED means only that run's planned work, not the entire queue. The receipt supplements per-issue comments and must not claim work that did not happen. Failed or blocked execution is distinct from a successful no-finds review.

The automation update at 2026-09-18T06:05:41Z retained its existing hourly schedule and normal Chat/no-email instruction. This configuration change alone does not verify the next unattended run or alert delivery.

## Acceptance and limits

The baseline had 209 tests with two already failing hard-coded search-matrix assertions after Evidence aliases were added. These were replaced with uniqueness, budget and two-cycle coverage invariants, not removed. New tests cover a 350-result interrupted search, failed pages, dense overflow, duplicates, exact watermark behavior, bounded recovery calls, 1,005 GitHub issues, later comment pages, untrusted markers, stale reviews, unknown shipping and complete small-page handoff.

The bounded two-call live eBay canary succeeded at 2026-09-18T05:54:50Z without changing production eBay state. The first main-branch health/index run, 35313168658, succeeded and persisted the derived files. Its status correctly remained ATTENTION because discovery and review were stale and the backlog was unresolved.

The cursor is not an immutable eBay inventory snapshot. Listings can sell or end between requests. Retained windows and explicit incompleteness reduce avoidable loss; they do not prove 100 percent recall. Previously skipped historical windows cannot be reconstructed retroactively by this change.

This is the first tranche, not completion of the wider audit. It does not yet implement seller-group batching, getItems access, central physical-request accounting, namesake query isolation, an external scheduler, independent automatic valuation, or backlog clearance. GitHub schedule timing remains best effort. The health workflow itself must also be checked for staleness.

## Rollback

Revert the source/workflow commit. New cursor fields are additive; do not delete candidate pools or review issues. Reverting to the old code will no longer consume the new continuation fields, so keep them for a forward fix rather than claiming those windows completed.
