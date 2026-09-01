# Photobook recognition library

This directory extends the live collectible-photobook canon without changing the fixed 628-record Parr/Badger baseline used by the one-time audit.

## Design goal

The live snapshot currently contains roughly 4,250 unique recognition records, inside the operating target of 2,000 to 5,000. The aim is not to collect every photography title. A record belongs here when identifying it cheaply on eBay could plausibly matter to a collector.

The complete live library is assembled at runtime from:

1. the existing Parr/Badger operational master;
2. the existing Roth 101 overlay;
3. supplemental CSV shards in this directory.

The largest supplemental shard is a checked-in snapshot of work-level records from the public-domain [Open Library catalogue](https://openlibrary.org/help/faq/using). It is limited to photography-subject records associated with selected specialist or historically important photography publishers. Every generated row links to its Open Library work record and is assigned a lower tier and slower search priority than the hand-curated canon.

Duplicate contributor and title pairs are merged, so a supplemental record can add aliases, priorities, edition notes or valuation fields to an existing canonical record without duplicating it.

## Record fields

- `Record ID`: stable local identifier.
- `Contributor`: photographer or credited creator.
- `Contributor aliases`: alternate spellings separated by `|`.
- `Title`: canonical working title.
- `Title aliases`: common variants, translated titles and likely seller wording separated by `|`.
- `Year`: publication year when confidently established.
- `Publisher`: publisher when confidently established.
- `ISBN`: useful ISBNs where they genuinely improve recognition.
- `Canon sources`: canon, award or other collector-relevance sources.
- `Collectibility tier`: `S`, `A`, `B`, `C` or `D`.
- `Search priority`: `0` is hottest, then `1`, `2`, `3`, and so on.
- `First edition notes`: concise edition-identification clues.
- `Strong buy GBP`: curated market threshold, blank until evidence is strong.
- `Bargain GBP`: unusually attractive threshold, blank until evidence is strong.
- `Evidence confidence`: confidence in the record metadata.
- `Source`: provenance for the metadata or prioritisation.
- `Search tier`: normally `CORE`; uncertain records should remain `BROAD`.

## Search priority

Priority `0` records are the books worth checking most aggressively and feed the hourly hot-canon lane. Roth-level material defaults to priority `1`. Parr/Badger CORE material defaults to priority `2`. Broader or provisional canon records default to priority `3`. The remaining library is rotated so the full long tail is repeatedly revisited without consuming the eBay API allowance in one burst.

## Expansion plan

The library should be expanded in curated source shards, with de-duplication and confidence tracking. High-value source families include:

- canonical photobook bibliographies and histories;
- major photobook award winners, finalists and historically important shortlists;
- important first monographs and early books by major photographers;
- Japanese photobook canon;
- British and Irish documentary photography;
- American colour and New Topographics traditions;
- Latin American photobooks;
- artist books and conceptual photography books with established collector demand;
- important publisher backlists, including MACK, Steidl, Scalo, Twin Palms, Nazraeli, Aperture, Dewi Lewis, Chris Boot, GOST, Loose Joints, Stanley/Barker, Setanta, TBW, Roma, Super Labo and Akio Nagasawa;
- emerging photographers whose books show strong institutional, award, publishing or secondary-market signals.

The target of 2,000 to 5,000 is a quality boundary, not a quota. Records should not be added simply to increase the count.

## Rebuilding the publisher snapshot

`photobook_library_builder.py` reads `openlibrary_publishers.json` and refreshes `openlibrary_publisher_backlists.csv`. It uses only Python's standard library, retries temporary source errors, excludes obvious instructional-photography titles, de-duplicates works and refuses to overwrite the snapshot if any publisher request fails or fewer than 2,500 records survive.

Run it manually when the publisher configuration changes:

```bash
python photobook_library_builder.py
```

The live GitHub monitor never calls Open Library. It reads the stable checked-in CSV, so an Open Library outage cannot interrupt eBay discovery.

## Matching behaviour

`photobook_recognition.py` normalises punctuation and accents, reuses the contributor-aware fuzzy matching already proven by the Parr/Badger monitor, and adds title aliases and contributor aliases. A token index keeps matching fast at more than 4,000 records. Short and eponymous titles receive stricter conflict checks, preventing a photographer's biography from being mistaken for an identically named monograph.

The opportunity score combines recognition confidence with collectibility tier, price, buying format, private-seller status, casual seller language, bibliographic detail and seller sophistication. Edition evidence is scored separately. A known reprint or conflicting publication year, publisher or ISBN cannot become an urgent first-edition alert, while missing metadata can still create a lower-confidence review candidate.

Recognition is only a discovery signal. Every purchase candidate must still be live-verified and then checked for exact edition, printing, completeness, condition, shipping and current market value before a buy recommendation.
