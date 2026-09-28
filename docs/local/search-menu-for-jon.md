# Photobook Radar search menu

**All 12 searches are selected on Jon’s Mac Studio (28 September 2026).** The first visit to each route records existing stock as a quiet baseline. Items known to have appeared since the fresh-start time, and later new or materially changed listings, can produce alerts. Switches live in the owner-only local `config.toml` under `[sources]`; set a lane to `false` and restart the worker to stop its future checks. Saved history stays in Archive.

| # | Search | What it checks | Cadence and breadth |
|---|---|---|---|
| 1 | Oxfam Photography | Oxfam’s photography category by stable item ID. | Newest 60 every 10 minutes; daily full sweep. |
| 2 | Oxfam wider books | Books placed in Oxfam’s wider Art & Photography area. | Two newest pages of 90 every 10 minutes. |
| 3 | Shelter and Crisis | Four charity-shop book collections, including rare and secondhand. | Every 10 minutes; paginated product feeds. |
| 4 | Specialist shops | The Photographers’ Gallery, Photobookstore, Village Books and Setanta. | Hourly; paginated product feeds. |
| 5 | eBay broad | UK Books searches for `photobook` and `photography book`. | Hourly; first 200 results of each search. |
| 6 | eBay private sellers | Rotating names, titles, wrong-category, signed and broad individual-seller searches. | Every 15 minutes; about 24 distinct first-page searches per cycle, up to 200 results each. Discovery up to £750; researched book alerts at £200 all-in or less. |
| 7 | eBay charity sellers | Rotating named charity and library sellers in the UK and US. | Hourly; 12 sellers from 103, up to five pages per seller when needed. |
| 8 | eBay Endgame | Auctions ending within 72 hours across 16 eBay markets. | Every 15 minutes; takes the Browse allowance left after private-seller searches and other checks, up to 35 first-page searches per cycle and two pages per route. |
| 9 | AbeBooks targets | Rotating exact book titles from the reference library. | 24 searches every 15 minutes, about 96 per hour; the 628-title rotation takes about 6½ hours. |
| 10 | Wider web | Biblio, viaLibri, ZVAB, PBFA and Catawiki leads, including unfamiliar photographers. | One focused GPT‑6 Luna sweep every 30 minutes, rotating sites; each site is revisited about every 2½ hours. |
| 11 | Publishers | Direct feeds from MACK/SPBH, STANLEY/BARKER, TBW, Loose Joints, RRB, Deadbeat, GOST and Setanta; official-page sweep for Nazraeli and VOID. | Product feeds every six hours; one bounded GPT‑6 Luna sweep daily. |
| 12 | Photography prizes | Official book award announcements and shortlists. | One bounded GPT‑6 Luna sweep daily. |

**Alerts:** GPT‑6 Sol researches a promising book before Telegram sends it. 💎/🦄 flip alerts need at least 40% below checked comparable prices and £50 indicative net resale room; 📚⭐ collection priorities need a curated Tier 1 work at least 20% below a checked sale, without a profit requirement. Both routes have a £200 all-in limit. The 175 photographers in three tiers, Parr/Badger’s three volumes and Roth’s *101 Books* remain recognition clues; unfamiliar books remain eligible for flip alerts. Tier and bibliography badges appear only when supported.

**Shared eBay allowance:** The account’s live Browse limit is 5,000 requests per provider window. Every local Browse attempt is recorded. The scheduler protects the last 25 calls for live checks, gives the private-seller lane its quarter-hour batch, then paces Endgame against the provider’s remaining quota. Endgame also retains its 3,600-call hard cap. The dashboard’s System page shows source health. Seven old GitHub scan schedules were removed at cutover; their manual triggers remain available. GitHub stays as source control, while the Mac is the scanner.
