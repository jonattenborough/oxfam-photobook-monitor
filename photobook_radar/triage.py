"""Fast, local, evidence-aware triage using the existing recognition library."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import ebay_core_targets
import photobook_recognition

from .config import Config
from .alert_format import format_lead
from .db import transaction
from .store import enqueue_job, enqueue_notification, now


PHOTO_CLUES = ("photobook", "photo book", "photographs", "street photography", "documentary photography", "monograph", "photographic")
NOISE = ("camera manual", "photography handbook", "photoshop", "lightroom", "how to photograph")


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
    price_ok = row["currency"] == "GBP" and row["price_minor"] is not None and row["price_minor"] <= int(Decimal(config.max_recommended_item_gbp) * 100)
    lead = result["score"] >= 58 and price_ok and not ended and bool(result["matches"] or result["core_matches"])
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
        # Historical imports are silent. A fresh lead can alert with an explicit
        # live-status warning while its independent exact-listing job runs.
        if lead and not row["imported"] and row["canonical_url"]:
            enqueue_job(db, f"verify:{job['listing_id']}:{row['current_observation_id']}", "VERIFY", listing_id=job["listing_id"], priority=80 if end else 50, payload={"observation_id": row["current_observation_id"]})
            research_worthy = result["score"] >= 70 or bool(result["matches"]) or (result["score"] >= 58 and row["price_minor"] is not None and row["price_minor"] <= 3000)
            if config.research_recurring_enabled and config.research_provider == "codex_cli" and research_worthy:
                pending = db.execute("SELECT COUNT(*) FROM jobs WHERE kind='RESEARCH_LEAD' AND status IN ('PENDING','RUNNING')").fetchone()[0]
                if pending < 24:
                    enqueue_job(db, f"research-lead:{job['listing_id']}:{row['current_observation_id']}", "RESEARCH_LEAD",
                                listing_id=job["listing_id"], priority=60 + result["score"], payload={"observation_id": row["current_observation_id"]})
            if config.production and config.allow_real_notifications and config.notification_enabled:
                headline, message = format_lead(row, result)
                enqueue_notification(
                    db, listing_id=job["listing_id"], stage="FAST_LEAD", material_version=str(row["current_observation_id"]),
                    channel=config.notification_primary,
                    payload={"title": headline, "message": message, "url": row["canonical_url"]},
                    expires_at=end or (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds").replace("+00:00", "Z"),
                )
        done = db.execute("UPDATE jobs SET status='DONE',lease_token=NULL,lease_owner=NULL,lease_until=NULL WHERE id=? AND lease_token=? AND status='RUNNING'", (job["id"], job["lease_token"]))
        if done.rowcount != 1:
            raise RuntimeError("Stale triage result")
    return result
