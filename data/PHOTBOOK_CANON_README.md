# Operational photobook canon

The live discovery system currently uses two primary reference layers.

## Parr / Badger

`data/parr_badger_master/*.csv`

- 628 operational records from *The Photobook: A History*, Volumes I to III.
- 526 CORE records and 102 BROAD or provisional records in the current working extraction.
- This directory remains physically unchanged while the one-time 628-record baseline sweep is in progress.

## Andrew Roth, The Book of 101 Books

`data/roth_101_master.csv`

- 101 books, all treated as CORE canonical references.
- 67 titles map to an existing Parr / Badger operational record.
- 34 titles add new unique search targets.
- Combined live canon: 662 operational records.

The Roth file retains the book's publication statement, Roth page, physical description of an ideal copy, edition or issue notes where stated, limitation clues, issued components and Parr / Badger overlap. These details are identification aids, not a substitute for checking an actual copy.

`canon_runner.py` overlays Roth onto the Parr / Badger records for the live Oxfam, Shelter and Crisis monitors. Overlapping Roth titles are promoted to CORE and tagged with the Roth page. Roth-only books become first-class CORE discovery records.

The fixed Parr / Badger loader in `parr_badger_runner.py` is intentionally left intact because current baseline and rotation jobs may depend on its stable 628-record ordering.

## Buying rule

A canon match means investigate. It does not by itself mean buy. Exact edition, printing, binding, jacket or other issued components, signature or limitation, condition and realistic market value must still be verified before an alert is escalated.
