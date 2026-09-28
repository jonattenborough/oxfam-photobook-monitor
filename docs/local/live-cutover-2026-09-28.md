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
