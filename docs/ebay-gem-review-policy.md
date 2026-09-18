Review new eBay photobook alert issues in the connected GitHub repository jonattenborough/oxfam-photobook-monitor. Process only open issues whose titles begin ENDGAME_4H:, ENDGAME_EARLY:, EBAY_PRIVATE_NEW:, or CHARITY_NEW:. Ignore historical scans, backfills, forensic audits, full-library scans, catalogue dumps, and all unrelated issue types.

Use this priority order:
1. ENDGAME_4H
2. ENDGAME_EARLY
3. EBAY_PRIVATE_NEW
4. CHARITY_NEW

Do not review an issue that already contains a comment beginning CHATGPT_GEM_REVIEWED:. Process every Endgame issue first, ordered by actual ending time. For non-Endgame work, inspect the open issue titles and candidate summaries BEFORE choosing batches; paginate beyond the newest results and inspect older waiting batches too. Never select simply by newest issue number: a scan creates its cheap HOT batches first and expensive REVIEW batches last.

Then review up to six non-Endgame issues or 60 non-Endgame listings per run. Rank batches containing plausible worthwhile books under £50 first, then under £100, then under £150, considering actual collectibility clues, Core 175 relevance, under-description and urgency. Do not let Core 175 membership displace a better unknown-photographer bargain. Reserve at least one of the six slots for the oldest still-unreviewed affordable batch, and include an affordable charity batch when one is waiting so that source is not starved. Ordinary over-budget batches must not displace affordable leads.

Use lightweight triage for obvious irrelevant objects, already-ended listings, clearly ordinary copies and ordinary over-budget stock; deep market research is for plausible opportunities, normally only the three strongest full cards. Deduplicate exact listing IDs across batches and recent review comments; only re-report when price, evidence or urgency materially changes. Every listing in a closed issue still needs a concise supported verdict. Leave partly reviewed issues open and without the completed marker, and do not close or discard a promising cheap listing merely to reduce the queue.

For every candidate that survives initial triage, freshly verify that the exact listing is still live on eBay or the charity's actual selling site. For auctions record the current bid, bid count, postage, currency, marketplace, exact ending time in Europe/London, and total likely delivered cost. For fixed-price listings confirm Buy It Now or Best Offer is still available. A technical verification failure must not silently suppress a promising candidate. Label it LIVE STATUS NOT VERIFIED - CHECK BEFORE BIDDING or BUYING.

Treat data/ebay_endgame_targets.json as the authoritative shared list of 175 core photographers. Give Tier 1, Tier 2, and Tier 3 matches extra scrutiny in that order across Endgame, private-seller, and charity issues, but never use the list as a whitelist. Continue to evaluate unknown photographers and broad unicorn discoveries. In private-seller issues, a grouped target search may have matched a seller description that is not present in the API summary. The listed possible target terms are leads, not proof. Open the listing and determine which photographer, if any, actually appears.

Identify the exact book as far as the evidence allows: photographer, title, publisher, year, edition, printing or impression, binding, jacket, signature, inscription, limitation, included print or ephemera, condition, and completeness. Distinguish true first editions from later printings, reprints, facsimiles, and ordinary later issues. Treat repository scores, target tiers, grouped query terms, and recognition matches as discovery evidence, not proof.

Research credible like-for-like active copies and useful recent sold evidence. Estimate a realistic market range. For auctions, give a maximum winning bid or hammer before postage that still represents good value, and separately show the estimated delivered total at that bid. Do not let one inflated asking price create a false bargain.

For ENDGAME_4H issues, favour speed and action. Give a concise BID NOW, WATCH CLOSELY, or PASS decision, confidence, decisive reason, maximum winning bid before postage, and estimated delivered total at that bid. For previously reported items, send one final reminder only for strong opportunities still worth pursuing. Recheck the earlier verdict and live price; suppress routine WATCH items, unchanged speculative leads, previous PASS items and overpriced copies. Report worthwhile late discoveries immediately.

For ENDGAME_EARLY issues, use the available lead time for a more careful exact-edition and market check. These usually arrive within roughly 48 hours of the end, but late discoveries must still be reviewed immediately. Classify each as BID, WATCH, INVESTIGATE, or PASS and record the maximum all-in bid for anything worth following.

For EBAY_PRIVATE_NEW and CHARITY_NEW candidates, classify each as BUY NOW, MAKE OFFER, WATCH, INVESTIGATE, or PASS. Put the strongest and most time-sensitive item first. Bias toward recall: surface a speculative lead when it could plausibly be a unicorn, but clearly separate confirmed evidence from uncertainty.

Notify Jon in normal Chat only when at least one listing merits BID NOW, BUY NOW, MAKE OFFER, an important WATCH, or a plausible under-budget INVESTIGATE. If all candidates are PASS, complete the GitHub housekeeping silently.

After fully reviewing an issue, add one GitHub comment beginning CHATGPT_GEM_REVIEWED: with the concise verdict for every listing, then close the issue. Do not add the marker or close an issue that was only partially reviewed. Keep this scheduled review in normal Chat, not Work.

FULL GEM CARD FOR EXCEPTIONAL FINDS

Use the detailed presentation below for genuine or plausible gems, normally the three strongest listings per run, but never suppress an additional genuine unicorn. Keep ordinary worthwhile leads concise. An overall BUY SCORE from 0% to 100% expresses recommendation strength for Jon at the actual price, considering identity confidence, condition, importance, scarcity, value, collection fit and urgency. It is not a prediction of investment return. Explain the two or three main drivers.

A lawful look-inside source may be Internet Archive/Open Library, Google Books, the publisher, a reputable dealer spread gallery, a museum or library, or a legitimate video flip-through. Do not bypass access controls; state registration or donation requirements where relevant. Label the best direct link FULL, EXTENSIVE, PARTIAL, VIDEO or NONE FOUND.

For urgent ENDGAME_4H finds, give the actionable verdict promptly. Do not suppress a plausible opportunity while waiting for one comparable, preview or bibliographic detail; label uncertainty clearly.

CORE 175 REPORT FLAG

For every listing surfaced to Jon, place one of these lines prominently beside the decision and direct link:

- CORE 175: YES - Tier 1 - [confirmed photographer name]
- CORE 175: YES - Tier 2 - [confirmed photographer name]
- CORE 175: YES - Tier 3 - [confirmed photographer name]
- CORE 175: POSSIBLE - Tier [number] - [suspected photographer name], not yet confirmed
- CORE 175: NO

Use data/ebay_endgame_targets.json as the authority for both membership and tier. Confirm the photographer from the actual listing title, description, photographs or reliable bibliographic evidence whenever possible. A photographer merely appearing among several grouped query terms is not proof. Use POSSIBLE when the grouped search matched but the listing evidence does not reveal which name caused the match. If a listing genuinely contains work by more than one Core 175 photographer, list every confirmed match and tier. Include this flag in both concise reports and Full Gem Cards.

REPORT PRESENTATION FORMAT

These presentation rules control the final report shown to Jon. Optimise for rapid scanning on desktop and mobile.

Do not begin with GitHub processing details, issue counts, comment markers, closed issues or queue housekeeping. Put any useful processing note in one short footer at the very end. Begin immediately with the buying conclusions.

Use this exact overall structure whenever there is at least one reportable listing:

# eBay Photobook Finds

## At a glance

Start with a compact Markdown table sorted by urgency and recommendation strength:

| Item | Core 175 | Decision | Current price | Likely real value | Potential bargain | Ends | Link |
|---|---|---|---:|---:|---|---|---|

Requirements:
- Keep item names short but identify photographer and title.
- Show Core 175 as T1, T2, T3, Possible, or No.
- Use only these decisions: BID NOW, BUY NOW, MAKE OFFER, WATCH, INVESTIGATE.
- In Current price, show the present item price or auction bid first, followed by postage in parentheses. If postage is unknown, write plus unknown postage.
- In Likely real value, show a conservative fair-market range for the exact edition, printing, condition, signature status and completeness. Use the seller's currency and approximate GBP where useful.
- In Potential bargain, show the estimated cash difference and percentage below the conservative market range at the current price. For an auction make clear that this is the gap at the current bid, not a prediction of its final price.
- Do not put a suggested bid, offer or maximum in the At a glance table. Keep that advice inside the detailed Gem Card.
- Show ending times in Europe/London using a clear form such as Thu 17 Sep, 1:26pm.
- Make Link a short clickable link labelled View.
- Use clean direct URLs without tracking parameters when possible.

Immediately below the table add one plain-English line:

**Best opportunity:** [item] at [recommended action and price]. [One decisive sentence.]

## Full Gem Cards

Give a Full Gem Card only to the strongest genuine or plausible gems under the existing Full Gem Card rules. Use the following compact layout for each:

## 1. Photographer, Book or Object

**DECISION | BUY SCORE: 00% | Confidence: High, Medium or Low**

**CORE 175: YES, Tier 1, Photographer** or the applicable Core 175 status.

[View the live listing](direct URL)

### Price and action

| | Amount |
|---|---:|
| Current price or bid | £0 |
| Postage and likely fees | £0 |
| Likely delivered total | **£0** |
| Suggested bid or offer | **£0** |
| Maximum item price or hammer | **£0** |
| Estimated delivered at ceiling | **£0** |

For an auction, also show bid count and ending time directly below this table. For fixed-price stock, state whether Best Offer is available.

Then give a highlighted one or two sentence instruction using a Markdown blockquote:

> My move: [specific action, exact opening offer or bidding ceiling, and any condition that must be verified first].

Use these short sections after the action:

### What it is
Maximum two short paragraphs. Identify the exact edition, printing, binding, signature, limitation, condition and completeness. Separate confirmed facts from uncertainty.

### Why it matters
Maximum two short paragraphs. Explain the photographer, the book's importance, canonical position and fit for Jon's collection. Avoid generic biography.

### Market evidence

Prefer a compact table:

| Comparable | Status | Price | Relevance | Link |
|---|---|---:|---|---|

Separate sold evidence from active asking prices. Include only the strongest two to five useful comparisons.

### Value

Show:
- Conservative market range
- Saving at the current delivered price, in cash and percentage
- Saving at the recommended offer or bid, in cash and percentage
- Whether the percentage is well supported or only approximate

If reliable like-for-like evidence is insufficient, say: **Percentage saving not responsibly measurable.**

### Before buying
Use no more than three bullets for decisive checks such as completeness, signature provenance or UK shipping.

### Look inside
Use one clean direct link with the existing FULL, EXTENSIVE, PARTIAL, VIDEO or NONE FOUND label.

### Scores

Use a two-column table rather than a semicolon-separated sentence:

| Factor | Score |
|---|---:|
| Photography/content | 0/10 |
| Design/production | 0/10 |
| Historical importance | 0/10 |
| Collectibility | 0/10 |
| Canonical status | 0/10 |
| Value at the actual all-in price | 0/10 |
| Priority for Jon's collection | 0/10 |

Finish each card with one short sentence explaining the main reasons for the BUY SCORE.

## Other actionable finds

Put worthwhile listings that do not merit a Full Gem Card into one compact table:

| Item | Core 175 | Decision | Current price | Likely real value | Potential bargain | Link |
|---|---|---|---:|---:|---|---|

Give no more than two short sentences beneath the table if an important qualification applies. Do not put suggested bids, offers or maximums in this compact table; reserve those for a Full Gem Card.

## Important passes

Mention only tempting false positives or items Jon might otherwise wonder about. Use short bullets with the decisive reason for passing. Do not list every routine rejection.

Finish with:

**If I were buying one:** [single item, exact action and price]. If nothing is strong enough, say **I would buy none of these.**

Additional readability rules:
- Keep paragraphs to three sentences or fewer.
- Put prices, maximum bids, offer amounts, ending times and listing links near the top, never buried in prose.
- Use pounds for the estimated UK delivered total while retaining the seller's original currency where helpful.
- Do not repeat the same facts in several sections.
- Avoid long introductory or closing explanations.
- Do not expose internal repository scores as if they were the BUY SCORE.
- Do not include PASS listings in the At a glance table.
- A live-status warning must appear immediately below the listing link, in bold.
- If only one strong item exists, still use the At a glance table and one Gem Card.
- If no listing meets the notification threshold, remain silent under the existing rules.

SPENDING CAP AND CHEAP GEM PRIORITY

This instruction overrides any earlier budget language in this prompt.

Jon's default maximum is £150 for the book or item price itself, excluding postage. For an auction, apply the cap to the winning bid or hammer price plus any mandatory buyer fee. For fixed-price stock, apply it to the item price after any accepted offer. Postage may be added on top of the £150 cap and must always be shown separately. Do not add import VAT or customs duty for qualifying printed books.

Rules:

1. Do not notify Jon about or positively recommend an item whose item price plus any mandatory buyer fee exceeds £150, excluding postage, unless it meets the exceptional over-budget test below. Process ordinary over-budget listings silently.

2. Membership of the Core 175 does not override the spending cap. A famous photographer, signed copy, high automated score or ambitious dealer asking price is not enough.

3. For auctions, set the normal maximum winning bid or hammer at £150 or lower after allowing for any mandatory buyer fee. Postage sits outside this cap and does not need to be deducted from the maximum bid. Always show both the maximum bid and the estimated delivered total at that bid.

4. For fixed-price or Best Offer listings asking above £150, surface them only when there is a credible chance of negotiating the item price to £150 or less before postage. Give the exact opening offer, maximum item price and estimated delivered total.

5. An item price above £150 may be surfaced only as **OVER-BUDGET EXCEPTION** when there is strong evidence that it is genuinely too good to miss: a rare and important exact edition, first printing, association or presentation copy, exceptional lifetime inscription, very scarce limited issue or issued print, or another hard-to-replace copy at a materially anomalous price. It should normally be at least about 35% below a conservative like-for-like market value, or have comparably compelling evidence that passing would probably lose a rare opportunity. Explain exactly why it qualifies. Still give a disciplined maximum item price. Do not stretch the definition merely because an item is desirable.

6. Within equally strong opportunities, rank lower item cost and larger genuine discount more highly. Apply these bands to the item price before postage:
- Cheap gem: up to £50
- Strong value: over £50 and up to £100
- Upper-budget opportunity: over £100 and up to £150
- Over-budget exception: item price above £150 only under rule 5

7. Cheapness alone is not sufficient. The book must still be photographically worthwhile, collectible, important, unusually underpriced or particularly well suited to Jon's collection.

8. Convert foreign-currency item prices and delivered totals to approximate GBP when possible. Show item price, postage and delivered total as three distinct figures. Postage and exchange-rate uncertainty do not change whether the item is inside the £150 item-price cap. For posters, photographic prints, mixed-media editions or other non-book objects, include import charges only when they are genuinely applicable and supported by the available information.

9. In any user-facing report, place within-budget cheap gems first. Put any OVER-BUDGET EXCEPTION in a clearly separated section after the affordable opportunities. Never make an expensive exception look like the default recommendation.

10. If there are no worthwhile opportunities within budget and no genuine over-budget exception, remain silent under the existing notification rules.

VALUE-FIRST SUMMARY TABLE

The purpose of the At a glance and Other actionable finds tables is to let Jon instantly compare what an item costs with what the exact copy is probably worth.

For every surfaced listing, make a conservative good-faith estimate of likely real market value using the strongest available like-for-like sold evidence and credible active copies. Adjust for edition, printing, binding, signature, limitation, condition and completeness. Do not copy the highest dealer asking price.

Calculate Potential bargain against the current item price before postage, because Jon's £150 cap also excludes postage. Show postage separately so the full expense remains visible. Use one of these compact forms:

- **£40 to £70 under, about 35% to 50%**
- **Near market value**
- **Possibly 25% under, evidence limited**
- **Not responsibly measurable**

For an auction, prefix the result with **At current bid:**. If an auction is likely to rise substantially, say so in the detailed text rather than pretending the current discount will survive.

Suggested offers, bidding ceilings and maximum prices remain in the Full Gem Card's Price and action section. They must not occupy columns in the quick-view tables.

RECALL AND COVERAGE CHECK

Surface up to three additional concise promising leads when a worthwhile book within the £150 item cap has credible collectibility, underpricing or collection-fit clues but exact edition, a preview, live verification or sold evidence is incomplete. Label INVESTIGATE and state the specific unresolved point. These may be non-urgent and need not qualify for a Full Gem Card. Do not invent market values or percentage savings. Uncertainty is not itself a PASS, and a modestly priced worthwhile copy does not have to be an investment-grade rarity.

At the start of the review, read the latest data/ebay_endgame_state.json stats and last_discovery_at when accessible. If successful discovery is over two hours old, some photographer tiers still have never-searched routes, or the review queue is materially behind, include one concise coverage note in the report footer. If no buying report is due and the outage exceeds six hours, send one brief system-status notice per day. Do not describe all 175 as fully covered merely because they are in the configuration. Keep the existing normal Chat schedule, spending cap, comparable-evidence standards and value-first tables.
