"""Fast, local, evidence-aware triage using the existing recognition library."""
from __future__ import annotations

import json
import re
import sqlite3
from decimal import Decimal

import ebay_core_targets
import photobook_recognition

from .config import Config
from .db import transaction
from .store import enqueue_job, now


PHOTO_CLUES = ("photobook", "photo book", "photographs", "street photography", "documentary photography", "monograph", "photographic")
NOISE = ("camera manual", "photography handbook", "photoshop", "lightroom", "how to photograph")
TITLE_STOPWORDS = {"the", "and", "for", "with", "from", "book", "books", "photographs", "photography", "photo", "first", "edition"}
COLLECTOR_CLUES = ("first edition", "first printing", "signed", "inscribed", "limited edition", "numbered", "book and print", "out of print")


def _tokens(value: object) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", str(value or "").casefold()) if len(word) >= 3 and word not in TITLE_STOPWORDS}


def object_in_seller_title(item: dict, result: dict) -> bool:
    """A description-only bibliography hit is not the object being sold."""
    title = _tokens(item.get("title"))
    for match in result["matches"]:
        work = _tokens(match.get("title"))
        author = _tokens(match.get("contributor"))
        if work and work.issubset(title) and (len(work) >= 2 or bool(author & title)):
            return True
    for match in result["core_matches"]:
        author = _tokens(match.get("name"))
        if author and author.issubset(title) and any(clue in str(item.get("title") or "").casefold() for clue in ("book", "photograph", "photo", "signed", "edition")):
            return True
    return False


def research_candidate(item: dict, result: dict) -> bool:
    """Shortlist plausible books, including uncertain and unfamiliar gems."""
    title = str(item.get("title") or "").casefold()
    if any(noise in title for noise in NOISE):
        return False
    if object_in_seller_title(item, result):
        return True
    # Some specialist sellers put only the work's title in the product name.
    # Its opening description can establish that the named photographer made
    # this item, unlike a bibliography mention buried later in the page.
    opening = re.sub(r"[^a-z0-9]+", " ", str(item.get("description") or "")[:240].casefold()).strip()
    named_title = re.sub(r"[^a-z0-9]+", " ", title).strip()
    names = [match.get("contributor") for match in result["matches"]] + [match.get("name") for match in result["core_matches"]]
    if len(named_title) >= 12 and f" {named_title} " in f" {opening} " and any(
        f" {re.sub(r'[^a-z0-9]+', ' ', str(name).casefold()).strip()} " in f" {opening} " for name in names if name
    ):
        return True
    photobook_title = any(clue in title for clue in ("photobook", "photo book", "photographs", "photography", "photographic monograph"))
    known_in_description = bool(result["matches"] or result["core_matches"])
    collectible_claim = any(clue in title for clue in COLLECTOR_CLUES)
    return bool(photobook_title and (known_in_description or collectible_claim))


def visible_item(item: dict) -> dict:
    """Only score seller-visible facts, never a search query or old issue packet.

    Legacy `context` contains the query URL, priority targets, and editorial
    comments. Feeding it to the recognition library assigns unrelated books
    to whichever photographer happened to be in that search batch.
    """
    fields = (
        "title", "description", "condition_description", "author", "publisher",
        "publication_year", "edition", "isbn", "category_id", "category_path",
        "price_gbp", "observed_price_gbp", "price_value", "price_currency",
        "seller_feedback_score", "search_lane", "buying_options", "item_end_date",
    )
    return {key: item[key] for key in fields if key in item}


def score(item: dict) -> dict:
    visible = visible_item(item)
    matches = photobook_recognition.match_listing(visible, limit=5)
    core_visible = {key: visible[key] for key in ("title", "description", "condition_description", "author", "category_id", "category_path") if key in visible}
    core_matches = ebay_core_targets.matches_for_item(core_visible)
    photo_text = " ".join(str(visible.get(key) or "") for key in ("title", "description", "condition_description")).casefold()
    photo = any(clue in photo_text for clue in PHOTO_CLUES)
    noise = any(clue in photo_text for clue in NOISE)
    if core_matches and ebay_core_targets.target_names_need_photo_evidence([m["name"] for m in core_matches]):
        if ebay_core_targets.target_object_context(core_visible) != "supported":
            core_matches = [m for m in core_matches if not ebay_core_targets.target_names_need_photo_evidence([m["name"]])]
    if matches:
        best = matches[0]
        rating, reasons = photobook_recognition.opportunity_score(visible, best)
        photographer = best.get("contributor")
        normalized_name = ebay_core_targets.normalized(photographer)
        normalized_listing = ebay_core_targets.normalized(
            " ".join(str(visible.get(key) or "") for key in ("title", "description", "author"))
        )
        name_visible = bool(normalized_name and f" {normalized_name} " in f" {normalized_listing} ")
        if not name_visible and not photo:
            rating = min(rating, 35)
            reasons.insert(0, "work match lacks visible photographer or photographic context")
            photographer = None
            matches = []
    else:
        best = None
        price = visible.get("price_gbp") or visible.get("observed_price_gbp")
        try:
            affordable = price is not None and Decimal(str(price)) <= Decimal("50")
        except Exception:
            affordable = False
        rating = 61 if core_matches and affordable and not noise else 43 if core_matches and not noise else 59 if photo and affordable and not noise else 35 if photo and not noise else 8
        reasons = ["Core photographer visible in seller title or description; book and edition unverified" if core_matches else "unfamiliar photographer or unidentified book retained for exploration" if rating == 59 else "photobook clues require identification" if rating == 35 else "no strong local photobook evidence"]
        photographer = core_matches[0]["name"] if core_matches else None
    if core_matches and not photographer:
        photographer = core_matches[0]["name"]
    tier = core_matches[0]["tier"] if core_matches else None
    return {"score": rating, "reasons": reasons[:8], "matches": matches, "core_matches": core_matches, "photographer": photographer, "core_tier": tier}


def run_triage(db: sqlite3.Connection, job: sqlite3.Row, config: Config) -> dict:
    row = db.execute("SELECT l.*,o.raw_json,o.price_minor,o.currency,o.shipping_minor,o.shipping_currency,o.auction_end_at,o.observed_at FROM listings l JOIN observations o ON o.id=l.current_observation_id WHERE l.id=?", (job["listing_id"],)).fetchone()
    if row is None:
        raise ValueError("Listing has no observation")
    requested_observation = json.loads(job["payload_json"]).get("observation_id")
    item = json.loads(row["raw_json"])
    result = score(item)
    end = row["auction_end_at"]
    ended = bool(end and end <= now())
    availability = "ENDED" if ended else row["availability"]
    priced = row["currency"] in {"GBP", "EUR", "USD", "CHF", "AUD", "CAD"} and row["price_minor"] is not None
    title = str(item.get("title") or "").casefold()
    unfamiliar_collectible = any(clue in title for clue in COLLECTOR_CLUES) and any(clue in title for clue in PHOTO_CLUES)
    lead = (result["score"] >= 55 or unfamiliar_collectible) and (priced or result["score"] >= 75) and not ended and research_candidate(item, result)
    with transaction(db):
        owned = db.execute("SELECT 1 FROM jobs WHERE id=? AND lease_token=? AND status='RUNNING'", (job["id"], job["lease_token"])).fetchone()
        if not owned:
            raise RuntimeError("Stale triage lease")
        latest_observation = db.execute("SELECT current_observation_id FROM listings WHERE id=?", (job["listing_id"],)).fetchone()[0]
        if latest_observation != row["current_observation_id"] or (requested_observation is not None and requested_observation != latest_observation):
            enqueue_job(db, f"triage:{job['listing_id']}:{latest_observation}", "TRIAGE", listing_id=job["listing_id"], priority=10, payload={"observation_id": latest_observation})
            db.execute("UPDATE jobs SET status='DONE',lease_token=NULL,lease_owner=NULL,lease_until=NULL WHERE id=? AND lease_token=?", (job["id"], job["lease_token"]))
            return {"stale_observation": True}
        db.execute("UPDATE listings SET triage_score=?,triage_reason=?,photographer=?,core_tier=?,processing='TRIAGED',availability=?,last_triaged_at=?,verdict=? WHERE id=?", (result["score"], "; ".join(result["reasons"][:3]), result["photographer"], result["core_tier"], availability, now(), "INVESTIGATE" if lead else None, job["listing_id"]))
        db.execute("DELETE FROM recognition_matches WHERE listing_id=?", (job["listing_id"],))
        for match in result["matches"]:
            db.execute("INSERT OR REPLACE INTO recognition_matches(listing_id,book_record_key,contributor,score,reason,source,matched_at) VALUES(?,?,?,?,?,?,?)", (job["listing_id"], match.get("record_id") or (str(match.get("contributor")) + "|" + str(match.get("title"))), match.get("contributor"), match.get("score"), match.get("reason"), match.get("source"), now()))
        # Local matching only shortlists candidates; it does not make the
        # collector decision or create a phone event.
        if lead and not row["imported"] and row["canonical_url"]:
            if row["platform"] == "ebay":
                enqueue_job(db, f"verify:{job['listing_id']}:{row['current_observation_id']}", "VERIFY", listing_id=job["listing_id"], priority=80 if end else 50, payload={"observation_id": row["current_observation_id"]})
            if config.research_recurring_enabled and config.research_provider == "codex_cli":
                # Search routes can find the same item in several windows. Only
                # the current observation can be researched, so retire older
                # queued work before it consumes the research queue.
                db.execute("UPDATE jobs SET status='CANCELLED',last_error='Superseded by a newer listing observation' "
                           "WHERE kind='RESEARCH_LEAD' AND listing_id=? AND status='PENDING' "
                           "AND job_key!=?", (job["listing_id"], f"research-lead:{job['listing_id']}:{row['current_observation_id']}"))
                enqueue_job(db, f"research-lead:{job['listing_id']}:{row['current_observation_id']}", "RESEARCH_LEAD",
                            listing_id=job["listing_id"], priority=60 + result["score"] + (25 if end else 0),
                            payload={"observation_id": row["current_observation_id"]})
        done = db.execute("UPDATE jobs SET status='DONE',lease_token=NULL,lease_owner=NULL,lease_until=NULL WHERE id=? AND lease_token=? AND status='RUNNING'", (job["id"], job["lease_token"]))
        if done.rowcount != 1:
            raise RuntimeError("Stale triage result")
    return result
