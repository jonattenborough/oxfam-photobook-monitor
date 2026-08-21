# Oxfam Photobook Monitor

A small GitHub Actions monitor for Oxfam UK's **Art & Photography Books** category.

## What it does

- Queries Oxfam's public Oracle Commerce storefront search endpoint.
- Verifies results are sorted newest first by `product.creationDate`.
- Checks the newest 30 listings every 10 minutes.
- Stores a persistent set of seen `HD_...` SKUs in `data/state.json`.
- Creates a GitHub issue titled `OXFAM_NEW:` only when previously unseen SKUs appear.
- Attempts to enrich new SKUs through Oracle Commerce's product endpoint.
- Does not send purchase alerts itself. The associated ChatGPT scheduled task reviews the issue, researches editions and market value, and emails only genuinely noteworthy finds.

## Why this architecture

Oxfam's normal category page is protected by bot-management rules, while the storefront itself exposes structured catalogue data through Oracle Commerce. Monitoring stable SKU IDs is more reliable than comparing rendered page text.

## Schedule

The workflow runs at minutes 3, 13, 23, 33, 43 and 53 of every hour. GitHub scheduled jobs can occasionally start late, so this should be treated as approximately every 10 minutes rather than a hard real-time guarantee.

## State safety

The repository is seeded with the 30 SKUs visible in the user's captured Oxfam JSON on 21 August 2026. This prevents the first live run from announcing the existing page as 30 new products. Baseline entries are silently hydrated with current fingerprints on the first successful live request.

## Manual test

Use **Actions → Oxfam photobook monitor → Run workflow**. A successful run prints the first ten SKUs it parsed. If Oxfam blocks GitHub-hosted runners, the workflow fails before changing the state file, so no listings are lost.
