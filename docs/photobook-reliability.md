# Reliability hardening, 18 September 2026

## This change

- Private searches retain a frozen start/end window and pagination cursor. A query's completed-through watermark advances only when that window drains. Up to four of the existing 17 logical calls are reserved for old continuations; no extra per-run or daily allowance is introduced.
- Endgame retains unfinished windows between runs and follows returned next links rather than restarting every dense query. Searches near the 10,000-result ceiling split time windows. An unsplittable dense window remains explicitly incomplete.
- Partial results survive a later page failure. A failed discovery attempt does not become a successful-discovery timestamp. Deadline-only runs retain the previous discovery statistics.
- Unknown postage stays unknown. A currency change cannot retain an old GBP delivered total.
- The new half-hourly health workflow indexes every open supported issue through the paginated list endpoint, including more than 1,000 results, and checks paginated comments for an owner-authored completion marker. It never closes, comments on, buys, bids, or discards anything.
- Health distinguishes successful discovery, recorded review completion, waiting batches, elapsed auction deadlines, and pending search windows. An automation trigger is not proof of work.

## Reader handoff

The existing normal-Chat reviewer should read `data/photobook_review_health.json` first and use `data/photobook_review_index.json` to inspect older and newer batch summaries before choosing work. An index older than 60 minutes, an incomplete scan, or any technical read failure requires a fresh paginated issue listing. Even with a fresh index, refetch a selected issue and its comments before reviewing or closing it.

The index contains discovery evidence only. Its observed prices, title matches and end times are not fresh eBay verification or exact-edition evidence. Keep the established Core 175, unknown-photographer recall, budget, market-comparison, and cheap-batch fairness rules.

After a real review, write `CHATGPT_GEM_REVIEWED:` only when every listing has a supported verdict, then close the issue and verify the mutation. A partially reviewed issue stays open and has no completed marker. Connection failure must never be summarized as all listings passing or the queue being clear.

## Acceptance and limits

The baseline had 209 tests with two already failing hard-coded search-matrix assertions after Evidence aliases were added. These are replaced with uniqueness, budget and two-cycle coverage invariants, not removed. New tests cover a 350-result interrupted search, failed pages, dense overflow, duplicates, exact watermark behavior, bounded recovery calls, 1,005 GitHub issues, later comment pages, untrusted markers, stale reviews and unknown shipping.

The cursor is not an immutable eBay inventory snapshot. Listings can sell or end between requests. Retained windows and explicit incompleteness reduce avoidable loss; they do not prove 100 percent recall. Previously skipped historical windows cannot be reconstructed retroactively by this change.

This is the first tranche, not completion of the wider audit. It does not yet implement seller-group batching, getItems access, central physical-request accounting, namesake query isolation, an external scheduler, independent automatic valuation, or backlog clearance. GitHub schedule timing remains best effort. The health workflow itself must also be checked for staleness.

## Rollback

Revert the source/workflow commit. New cursor fields are additive; do not delete candidate pools or review issues. Reverting to the old code will no longer consume the new continuation fields, so keep them for a forward fix rather than claiming those windows completed.
