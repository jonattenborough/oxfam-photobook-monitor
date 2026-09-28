# Photobook Radar live cutover — 28 September 2026

**State at 08:59 UTC:** Production is running on Jon’s Mac Studio. The web and worker LaunchAgents are healthy; the dashboard responds in production mode. All 12 selected search lanes have recorded at least one successful live fetch. This is an initial acceptance snapshot, not a completed 24-hour soak or a reboot recovery test.

| Lane | First live evidence | Current status at snapshot |
|---|---|---|
| Oxfam Photography | Live pages captured from the photography category | Baselining a daily full sweep |
| Oxfam wider books | Two newest Art & Photography pages captured | Partial by design: newest frontier only |
| Shelter and Crisis | Live product pages captured; Unicode handles now URL encoded | Active |
| Specialist shops | Live product pages captured | Baselining paginated feeds |
| eBay broad | UK Books searches returned listings | Partial by design: first 200 per query |
| eBay private | Live Browse requests returned listings | Active |
| eBay charity | Live seller requests returned listings | Baselining paginated seller results |
| eBay Endgame | Live auction requests succeeded across the scheduled matrix | Partial where bounded pages exist |
| AbeBooks targets | Live exact-title pages parsed | Active |
| Wider web | GPT‑6 Luna returned three source-page checked items | Active |
| Publishers | Eight direct product feeds and a GPT‑6 Luna official-page sweep worked | Baselining feeds; sweep active |
| Photography prizes | GPT‑6 Luna returned two validated official pages | Active |

**Quota:** The eBay account reports a 5,000-call Browse window resetting 29 September at 07:00 UTC. At the snapshot the local ledger had consumed 81 Browse attempts, with 4,352 provider calls conservatively remaining and none uncertain. A 650-call reserve and a 3,600/day Endgame cap are enforced. The remote `main` branch has no scheduled scanner workflows. Three runs already queued before the merge later committed state, creating a short overlap; GitHub subsequently showed zero queued and zero in-progress runs. The gateway will take the lower of the next provider reading and its local remaining balance.

**Telegram:** Jon previously confirmed the setup message on his phone. Five early generic photography-book leads were provider accepted during the first broad eBay scan. This revealed a low-signal rule; the live worker was changed so phone leads require a recognized book or named photographer. Generic candidates remain in the local database for exploration. Three of those lead research jobs found no authoritative supporting page and correctly sent no researched update. A fresh qualifying post-fix book alert has not yet occurred, so the revised automatic format has not been confirmed on a new live result.

**Verification:** The full local suite passed 340 tests after the live fixes. A production database backup at 08:58 UTC passed integrity, and an isolated restore passed integrity with 102,804 listings, 135,552 observations, 94,994 jobs and five notification events. The service restarted successfully after the live fixes; reboot, logout, offline recovery and 24-hour quota pacing remain to be observed.

**09:15 UTC follow-up:** A French Endgame category task returned HTTP 400 because the UK category ID is not valid there. The scanner now uses localized book queries for category tasks outside the UK. A metered French probe returned 100 items; the failed route completed on retry and Endgame returned to `ACTIVE`. The regression suite now passes 341 tests. By 09:12 UTC Telegram had provider-accepted seven book alerts, including recognized Henri Cartier-Bresson and Ed Ruscha leads after the phone filter correction.

**09:40 UTC collector-policy correction:** Jon reported that the alerts were mostly irrelevant and required research before sending. Delivery was paused while the worker was changed. The prior outbox had 11 accepted fast leads and one accepted research update; several were books that merely mentioned a target in their description. A seller-title object check now rejects those matches. A new researched-find event needs a current exact eBay check, source-backed Codex collector assessment, a concrete edition or special-copy clue, and a PAY_ATTENTION/GEM/UNICORN verdict. The delivery worker independently rechecks the review and live copy. Publisher and sweep discoveries remain in the dashboard. The full suite passes 345 tests. The production worker restarted with the new gate and Telegram delivery re-enabled; no new alert was queued at restart. Today's 16 research jobs were already used under the old policy, so a live model-generated alert with the revised schema cannot be verified until the next UTC daily reset. The 12 source switches remain enabled.

**11:20 UTC operational check:** The worker heartbeat and new captures confirmed ongoing scanning. No revised Telegram alert had been queued; four lead reviews were deferred to the next UTC research allowance. Belgian Endgame routes were failing because the eBay API returns `www.benl.ebay.be` while the local host map expected an incorrect Belgian domain. The map was corrected, the full suite passed 346 tests, and the Mac worker restarted. Belgian routes began completing and Endgame returned to `ACTIVE` without a source error. Previously failed route snapshots remain in the ledger; newer pending route jobs are recovering coverage.
