# Photobook recognition library

This directory extends the live collectible-photobook canon without changing the fixed 628-record Parr/Badger baseline used by the one-time audit.

## Design goal

The working target is about 3,000 high-quality recognition records, with room to grow to roughly 5,000 when the additional records add real discovery value. The aim is not to collect every photography title. A record belongs here when identifying it cheaply on eBay could plausibly matter to a collector.

The complete live library is assembled at runtime from:

1. the existing Parr/Badger operational master;
2. the existing Roth 101 overlay;
3. supplemental CSV shards in this directory.

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

The target of 3,000 to 5,000 is a quality ceiling, not a quota. Records should not be added simply to increase the count.

## Matching behaviour

`photobook_recognition.py` normalises punctuation and accents, reuses the contributor-aware fuzzy matching already proven by the Parr/Badger monitor, and adds title aliases and contributor aliases. It then combines recognition confidence with collectibility tier, price, buying format, private-seller status, casual seller language, missing bibliographic detail and seller sophistication to produce an opportunity score.

Recognition is only a discovery signal. Every purchase candidate must still be live-verified and then checked for exact edition, printing, completeness, condition, shipping and current market value before a buy recommendation.
