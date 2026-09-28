"""Failure cases for the local radar persistence and screening boundary."""
from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from photobook_radar.config import Config
from photobook_radar.abebooks_scheduler import schedule_abebooks
from photobook_radar.bargains import assess_bargain, check_ebay_asking
from photobook_radar.db import connect, migrate, transaction
from photobook_radar.ebay_gateway import BrowseWindow, MeteredEbayBrowseClient
from photobook_radar.ebay_lanes import _endgame_slots, run_ebay_lane_job, schedule_ebay_lanes
from photobook_radar.ebay_scheduler import run_ebay_broad_job, schedule_ebay_broad
from photobook_radar.notifications import send_one
from photobook_radar.source_scheduler import run_oxfam_scan_job, schedule_oxfam
from photobook_radar.oxfam_broad import run_oxfam_broad_job, schedule_oxfam_broad
from photobook_radar.research_sweeps import ResearchDeferred, run_lead_research, run_research_job, schedule_research
from photobook_radar.shopify_scheduler import parse_products, run_shopify_job, schedule_shopify
from photobook_radar.sources.ebay import capture_browse_page, parse_browse_page
from photobook_radar.sources.oxfam import capture_photography_page, parse_photography_page
from photobook_radar.store import capture, capture_page, claim_job, decide, enqueue_job, enqueue_notification, finish_job, now, reserve_request, safe_url, settle_request
from photobook_radar.telegram_photos import ingest_updates, run_photo_research
from photobook_radar.triage import object_in_seller_title, research_candidate, run_triage, score
from photobook_radar.verification import run_verify


class RadarPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.folder.name) / "radar.db")
        migrate(self.db)

    def tearDown(self):
        self.db.close()
        self.folder.cleanup()

    def test_bargain_screen_requires_recent_sold_like_for_like_and_margin(self):
        today = datetime.now(timezone.utc).date().isoformat()
        comps = [{"url": "https://www.ebay.co.uk/itm/200000000001", "kind": "SOLD", "price_gbp": 200,
                  "sold_date": today, "same_edition": True, "condition_no_better": True},
                 {"url": "https://www.abaa.org/book/second", "kind": "ASKING", "price_gbp": 180,
                  "sold_date": "", "same_edition": True, "condition_no_better": True}]
        def assess(candidates, *, price_minor=2000, currency="GBP", checker=lambda *_: True):
            return assess_bargain(candidates, title="Danny Lyon The Bikeriders", price_minor=price_minor,
                                  currency=currency, shipping_minor=None, shipping_currency=None,
                                  max_buy_gbp=Decimal("200"), min_profit_gbp=Decimal("50"),
                                  min_discount_pct=40, checker=checker)
        good = assess(comps)
        self.assertTrue(good["accepted"])
        self.assertEqual(good["landed_gbp"], Decimal("40"))
        self.assertEqual(good["comp_floor_gbp"], Decimal("180"))
        self.assertFalse(assess([{**comps[0], "same_edition": False}, comps[1]])["accepted"])
        self.assertFalse(assess([{**comps[0], "kind": "ASKING"}, comps[1]])["accepted"])
        self.assertFalse(assess([{**comps[0], "sold_date": "2018-01-01"}, comps[1]])["accepted"])
        self.assertFalse(assess(comps, checker=lambda *_: False)["accepted"])
        self.assertFalse(assess(comps, currency="EUR")["accepted"])
        self.assertTrue(assess(comps, price_minor=6000)["accepted"])
        self.assertFalse(assess(comps, price_minor=8000)["accepted"])
        self.assertFalse(assess(comps, price_minor=9000)["accepted"])
        self.assertFalse(assess(comps, price_minor=19000)["accepted"])

    def test_collector_priority_can_have_no_flip_profit_but_requires_curated_tier_one_work(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_real_notifications=True,
                        notification_enabled=True, research_provider="codex_cli", research_recurring_enabled=True)
        sold = [{"url": "https://www.ebay.co.uk/itm/200000000001", "kind": "SOLD", "price_gbp": 200,
                 "sold_date": datetime.now(timezone.utc).date().isoformat(), "same_edition": True,
                 "condition_no_better": True, "note": "Same edition and wear"}]
        result = {"decision": "COLLECTOR", "actual_book": True, "collector_fit": True, "edition_supported": True,
                  "context": "A central photography book by a Tier 1 artist.",
                  "opportunity_reason": "The identified priority edition is offered below the checked matching sale.",
                  "edition_note": "The seller identifies this printing.", "risk": "Check the title page and wear.",
                  "source_urls": [], "market_comparables": sold}
        for number, title, expected in ((1, "Alec Soth Sleeping by the Mississippi first edition", "DONE"),
                                        (2, "Good Morning America Volume Two Mark Power Signed", "NEEDS_EVIDENCE")):
            with transaction(self.db):
                listing_id = capture(self.db, {"key": f"ebay:12345678901{number}", "title": title,
                                               "price_gbp": "140", "url": f"https://www.ebay.co.uk/itm/12345678901{number}"},
                                     source_id="ebay", origin_key=f"collector-{number}", imported=False)
                observation = self.db.execute("SELECT current_observation_id FROM listings WHERE id=?", (listing_id,)).fetchone()[0]
                enqueue_job(self.db, f"research:collector-{number}", "RESEARCH_LEAD", listing_id=listing_id,
                            payload={"observation_id": observation})
                self.db.execute("INSERT INTO live_checks(listing_id,observation_id,provider,checked_at,availability,price_minor,currency,result_json) VALUES(?,?,?,?,?,?,?,?)",
                                (listing_id, observation, "ebay_browse", now(), "LIVE", 14000, "GBP", "{}"))
            job = claim_job(self.db, "collector-test", kinds=("RESEARCH_LEAD",))
            outcome = run_lead_research(self.db, job, config, provider=lambda *_: result,
                                        link_check=lambda *_: True, market_check=lambda *_: True)
            self.assertEqual(outcome["status"], expected)
        event = self.db.execute("SELECT payload_json FROM notification_events WHERE stage='BARGAIN_FIND'").fetchone()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notification_events WHERE stage='BARGAIN_FIND'").fetchone()[0], 1)
        payload = __import__("json").loads(event[0])
        self.assertIn("Collection priority", payload["message"])
        self.assertNotIn("possible resale margin", payload["message"])
        self.assertTrue(send_one(self.db, config, fake=True))

    def test_possible_gem_uses_independent_asking_prices_and_explicit_label(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True,
                        allow_real_notifications=True, notification_enabled=True,
                        research_provider="codex_cli", research_recurring_enabled=True)
        sold_free_comps = [
            {"url": "https://www.ebay.co.uk/itm/200000000001", "kind": "ASKING", "price_gbp": 220,
             "sold_date": "", "same_edition": True, "condition_no_better": True, "note": "Same printing, similar wear"},
            {"url": "https://www.abebooks.co.uk/book/200000000002", "kind": "ASKING", "price_gbp": 190,
             "sold_date": "", "same_edition": True, "condition_no_better": True, "note": "Same printing, similar wear"},
        ]
        result = {"decision": "POSSIBLE_GEM", "actual_book": True, "collector_fit": True,
                  "edition_supported": True, "context": "Danny Lyon's documentary book is a key work.",
                  "opportunity_reason": "The seller identifies an early printing priced far below two independent offers.",
                  "edition_note": "The seller describes the printing.", "risk": "Asking prices are not completed sales.",
                  "source_urls": [], "market_comparables": sold_free_comps}
        def review(number, comps, decision="POSSIBLE_GEM"):
            with transaction(self.db):
                listing_id = capture(self.db, {"key": f"ebay:12345678901{number}",
                                               "title": "Danny Lyon The Bikeriders first printing 1968",
                                               "price_gbp": "20", "url": f"https://www.ebay.co.uk/itm/12345678901{number}"},
                                     source_id="ebay", origin_key=f"possible-{number}")
                observation = self.db.execute("SELECT current_observation_id FROM listings WHERE id=?", (listing_id,)).fetchone()[0]
                enqueue_job(self.db, f"research:possible-{number}", "RESEARCH_LEAD", listing_id=listing_id,
                            payload={"observation_id": observation})
                self.db.execute("INSERT INTO live_checks(listing_id,observation_id,provider,checked_at,availability,price_minor,currency,result_json) VALUES(?,?,?,?,?,?,?,?)",
                                (listing_id, observation, "ebay_browse", now(), "LIVE", 2000, "GBP", "{}"))
            return run_lead_research(self.db, claim_job(self.db, "test", kinds=("RESEARCH_LEAD",)), config,
                                     provider=lambda *_: {**result, "decision": decision, "market_comparables": comps},
                                     market_check=lambda *_: True)
        outcome = review(1, sold_free_comps)
        self.assertEqual((outcome["status"], outcome["verdict"]), ("DONE", "POSSIBLE_GEM"))
        payload = json.loads(self.db.execute("SELECT payload_json FROM notification_events WHERE stage='BARGAIN_FIND'").fetchone()[0])
        self.assertIn("POSSIBLE GEM", payload["title"])
        self.assertIn("asking prices, not sales", payload["message"])
        self.assertEqual(payload["comp_label"], "Asking comp")
        self.assertTrue(send_one(self.db, config, fake=True))
        same_market = [sold_free_comps[0], {**sold_free_comps[1], "url": "https://www.ebay.co.uk/itm/200000000002"}]
        self.assertEqual(review(2, same_market)["status"], "NEEDS_EVIDENCE")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notification_events WHERE stage='BARGAIN_FIND'").fetchone()[0], 1)
        self.assertEqual(review(3, sold_free_comps, decision="PASS")["verdict"], "POSSIBLE_GEM")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notification_events WHERE stage='BARGAIN_FIND'").fetchone()[0], 2)

    def test_possible_collection_buy_uses_two_independent_asking_prices(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True,
                        allow_real_notifications=True, notification_enabled=True,
                        research_provider="codex_cli", research_recurring_enabled=True)
        with transaction(self.db):
            listing_id = capture(self.db, {"key": "ebay:123456789012", "title": "Alec Soth Sleeping by the Mississippi first edition",
                                           "price_gbp": "100", "url": "https://www.ebay.co.uk/itm/123456789012"},
                                 source_id="ebay", origin_key="possible-collector")
            observation = self.db.execute("SELECT current_observation_id FROM listings WHERE id=?", (listing_id,)).fetchone()[0]
            enqueue_job(self.db, "research:possible-collector", "RESEARCH_LEAD", listing_id=listing_id,
                        payload={"observation_id": observation})
            self.db.execute("INSERT INTO live_checks(listing_id,observation_id,provider,checked_at,availability,price_minor,currency,result_json) VALUES(?,?,?,?,?,?,?,?)",
                            (listing_id, observation, "ebay_browse", now(), "LIVE", 10000, "GBP", "{}"))
        result = {"decision": "POSSIBLE_COLLECTOR", "actual_book": True, "collector_fit": True,
                  "edition_supported": True, "context": "A central book by a Tier 1 documentary photographer.",
                  "opportunity_reason": "The seller identifies the desired edition below two independent offers.",
                  "edition_note": "The seller identifies the first printing.", "risk": "Verify printing and condition.",
                  "source_urls": [], "market_comparables": [
                      {"url": "https://www.ebay.co.uk/itm/200000000001", "kind": "ASKING", "price_gbp": 200,
                       "sold_date": "", "same_edition": True, "condition_no_better": True, "note": "Same edition"},
                      {"url": "https://www.abebooks.co.uk/book/200000000002", "kind": "ASKING", "price_gbp": 195,
                       "sold_date": "", "same_edition": True, "condition_no_better": True, "note": "Same edition"}]}
        outcome = run_lead_research(self.db, claim_job(self.db, "test", kinds=("RESEARCH_LEAD",)), config,
                                     provider=lambda *_: result, market_check=lambda *_: True)
        self.assertEqual((outcome["status"], outcome["verdict"]), ("DONE", "POSSIBLE_COLLECTOR"))
        payload = json.loads(self.db.execute("SELECT payload_json FROM notification_events WHERE stage='BARGAIN_FIND'").fetchone()[0])
        self.assertIn("POSSIBLE COLLECTION BUY", payload["title"])
        self.assertIn("resale unproven", payload["message"])

    def test_old_research_alert_waiting_in_outbox_is_suppressed(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_real_notifications=True,
                        notification_enabled=True)
        with transaction(self.db):
            listing_id = capture(self.db, {"key": "ebay:123456789012", "title": "Possible book"},
                                 source_id="ebay", origin_key="old", imported=False)
            enqueue_notification(self.db, listing_id=listing_id, stage="RESEARCHED_FIND", material_version="old",
                                 channel="telegram", payload={"title": "Old lead"})
        self.assertFalse(send_one(self.db, config, fake=True))
        self.assertEqual(self.db.execute("SELECT status FROM notification_events").fetchone()[0], "SUPPRESSED")

    def test_bargain_alert_is_suppressed_if_live_price_rose(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_real_notifications=True,
                        notification_enabled=True)
        with transaction(self.db):
            listing_id = capture(self.db, {"key": "ebay:123456789012", "title": "Danny Lyon The Bikeriders",
                                           "price_gbp": "20", "url": "https://www.ebay.co.uk/itm/123456789012"},
                                 source_id="ebay", origin_key="price-rise", imported=False)
            observation = self.db.execute("SELECT current_observation_id FROM listings WHERE id=?", (listing_id,)).fetchone()[0]
            review = self.db.execute("INSERT INTO reviews(listing_id,observation_id,provider,policy_hash,verdict,status,result_json) VALUES(?,?,?,?,?,?,?)",
                                     (listing_id, observation, "codex_cli", "collector-bargains-v8", "GEM", "DONE",
                                      '{"bargain_screen":{"accepted":true}}'))
            enqueue_notification(self.db, listing_id=listing_id, stage="BARGAIN_FIND", material_version="price-rise",
                                 channel="telegram", payload={"title": "Gem", "review_id": review.lastrowid,
                                                              "price_minor": 2000, "shipping_minor": None,
                                                              "shipping_currency": None})
            self.db.execute("INSERT INTO live_checks(listing_id,observation_id,provider,checked_at,availability,price_minor,currency,result_json) VALUES(?,?,?,?,?,?,?,?)",
                            (listing_id, observation, "ebay_browse", now(), "LIVE", 4500, "GBP", "{}"))
        self.assertFalse(send_one(self.db, config, fake=True))
        self.assertEqual(self.db.execute("SELECT status FROM notification_events").fetchone()[0], "SUPPRESSED")

    def test_observation_order_and_repeat_import(self):
        item = {"key": "ebay:123456789012", "title": "Danny Lyon Bikeriders first", "price_gbp": "20"}
        with transaction(self.db):
            first = capture(self.db, item, source_id="ebay", origin_key="a", imported=True, observed_at="2026-09-26T10:00:00Z")
            current = capture(self.db, {**item, "title": "Later copy", "price_gbp": "18"}, source_id="ebay", origin_key="b", imported=True, observed_at="2026-09-28T10:00:00Z")
            stale = capture(self.db, {**item, "title": "Older copy", "price_gbp": "10"}, source_id="ebay", origin_key="c", imported=True, observed_at="2026-09-27T10:00:00Z")
            repeated = capture(self.db, {**item, "title": "Later copy", "price_gbp": "18"}, source_id="ebay", origin_key="b", imported=True, observed_at="2026-09-28T10:00:00Z")
        self.assertEqual((first, current, stale, repeated), (first,) * 4)
        row = self.db.execute("SELECT l.title,o.price_minor FROM listings l JOIN observations o ON o.id=l.current_observation_id WHERE l.id=?", (first,)).fetchone()
        self.assertEqual((row["title"], row["price_minor"]), ("Later copy", 1800))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 3)

    def test_different_seller_copies_stay_distinct(self):
        with transaction(self.db):
            left = capture(self.db, {"key": "ebay:123456789012", "title": "Same book", "isbn": "9780000000001"}, source_id="ebay", origin_key="left", imported=True)
            right = capture(self.db, {"key": "ebay:123456789013", "title": "Same book", "isbn": "9780000000001"}, source_id="ebay", origin_key="right", imported=True)
        self.assertNotEqual(left, right)

    def test_live_rediscovery_of_identical_archived_copy_becomes_fresh_work(self):
        item = {"key": "ebay:123456789012", "title": "Danny Lyon Bikeriders", "price_gbp": "20", "url": "https://www.ebay.co.uk/itm/123456789012"}
        with transaction(self.db):
            listing_id = capture(self.db, item, source_id="ebay", origin_key="historical", imported=True, observed_at="2026-09-01T00:00:00Z")
            self.db.execute("UPDATE listings SET processing='TRIAGED',triage_score=80 WHERE id=?", (listing_id,))
        from photobook_radar.web import _cards
        self.assertEqual(_cards(self.db, "finds", "", 1)[1], 0)
        with transaction(self.db):
            rediscovered = capture(self.db, item, source_id="ebay", origin_key="live-rediscovery", imported=False)
        self.assertEqual(rediscovered, listing_id)
        row = self.db.execute("SELECT imported,processing,triage_score FROM listings WHERE id=?", (listing_id,)).fetchone()
        self.assertEqual(tuple(row), (0, "CAPTURED", None))

    def test_first_source_baseline_stays_silent_until_material_change(self):
        item = {"key": "ebay:123456789012", "title": "Danny Lyon Bikeriders", "price_gbp": "20", "url": "https://www.ebay.co.uk/itm/123456789012"}
        ids = capture_page(self.db, source_id="ebay", route_id="route", window_id="baseline", page_number=1, continuation={}, complete=True, items=[item], imported=True)
        capture_page(self.db, source_id="ebay", route_id="route", window_id="repeat", page_number=1, continuation={}, complete=True, items=[item])
        row = self.db.execute("SELECT imported,baseline_at FROM listings WHERE id=?", (ids[0],)).fetchone()
        self.assertEqual(row["imported"], 1)
        self.assertIsNotNone(row["baseline_at"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='TRIAGE'").fetchone()[0], 0)
        capture_page(self.db, source_id="ebay", route_id="route", window_id="changed", page_number=1, continuation={}, complete=True, items=[{**item, "price_gbp": "18"}])
        self.assertEqual(self.db.execute("SELECT imported FROM listings WHERE id=?", (ids[0],)).fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='TRIAGE'").fetchone()[0], 1)

    def test_new_listing_after_fresh_epoch_is_not_silenced_by_late_route_baseline(self):
        with transaction(self.db):
            self.db.execute("INSERT INTO health(key,value,updated_at) VALUES('fresh_start_at','2026-09-28T07:50:25Z','2026-09-28T07:50:25Z')")
        item = {"key": "ebay:123456789012", "title": "New photobook", "item_creation_date": "2026-09-28T08:00:00Z"}
        ids = capture_page(self.db, source_id="ebay", route_id="route", window_id="late-baseline", page_number=1, continuation={}, complete=True, items=[item], imported=True)
        self.assertEqual(self.db.execute("SELECT imported FROM listings WHERE id=?", (ids[0],)).fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='TRIAGE'").fetchone()[0], 1)

    def test_historical_copy_first_seen_in_baseline_stays_archived(self):
        item = {"key": "ebay:123456789012", "title": "Danny Lyon Bikeriders", "price_gbp": "20"}
        with transaction(self.db):
            listing_id = capture(self.db, item, source_id="ebay", origin_key="history", imported=True, observed_at="2026-09-01T00:00:00Z")
        capture_page(self.db, source_id="ebay", route_id="route", window_id="baseline", page_number=1, continuation={}, complete=True, items=[item], imported=True)
        capture_page(self.db, source_id="ebay", route_id="route", window_id="repeat", page_number=1, continuation={}, complete=True, items=[item])
        self.assertEqual(self.db.execute("SELECT imported FROM listings WHERE id=?", (listing_id,)).fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='TRIAGE'").fetchone()[0], 0)

    def test_shopify_first_pass_baselines_and_next_change_is_screened(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True, source_charity_shops=True)
        self.assertEqual(schedule_shopify(self.db, config), 4)
        job = claim_job(self.db, "shopify-test", kinds=("SCAN_SHOPIFY",))
        product = {"id": 1234, "title": "Danny Lyon Bikeriders", "handle": "bikeriders", "variants": [{"available": True, "price": "20.00"}]}
        payload = {"products": [product]}
        self.assertEqual(run_shopify_job(self.db, job, config, fetch=lambda *_: payload)["count"], 1)
        finish_job(self.db, job["id"], job["lease_token"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='TRIAGE'").fetchone()[0], 0)
        with transaction(self.db):
            self.db.execute("UPDATE jobs SET status='CANCELLED' WHERE kind='SCAN_SHOPIFY' AND route_id!=?", (job["route_id"],))
            self.db.execute("UPDATE source_routes SET next_due_at=NULL WHERE id=?", (job["route_id"],))
        schedule_shopify(self.db, config)
        next_job = claim_job(self.db, "shopify-test", kinds=("SCAN_SHOPIFY",))
        self.assertEqual(run_shopify_job(self.db, next_job, config, fetch=lambda *_: {"products": [{**product, "variants": [{"available": True, "price": "18.00"}]}]})["count"], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='TRIAGE'").fetchone()[0], 1)

    def test_shopify_unicode_handle_is_encoded_as_one_safe_path_segment(self):
        product = {"id": 1234, "title": "Japanese photobook", "handle": "消えたママ友-mf-comic-essay",
                   "variants": [{"available": True, "price": "20.00"}]}
        row = parse_products("charity:shelter-books", {"products": [product]})[0]
        self.assertIn("/products/%E6%B6%88", row["url"])
        product["handle"] = "../private"
        with self.assertRaises(ValueError):
            parse_products("charity:shelter-books", {"products": [product]})

    def test_shopify_signed_and_unsigned_variants_have_their_own_prices(self):
        product = {"id": 1234, "title": "As It Was Give(n) To Me", "handle": "as-it-was-given-to-me",
                   "body_html": "<p>Signed by Stacy Kranitz.</p>",
                   "variants": [{"id": 21, "title": "unsigned", "available": True, "price": "70.00"},
                                {"id": 22, "title": "signed", "available": True, "price": "80.00"},
                                {"id": 23, "title": "special", "available": False, "price": "30.00"}]}
        rows = parse_products("specialist:setanta", {"products": [product]})
        self.assertEqual(len(rows), 2)
        self.assertEqual([(row["offered_variant"], row["price_gbp"]) for row in rows],
                         [("unsigned", 70.0), ("signed", 80.0)])
        self.assertNotEqual(rows[0]["key"], rows[1]["key"])
        self.assertTrue(rows[0]["url"].endswith("?variant=21"))

    def test_ebay_asking_comparable_uses_exact_live_browse_price(self):
        item = {"title": "Danny Lyon The Bikeriders 1968 first edition", "price": {"value": "200", "currency": "GBP"},
                "buyingOptions": ["FIXED_PRICE"]}
        url = "https://www.ebay.co.uk/itm/123456789012"
        self.assertTrue(check_ebay_asking(url, "Danny Lyon The Bikeriders", Decimal("200"), "ASKING", lambda _: item))
        self.assertFalse(check_ebay_asking(url, "Danny Lyon The Bikeriders", Decimal("160"), "ASKING", lambda _: item))
        self.assertFalse(check_ebay_asking(url, "Danny Lyon The Bikeriders", Decimal("200"), "SOLD", lambda _: item))
        self.assertFalse(check_ebay_asking(url, "Danny Lyon The Bikeriders", Decimal("200"), "ASKING",
                                           lambda _: {**item, "buyingOptions": ["AUCTION"]}))

    def test_three_ebay_lanes_schedule_with_one_durable_budget(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True,
                        source_ebay_private=True, source_ebay_charity=True, source_ebay_endgame=True)
        counts = schedule_ebay_lanes(self.db, config)
        self.assertEqual(counts["private"], 24)
        self.assertEqual(counts["charity"], 12)
        self.assertEqual(counts["endgame"], 26)
        self.assertEqual(schedule_ebay_lanes(self.db, config), {})
        job = claim_job(self.db, "ebay-test", kinds=("SCAN_EBAY_ENDGAME",))
        item = {"itemId": "v1|123456789012|0", "title": "Danny Lyon photobook", "price": {"value": "20", "currency": "GBP"},
                "itemWebUrl": "https://www.ebay.co.uk/itm/123456789012", "itemCreationDate": "2026-09-01T00:00:00Z"}
        result = run_ebay_lane_job(self.db, job, config, fetch=lambda *_: {"itemSummaries": [item], "total": 1, "offset": 0})
        self.assertEqual(result["items"], 1)
        self.assertEqual(self.db.execute("SELECT imported FROM listings WHERE platform='ebay'").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='TRIAGE'").fetchone()[0], 0)

    def test_endgame_yields_quota_to_private_sellers(self):
        config = Config(data_dir=Path(self.folder.name), ebay_reserve=25)
        reset = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with transaction(self.db):
            self.db.execute("INSERT INTO api_windows(bucket,window_start,reset_at,provider_limit,provider_remaining) VALUES('ebay_browse',?,?,5000,5000)", (now(), reset))
        self.assertLess(_endgame_slots(self.db, config), 20)
        with transaction(self.db):
            self.db.execute("UPDATE api_windows SET provider_remaining=100 WHERE bucket='ebay_browse'")
        self.assertEqual(_endgame_slots(self.db, config), 0)

    def test_abebooks_rotates_ninety_six_titles_per_hour(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True, source_abebooks=True)
        self.assertEqual(schedule_abebooks(self.db, config), 24)
        self.assertEqual(self.db.execute("SELECT cadence_seconds FROM sources WHERE id='abebooks'").fetchone()[0], 900)
        with transaction(self.db):
            self.db.execute("UPDATE health SET value='2000-01-01T00:00:00Z' WHERE key='abebooks_next_schedule'")
        self.assertEqual(schedule_abebooks(self.db, config), 24)
        self.assertEqual(self.db.execute("SELECT value FROM health WHERE key='abebooks_cursor'").fetchone()[0], "48")

    def test_belgian_ebay_listing_uses_the_real_marketplace_host(self):
        from ebay_api import MARKETPLACE_DOMAINS, listing_from_summary
        source = {"id": "ebay-endgame", "name": "eBay Belgium", "marketplace": "EBAY_BE"}
        item = {"itemId": "v1|123456789012|0", "title": "Collectible photobook",
                "itemWebUrl": "https://www.benl.ebay.be/itm/123456789012",
                "price": {"value": "20", "currency": "EUR"}}
        self.assertEqual(MARKETPLACE_DOMAINS["EBAY_BE"], "www.benl.ebay.be")
        self.assertEqual(parse_browse_page({"itemSummaries": [item], "total": 1, "offset": 0}, source).items[0]["url"], item["itemWebUrl"])
        self.assertEqual(listing_from_summary({k: v for k, v in item.items() if k != "itemWebUrl"}, source)["url"], item["itemWebUrl"])
        with self.assertRaisesRegex(ValueError, "unexpected seller URL"):
            parse_browse_page({"itemSummaries": [{**item, "itemWebUrl": "https://www.evil.example/itm/123456789012"}], "total": 1, "offset": 0}, source)

    def test_foreign_endgame_category_uses_a_market_query(self):
        from photobook_radar import ebay_lanes
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True)
        payload = {"route_id": "ebay-endgame:fr", "lane": "endgame",
                   "definition": {"marketplace": "EBAY_FR", "lane_name": "category", "category_ids": "261186", "query": None, "horizon_hours": 72}}
        with patch.object(ebay_lanes, "thread_client") as factory:
            factory.return_value.search_page.return_value = {"itemSummaries": []}
            ebay_lanes._fetch(self.db, config, payload)
            args, kwargs = factory.return_value.search_page.call_args
        self.assertEqual(args[0], "livre photographie")
        self.assertIsNone(kwargs["category_ids"])

    def test_oxfam_broad_uses_newest_first_and_silent_baseline(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True, source_oxfam_broad=True)
        self.assertTrue(schedule_oxfam_broad(self.db, config))
        job = claim_job(self.db, "oxfam-broad-test", kinds=("SCAN_OXFAM_BROAD",))
        payload = {"searchEventSummary": {"resultsSummary": [{"sort": {"sortKeys": [{"attribute": "product.creationDate", "direction": "desc"}]},
                   "totalMatchingRecords": 1, "records": [{"sku.listingId": "HD_123", "product.displayName": "Danny Lyon photobook", "sku.activePrice": "20", "product.route": "/bikeriders/product/HD_123"}]}]}}
        result = run_oxfam_broad_job(self.db, job, config, fetch=lambda *_: payload)
        self.assertEqual(result["items"], 1)
        self.assertEqual(self.db.execute("SELECT imported FROM listings WHERE platform='oxfam'").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='TRIAGE'").fetchone()[0], 0)

    def test_research_sweeps_have_three_cadenced_schedules(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True,
                        research_provider="codex_cli", research_recurring_enabled=True,
                        source_wider_web=True, source_publishers=True, source_prizes=True)
        self.assertEqual(schedule_research(self.db, config), 3)
        self.assertEqual(schedule_research(self.db, config), 0)
        wider = self.db.execute("SELECT payload_json FROM jobs WHERE kind='RESEARCH_SWEEP' AND route_id='research-wider'").fetchone()
        self.assertEqual(json.loads(wider[0])["market"], "Biblio")
        with transaction(self.db):
            enqueue_job(self.db, "lead-backlog", "RESEARCH_LEAD", priority=130)
        job = claim_job(self.db, "research-test", kinds=("RESEARCH_LEAD", "RESEARCH_SWEEP"), lease_seconds=180)
        self.assertEqual(job["kind"], "RESEARCH_SWEEP")
        result = {"items": [{"title": "New photobook", "url": "https://www.biblio.com/book/123456789",
                             "source_name": "Biblio", "why": "Photographer's book", "published_at": "", "price_amount": None, "currency": ""}]}
        outcome = run_research_job(self.db, job, config, provider=lambda *_: result, link_check=lambda *_: True)
        self.assertEqual(outcome["validated"], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM research_sweeps WHERE status='DONE'").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT imported FROM listings").fetchone()[0], 1)

    def test_wider_web_rotates_sites_every_half_hour(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True,
                        research_provider="codex_cli", research_recurring_enabled=True, source_wider_web=True)
        self.assertEqual(schedule_research(self.db, config), 1)
        first = claim_job(self.db, "research-test", kinds=("RESEARCH_SWEEP",), lease_seconds=180)
        self.assertEqual(json.loads(first["payload_json"])["market"], "Biblio")
        run_research_job(self.db, first, config, provider=lambda *_: {"items": []})
        finish_job(self.db, first["id"], first["lease_token"])
        with transaction(self.db):
            self.db.execute("UPDATE source_routes SET next_due_at='2000-01-01T00:00:00Z' WHERE id='research-wider'")
        self.assertEqual(schedule_research(self.db, config), 1)
        second = self.db.execute("SELECT payload_json FROM jobs WHERE kind='RESEARCH_SWEEP' AND status='PENDING'").fetchone()
        self.assertEqual(json.loads(second[0])["market"], "viaLibri")
        self.assertEqual(self.db.execute("SELECT cadence_seconds FROM sources WHERE id='research-wider'").fetchone()[0], 1800)

    def test_old_local_research_limit_does_not_block_wider_sweep(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True,
                        research_provider="codex_cli", research_recurring_enabled=True,
                        source_wider_web=True)
        with transaction(self.db):
            self.db.execute("INSERT INTO sources(id,adapter,status,last_error) VALUES('research-wider','codex-web','DEGRADED','Daily Codex research job limit reached')")
        self.assertEqual(schedule_research(self.db, config), 1)
        row = self.db.execute("SELECT status,last_error FROM sources WHERE id='research-wider'").fetchone()
        self.assertEqual(tuple(row), ("ACTIVE", None))

    def test_bargain_alert_requires_live_copy_and_checked_market_comparables(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True,
                        allow_real_notifications=True, notification_enabled=True,
                        research_provider="codex_cli", research_recurring_enabled=True)
        with transaction(self.db):
            listing_id = capture(self.db, {"key": "ebay:123456789012", "title": "Danny Lyon The Bikeriders first printing 1968", "price_gbp": "20",
                                           "url": "https://www.ebay.co.uk/itm/123456789012"}, source_id="ebay", origin_key="lead")
            observation = self.db.execute("SELECT current_observation_id FROM listings WHERE id=?", (listing_id,)).fetchone()[0]
            enqueue_job(self.db, "research:lead", "RESEARCH_LEAD", listing_id=listing_id, payload={"observation_id": observation})
        job = claim_job(self.db, "research-test", kinds=("RESEARCH_LEAD",), lease_seconds=180)
        sold_date = datetime.now(timezone.utc).date().isoformat()
        comps = [{"url": "https://www.ebay.co.uk/itm/200000000001", "kind": "SOLD", "price_gbp": 200,
                  "sold_date": sold_date, "same_edition": True, "condition_no_better": True, "note": "Same printing, worn jacket"},
                 {"url": "https://www.abaa.org/book/200000000002", "kind": "ASKING", "price_gbp": 180,
                  "sold_date": "", "same_edition": True, "condition_no_better": True, "note": "Same printing, similar wear"}]
        result = {"decision": "GEM", "actual_book": True, "collector_fit": True, "edition_supported": True,
                  "context": "Danny Lyon's documentary book is a key work.",
                  "opportunity_reason": "The seller identifies the early printing, far below two like-for-like market pages.",
                  "edition_note": "Seller identifies the first printing.", "risk": "Inspect title page and condition.",
                  "source_urls": ["https://www.moma.org/books/bikeriders"], "market_comparables": comps}
        with self.assertRaises(ResearchDeferred):
            run_lead_research(self.db, job, config, provider=lambda *_: result, link_check=lambda *_: True)
        with transaction(self.db):
            self.db.execute("INSERT INTO live_checks(listing_id,observation_id,provider,checked_at,availability,price_minor,currency,result_json) VALUES(?,?,?,?,?,?,?,?)",
                            (listing_id, observation, "ebay_browse", now(), "LIVE", 2000, "GBP", "{}"))
        self.assertEqual(run_lead_research(self.db, job, config, provider=lambda *_: result, link_check=lambda *_: True,
                                           market_check=lambda *_: True)["status"], "DONE")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notification_events WHERE stage='BARGAIN_FIND'").fetchone()[0], 1)
        self.assertTrue(send_one(self.db, config, fake=True))
        with transaction(self.db):
            capture(self.db, {"key": "ebay:123456789012", "title": "Danny Lyon The Bikeriders first printing 1968",
                              "price_gbp": "19", "url": "https://www.ebay.co.uk/itm/123456789012"},
                    source_id="ebay", origin_key="lead-price-change")
            newer_observation = self.db.execute("SELECT current_observation_id FROM listings WHERE id=?", (listing_id,)).fetchone()[0]
            enqueue_job(self.db, "research:lead:new-price", "RESEARCH_LEAD", listing_id=listing_id,
                        payload={"observation_id": newer_observation})
            self.db.execute("INSERT INTO live_checks(listing_id,observation_id,provider,checked_at,availability,price_minor,currency,result_json) VALUES(?,?,?,?,?,?,?,?)",
                            (listing_id, newer_observation, "ebay_browse", now(), "LIVE", 1900, "GBP", "{}"))
        newer_job = claim_job(self.db, "research-test", kinds=("RESEARCH_LEAD",), lease_seconds=180)
        self.assertEqual(run_lead_research(self.db, newer_job, config, provider=lambda *_: result, link_check=lambda *_: True,
                                           market_check=lambda *_: True)["status"], "DONE")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notification_events WHERE stage='BARGAIN_FIND'").fetchone()[0], 1)

    def test_research_rejects_generic_or_unsupported_find(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True,
                        allow_real_notifications=True, notification_enabled=True,
                        research_provider="codex_cli", research_recurring_enabled=True)
        with transaction(self.db):
            listing_id = capture(self.db, {"key": "ebay:123456789012", "title": "Danny Lyon The Bikeriders", "price_gbp": "20",
                                           "url": "https://www.ebay.co.uk/itm/123456789012"}, source_id="ebay", origin_key="lead")
            observation = self.db.execute("SELECT current_observation_id FROM listings WHERE id=?", (listing_id,)).fetchone()[0]
            enqueue_job(self.db, "research:lead", "RESEARCH_LEAD", listing_id=listing_id, payload={"observation_id": observation})
            self.db.execute("INSERT INTO live_checks(listing_id,observation_id,provider,checked_at,availability,price_minor,currency,result_json) VALUES(?,?,?,?,?,?,?,?)",
                            (listing_id, observation, "ebay_browse", now(), "LIVE", 2000, "GBP", "{}"))
        job = claim_job(self.db, "research-test", kinds=("RESEARCH_LEAD",), lease_seconds=180)
        with transaction(self.db):
            self.db.execute("INSERT INTO sources(id,adapter,status) VALUES('research-leads','codex-web','ACTIVE')")
            self.db.executemany("INSERT INTO research_sweeps(source_id,job_id,started_at,provider,model,status) VALUES('research-leads',?,?,'codex_cli','gpt-6-sol','DONE')",
                                [(job["id"], now())] * 48)
        result = {"decision": "PASS", "actual_book": True, "collector_fit": False, "edition_supported": False,
                  "context": "Routine reprint.", "opportunity_reason": "No collector opportunity.",
                  "edition_note": "Later edition.", "risk": "Common copy.", "source_urls": ["https://www.moma.org/books/bikeriders"],
                  "market_comparables": []}
        self.assertEqual(run_lead_research(self.db, job, config, provider=lambda *_: result, link_check=lambda *_: True)["status"], "REJECTED")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0], 0)

    def test_duplicate_search_result_reuses_review_but_price_and_condition_changes_do_not(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True,
                        research_provider="codex_cli", research_recurring_enabled=True)
        result = {"decision": "PASS", "actual_book": True, "collector_fit": False, "edition_supported": False,
                  "context": "This is a routine printing of a documentary book.",
                  "opportunity_reason": "The seller has not identified a collectible edition.",
                  "edition_note": "Printing unknown.", "risk": "Do not assume it is a first printing.",
                  "source_urls": [], "market_comparables": []}
        calls = []
        def provider(*_):
            calls.append(1)
            return result
        def review(number, price, condition):
            with transaction(self.db):
                listing_id = capture(self.db, {"key": "ebay:123456789012", "title": "Danny Lyon The Bikeriders",
                                               "price_gbp": price, "condition": condition,
                                               "source_name": f"route-{number}",
                                               "url": f"https://www.ebay.co.uk/itm/123456789012?_skw=route{number}"},
                                     source_id="ebay", origin_key=f"route-{number}")
                observation = self.db.execute("SELECT current_observation_id FROM listings WHERE id=?", (listing_id,)).fetchone()[0]
                enqueue_job(self.db, f"research:route-{number}", "RESEARCH_LEAD", listing_id=listing_id,
                            payload={"observation_id": observation})
                self.db.execute("INSERT INTO live_checks(listing_id,observation_id,provider,checked_at,availability,price_minor,currency,result_json) VALUES(?,?,?,?,?,?,?,?)",
                                (listing_id, observation, "ebay_browse", now(), "LIVE", int(Decimal(price) * 100), "GBP", "{}"))
            job = claim_job(self.db, "research-test", kinds=("RESEARCH_LEAD",))
            outcome = run_lead_research(self.db, job, config, provider=provider)
            finish_job(self.db, job["id"], job["lease_token"])
            return outcome
        self.assertEqual(review(1, "20", "Used: Good")["status"], "REJECTED")
        self.assertTrue(review(2, "20", "Used: Good")["reused"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(review(3, "19", "Used: Good")["status"], "REJECTED")
        self.assertEqual(review(4, "19", "Used: Very Good")["status"], "REJECTED")
        self.assertEqual(len(calls), 3)

    def test_new_observation_cancels_superseded_pending_research(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True,
                        research_provider="codex_cli", research_recurring_enabled=True)
        with transaction(self.db):
            listing_id = capture(self.db, {"key": "ebay:123456789012", "title": "Danny Lyon The Bikeriders",
                                           "price_gbp": "20", "url": "https://www.ebay.co.uk/itm/123456789012"},
                                 source_id="ebay", origin_key="route-one")
            enqueue_job(self.db, "triage:route-one", "TRIAGE", listing_id=listing_id)
        run_triage(self.db, claim_job(self.db, "test", kinds=("TRIAGE",)), config)
        with transaction(self.db):
            capture(self.db, {"key": "ebay:123456789012", "title": "Danny Lyon The Bikeriders",
                              "price_gbp": "19", "url": "https://www.ebay.co.uk/itm/123456789012"},
                    source_id="ebay", origin_key="route-two")
            enqueue_job(self.db, "triage:route-two", "TRIAGE", listing_id=listing_id)
        run_triage(self.db, claim_job(self.db, "test", kinds=("TRIAGE",)), config)
        states = [row[0] for row in self.db.execute(
            "SELECT status FROM jobs WHERE kind='RESEARCH_LEAD' AND listing_id=? ORDER BY id", (listing_id,))]
        self.assertEqual(states, ["CANCELLED", "PENDING"])

    def test_unproven_gem_stays_off_telegram(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True,
                        allow_real_notifications=True, notification_enabled=True,
                        research_provider="codex_cli", research_recurring_enabled=True)
        with transaction(self.db):
            listing_id = capture(self.db, {"key": "ebay:123456789012", "title": "Danny Lyon The Bikeriders", "price_gbp": "20",
                                           "url": "https://www.ebay.co.uk/itm/123456789012"}, source_id="ebay", origin_key="lead")
            observation = self.db.execute("SELECT current_observation_id FROM listings WHERE id=?", (listing_id,)).fetchone()[0]
            enqueue_job(self.db, "research:lead", "RESEARCH_LEAD", listing_id=listing_id, payload={"observation_id": observation})
            self.db.execute("INSERT INTO live_checks(listing_id,observation_id,provider,checked_at,availability,price_minor,currency,result_json) VALUES(?,?,?,?,?,?,?,?)",
                            (listing_id, observation, "ebay_browse", now(), "LIVE", 2000, "GBP", "{}"))
        job = claim_job(self.db, "research-test", kinds=("RESEARCH_LEAD",), lease_seconds=180)
        result = {"decision": "GEM", "actual_book": True, "collector_fit": True, "edition_supported": False,
                  "context": "This is a major documentary photobook.",
                  "opportunity_reason": "A cheap copy, but the actual printing is not identified by the seller.",
                  "edition_note": "Printing unknown.", "risk": "It could be a routine reprint.",
                  "source_urls": ["https://www.moma.org/books/bikeriders"], "market_comparables": []}
        outcome = run_lead_research(self.db, job, config, provider=lambda *_: result, link_check=lambda *_: True)
        self.assertEqual((outcome["status"], outcome["verdict"]), ("NEEDS_EVIDENCE", "PAY_ATTENTION"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notification_events WHERE stage='BARGAIN_FIND'").fetchone()[0], 0)

    def test_non_ebay_book_can_be_researched_and_alerted_from_recent_feed(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_real_notifications=True,
                        notification_enabled=True, research_provider="codex_cli", research_recurring_enabled=True)
        item = {"key": "specialist:123", "title": "Danny Lyon The Bikeriders", "price_gbp": "35",
                "available": True, "url": "https://bookshop.thephotographersgallery.org.uk/products/the-bikeriders"}
        with transaction(self.db):
            listing_id = capture(self.db, item, source_id="specialist", origin_key="fresh-shop", imported=False)
            observation = self.db.execute("SELECT current_observation_id FROM listings WHERE id=?", (listing_id,)).fetchone()[0]
            enqueue_job(self.db, "research:shop", "RESEARCH_LEAD", listing_id=listing_id,
                        payload={"observation_id": observation})
        job = claim_job(self.db, "research-test", kinds=("RESEARCH_LEAD",), lease_seconds=180)
        result = {"decision": "GEM", "actual_book": True, "collector_fit": True, "edition_supported": True,
                  "context": "Danny Lyon's Bikeriders is a major documentary photobook.",
                  "opportunity_reason": "The seller's identified printing is far below a matched sold copy and a live offer.",
                  "edition_note": "Printing identified by seller.", "risk": "Confirm availability and edition with the seller.",
                  "source_urls": [], "market_comparables": [
                      {"url": "https://www.ebay.co.uk/itm/200000000003", "kind": "SOLD", "price_gbp": 240,
                       "sold_date": datetime.now(timezone.utc).date().isoformat(), "same_edition": True,
                       "condition_no_better": True, "note": "Similar copy"},
                      {"url": "https://www.abaa.org/book/200000000004", "kind": "ASKING", "price_gbp": 220,
                       "sold_date": "", "same_edition": True, "condition_no_better": True, "note": "Same printing"}]}
        outcome = run_lead_research(self.db, job, config, provider=lambda *_: result, link_check=lambda *_: False,
                                    market_check=lambda *_: True)
        self.assertEqual((outcome["status"], outcome["verdict"]), ("DONE", "GEM"))
        self.assertTrue(send_one(self.db, config, fake=True))

    def test_unfamiliar_signed_photobook_is_researched(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True,
                        research_provider="codex_cli", research_recurring_enabled=True)
        item = {"key": "ebay:123456789012", "title": "Signed documentary photobook by an unfamiliar artist",
                "price_gbp": "120", "url": "https://www.ebay.co.uk/itm/123456789012"}
        with transaction(self.db):
            listing_id = capture(self.db, item, source_id="ebay", origin_key="unfamiliar", imported=False)
            enqueue_job(self.db, "triage:unfamiliar", "TRIAGE", listing_id=listing_id)
        run_triage(self.db, claim_job(self.db, "test", kinds=("TRIAGE",)), config)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='RESEARCH_LEAD'").fetchone()[0], 1)

    def test_codex_account_limit_defers_without_exhausting_job_attempts(self):
        from photobook_radar import research_sweeps, worker
        config = Config(data_dir=Path(self.folder.name), mode="production", research_provider="codex_cli",
                        research_recurring_enabled=True)
        with transaction(self.db):
            listing_id = capture(self.db, {"key": "ebay:123456789012", "title": "Danny Lyon The Bikeriders"},
                                 source_id="ebay", origin_key="limit-test", imported=False)
            enqueue_job(self.db, "research:limit-test", "RESEARCH_LEAD", listing_id=listing_id,
                        payload={"observation_id": self.db.execute("SELECT current_observation_id FROM listings WHERE id=?", (listing_id,)).fetchone()[0]})
        job = claim_job(self.db, "limit-test", kinds=("RESEARCH_LEAD",))
        with patch.object(research_sweeps.shutil, "which", return_value="/usr/bin/true"), patch.object(
            research_sweeps.subprocess, "run", return_value=type("Completed", (), {"returncode": 1, "stderr": "Usage limit reached"})()
        ):
            with self.assertRaises(ResearchDeferred) as raised:
                research_sweeps._codex(config, "prompt")
        self.assertTrue(raised.exception.provider_limited)
        with patch.object(worker, "run_lead_research", side_effect=raised.exception):
            worker._run_research_job(config, job["id"], job["lease_token"])
        state = self.db.execute("SELECT status,attempts,last_error FROM jobs WHERE id=?", (job["id"],)).fetchone()
        self.assertEqual((state["status"], state["attempts"]), ("PENDING", 0))
        self.assertIn("Codex account usage limit", state["last_error"])
        pause = self.db.execute("SELECT value FROM health WHERE key='codex_research_backoff_until'").fetchone()
        self.assertGreater(pause[0], now())

    def test_codex_lead_schema_accepts_context_response(self):
        from photobook_radar import research_sweeps
        response = {"decision": "PASS", "actual_book": False, "collector_fit": False, "edition_supported": False,
                    "context": "A short verified context.", "opportunity_reason": "No particular opportunity.",
                    "edition_note": "Edition unknown.", "risk": "Inspect condition.", "source_urls": [],
                    "market_comparables": []}
        def fake_run(command, **kwargs):
            Path(command[command.index("-o") + 1]).write_text(__import__("json").dumps(response))
            return type("Completed", (), {"returncode": 0})()
        with patch.object(research_sweeps.shutil, "which", return_value="/usr/bin/true"), patch.object(research_sweeps.subprocess, "run", side_effect=fake_run):
            self.assertEqual(research_sweeps._codex(Config(data_dir=Path(self.folder.name)), "prompt", schema=research_sweeps.LEAD_SCHEMA), response)

    def test_fresh_start_cancels_historical_backlog_without_deleting_archive(self):
        from photobook_radar import cli
        with transaction(self.db):
            listing_id = capture(self.db, {"key": "ebay:123456789012", "title": "Old book"}, source_id="ebay", origin_key="old", imported=True)
            enqueue_job(self.db, "triage:old", "TRIAGE", listing_id=listing_id)
        with patch.object(cli, "load_config", return_value=Config(data_dir=Path(self.folder.name))):
            cli.start_fresh()
            cli.start_fresh()
        self.assertEqual(self.db.execute("SELECT status FROM jobs WHERE job_key='triage:old'").fetchone()[0], "CANCELLED")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM listings").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM app_events WHERE kind='FRESH_START'").fetchone()[0], 1)

    def test_exact_check_records_live_price_separately_and_suppresses_expired_lead(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True)
        with transaction(self.db):
            listing_id = capture(self.db, {"key": "ebay:123456789012", "rest_item_id": "v1|123456789012|0", "title": "Danny Lyon Bikeriders", "price_gbp": "20", "url": "https://www.ebay.co.uk/itm/123456789012"}, source_id="ebay", origin_key="new", imported=False)
            observation_id = self.db.execute("SELECT current_observation_id FROM listings WHERE id=?", (listing_id,)).fetchone()[0]
            enqueue_job(self.db, "verify:one", "VERIFY", listing_id=listing_id, payload={"observation_id": observation_id})
            enqueue_notification(self.db, listing_id=listing_id, stage="FAST_LEAD", material_version="initial", channel="telegram", payload={"message": "Potential lead"})
        job = claim_job(self.db, "verify-test", kinds=("VERIFY",))
        detail = {"itemId": "v1|123456789012|0", "title": "Danny Lyon Bikeriders", "price": {"value": "25", "currency": "GBP"}, "itemWebUrl": "https://www.ebay.co.uk/itm/123456789012", "buyingOptions": ["FIXED_PRICE"], "estimatedAvailabilityStatus": "OUT_OF_STOCK"}
        result = run_verify(self.db, job, config, lookup=lambda *_: (False, "out of stock", detail))
        self.assertEqual(result["availability"], "UNAVAILABLE")
        self.assertEqual(self.db.execute("SELECT price_minor FROM observations WHERE id=?", (observation_id,)).fetchone()[0], 2000)
        self.assertEqual(self.db.execute("SELECT price_minor FROM live_checks").fetchone()[0], 2500)
        self.assertEqual(self.db.execute("SELECT status FROM notification_events").fetchone()[0], "SUPPRESSED")
        self.assertEqual(self.db.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()[0], "DONE")

    def test_mismatched_exact_check_cannot_mark_listing_live(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True)
        with transaction(self.db):
            listing_id = capture(self.db, {"key": "ebay:123456789012", "title": "Photobook", "price_gbp": "20"}, source_id="ebay", origin_key="new", imported=False)
            observation_id = self.db.execute("SELECT current_observation_id FROM listings WHERE id=?", (listing_id,)).fetchone()[0]
            enqueue_job(self.db, "verify:one", "VERIFY", listing_id=listing_id, payload={"observation_id": observation_id})
        job = claim_job(self.db, "verify-test", kinds=("VERIFY",))
        detail = {"itemId": "v1|999999999999|0", "title": "Wrong item", "price": {"value": "10", "currency": "GBP"}}
        with self.assertRaisesRegex(ValueError, "does not match"):
            run_verify(self.db, job, config, lookup=lambda *_: (True, "live", detail))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM live_checks").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT availability FROM listings WHERE id=?", (listing_id,)).fetchone()[0], "UNKNOWN")

    def test_research_uses_seller_detail_from_exact_ebay_check(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True,
                        research_provider="codex_cli", research_recurring_enabled=True)
        with transaction(self.db):
            listing_id = capture(self.db, {"key": "ebay:123456789012", "title": "Danny Lyon The Bikeriders",
                                           "price_gbp": "20", "url": "https://www.ebay.co.uk/itm/123456789012"},
                                 source_id="ebay", origin_key="seller-detail")
            observation = self.db.execute("SELECT current_observation_id FROM listings WHERE id=?", (listing_id,)).fetchone()[0]
            enqueue_job(self.db, "verify:seller-detail", "VERIFY", listing_id=listing_id,
                        payload={"observation_id": observation})
            enqueue_job(self.db, "research:seller-detail", "RESEARCH_LEAD", listing_id=listing_id,
                        payload={"observation_id": observation})
        detail = {"itemId": "v1|123456789012|0", "title": "Danny Lyon The Bikeriders",
                  "price": {"value": "20", "currency": "GBP"},
                  "itemWebUrl": "https://www.ebay.co.uk/itm/123456789012",
                  "buyingOptions": ["FIXED_PRICE"], "condition": "Very Good",
                  "description": "<style>ignore this CSS</style><p>Signed first edition with dust jacket.</p>",
                  "localizedAspects": [{"name": "Edition", "value": "First Edition"},
                                       {"name": "Publisher", "value": "Macmillan"}]}
        run_verify(self.db, claim_job(self.db, "test", kinds=("VERIFY",)), config,
                   lookup=lambda *_: (True, "live", detail))
        checked = json.loads(self.db.execute("SELECT result_json FROM live_checks WHERE listing_id=?", (listing_id,)).fetchone()[0])
        self.assertEqual(checked["seller_description"], "Signed first edition with dust jacket.")
        self.assertEqual(checked["seller_aspects"]["Edition"], "First Edition")
        prompts = []
        def provider(_, prompt):
            prompts.append(prompt)
            return {"decision": "PASS", "actual_book": True, "collector_fit": False,
                    "edition_supported": False, "context": "Ordinary copy.",
                    "opportunity_reason": "No verified resale margin.", "edition_note": "Seller claim.",
                    "risk": "Confirm the printing.", "source_urls": [], "market_comparables": []}
        run_lead_research(self.db, claim_job(self.db, "test", kinds=("RESEARCH_LEAD",)), config,
                          provider=provider)
        self.assertIn("Signed first edition with dust jacket.", prompts[0])
        self.assertIn('"seller_condition": "Very Good"', prompts[0])
        self.assertIn('"Edition": "First Edition"', prompts[0])
        self.assertNotIn("ignore this CSS", prompts[0])

    def test_fresh_capture_waits_for_research_before_phone_event(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_real_notifications=True,
                        notification_enabled=True, research_provider="codex_cli", research_recurring_enabled=True)
        item = {"key": "ebay:123456789012", "title": "The Bikeriders Danny Lyon first printing 1968", "price_gbp": "20", "url": "https://www.ebay.co.uk/itm/123456789012"}
        with transaction(self.db):
            listing_id = capture(self.db, item, source_id="ebay", origin_key="fresh", imported=False)
            enqueue_job(self.db, f"triage:{listing_id}", "TRIAGE", listing_id=listing_id)
        job = claim_job(self.db, "test", kinds=("TRIAGE",))
        run_triage(self.db, job, config)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0], 0)
        self.assertFalse(send_one(self.db, config, fake=True))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='RESEARCH_LEAD'").fetchone()[0], 1)

    def test_imported_stock_is_silent(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_real_notifications=True, notification_enabled=True)
        item = {"key": "ebay:123456789012", "title": "The Bikeriders Danny Lyon first printing 1968", "price_gbp": "20", "url": "https://www.ebay.co.uk/itm/123456789012"}
        with transaction(self.db):
            listing_id = capture(self.db, item, source_id="ebay", origin_key="imported", imported=True)
            enqueue_job(self.db, f"triage:{listing_id}", "TRIAGE", listing_id=listing_id)
        run_triage(self.db, claim_job(self.db, "test", kinds=("TRIAGE",)), config)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0], 0)

    def test_generic_cheap_photo_book_is_retained_without_phone_alert(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_real_notifications=True, notification_enabled=True)
        with transaction(self.db):
            listing_id = capture(self.db, {"key": "ebay:123456789012", "title": "Bath Abbey Britain in Old Photographs",
                                           "price_gbp": "5.98", "url": "https://www.ebay.co.uk/itm/123456789012"},
                                 source_id="ebay", origin_key="generic", imported=False)
            enqueue_job(self.db, "triage:generic", "TRIAGE", listing_id=listing_id)
        run_triage(self.db, claim_job(self.db, "test", kinds=("TRIAGE",)), config)
        self.assertEqual(self.db.execute("SELECT triage_score FROM listings WHERE id=?", (listing_id,)).fetchone()[0], 59)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0], 0)

    def test_unknown_photobook_retained_but_above_cap_or_ended_is_not_actionable(self):
        self.assertGreaterEqual(score({"title": "Unidentified documentary photobook", "price_gbp": "20"})["score"], 58)
        config = Config(data_dir=Path(self.folder.name))
        for suffix, price, end in (("expensive", "151", None), ("ended", "20", "2020-01-01T10:00:00Z")):
            item = {"key": "ebay:" + ("123456789012" if suffix == "expensive" else "123456789013"), "title": "Unidentified documentary photobook", "price_gbp": price}
            if end:
                item.update({"item_end_date": end, "buying_options": ["AUCTION"]})
            with transaction(self.db):
                listing_id = capture(self.db, item, source_id="ebay", origin_key=suffix, imported=True)
                enqueue_job(self.db, f"triage:{listing_id}", "TRIAGE", listing_id=listing_id)
            run_triage(self.db, claim_job(self.db, "test", kinds=("TRIAGE",)), config)
            row = self.db.execute("SELECT verdict,availability FROM listings WHERE id=?", (listing_id,)).fetchone()
            self.assertIsNone(row["verdict"])
            if end:
                self.assertEqual(row["availability"], "ENDED")

    def test_expired_lease_recovered_and_stale_worker_rejected(self):
        with transaction(self.db):
            enqueue_job(self.db, "triage:1", "TRIAGE")
        old = claim_job(self.db, "old", kinds=("TRIAGE",))
        with transaction(self.db):
            self.db.execute("UPDATE jobs SET lease_until='2020-01-01T00:00:00Z' WHERE id=?", (old["id"],))
        new = claim_job(self.db, "new", kinds=("TRIAGE",))
        self.assertEqual(new["id"], old["id"])
        self.assertNotEqual(new["lease_token"], old["lease_token"])
        with self.assertRaises(RuntimeError):
            finish_job(self.db, old["id"], old["lease_token"])
        finish_job(self.db, new["id"], new["lease_token"])
        self.assertEqual(self.db.execute("SELECT status FROM jobs WHERE id=?", (new["id"],)).fetchone()[0], "DONE")

    def test_capture_page_rolls_back_on_bad_item(self):
        with self.assertRaises(ValueError):
            capture_page(self.db, source_id="ebay", route_id="route", window_id="window", page_number=1, continuation={"next": "page2"}, complete=False, items=[{"key": "ebay:123456789012", "title": "Valid"}, {"key": "ebay:123456789013"}])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM search_windows").fetchone()[0], 0)

    def test_cancelled_source_job_cannot_commit_a_late_page(self):
        with transaction(self.db):
            enqueue_job(self.db, "scan:late", "SCAN_OXFAM")
        job = claim_job(self.db, "old-source", kinds=("SCAN_OXFAM",))
        with transaction(self.db):
            self.db.execute("UPDATE jobs SET status='CANCELLED',lease_token=NULL,lease_until=NULL WHERE id=?", (job["id"],))
        with self.assertRaisesRegex(RuntimeError, "Stale source lease"):
            capture_page(self.db, source_id="oxfam-photography", route_id="oxfam-photography", window_id="late-window", page_number=1, continuation={}, complete=True, items=[{"sku": "HD_123", "title": "Late book"}], lease_job_id=job["id"], lease_token=job["lease_token"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 0)

    def test_new_observation_is_screened_after_prior_job_completed(self):
        item = {"key": "ebay:123456789012", "title": "The Bikeriders Danny Lyon", "price_gbp": "20"}
        config = Config(data_dir=Path(self.folder.name))
        ids = capture_page(self.db, source_id="ebay", route_id="route", window_id="window", page_number=1, continuation={}, complete=True, items=[item])
        run_triage(self.db, claim_job(self.db, "first", kinds=("TRIAGE",)), config)
        self.assertIsNotNone(self.db.execute("SELECT triage_score FROM listings WHERE id=?", (ids[0],)).fetchone()[0])
        capture_page(self.db, source_id="ebay", route_id="route", window_id="window2", page_number=1, continuation={}, complete=True, items=[{**item, "title": "Revised camera manual"}])
        self.assertEqual(self.db.execute("SELECT processing FROM listings WHERE id=?", (ids[0],)).fetchone()[0], "CAPTURED")
        next_job = claim_job(self.db, "second", kinds=("TRIAGE",))
        self.assertIsNotNone(next_job)
        run_triage(self.db, next_job, config)
        row = self.db.execute("SELECT processing,title FROM listings WHERE id=?", (ids[0],)).fetchone()
        self.assertEqual((row["processing"], row["title"]), ("TRIAGED", "Revised camera manual"))

    def test_same_seller_visible_snapshot_does_not_requeue_or_repeat_alert(self):
        item = {"key": "ebay:123456789012", "title": "The Bikeriders Danny Lyon", "price_gbp": "20", "url": "https://www.ebay.co.uk/itm/123456789012"}
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_real_notifications=True, notification_enabled=True)
        capture_page(self.db, source_id="ebay", route_id="route", window_id="window", page_number=1, continuation={}, complete=True, items=[item])
        run_triage(self.db, claim_job(self.db, "first", kinds=("TRIAGE",)), config)
        capture_page(self.db, source_id="ebay", route_id="route", window_id="window2", page_number=1, continuation={}, complete=True, items=[{**item, "context": "different search query"}])
        self.assertIsNone(claim_job(self.db, "second", kinds=("TRIAGE",)))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0], 0)

    def test_description_only_book_mentions_do_not_enter_research_queue(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True,
                        research_provider="codex_cli", research_recurring_enabled=True)
        examples = [
            ("Copy, Tweak, Paste", "Discusses Ed Ruscha's Twentysix Gasoline Stations"),
            ("Women Seeing Women", "Includes a mention of Clementina, Lady Hawarden by Virginia Dodier"),
            ("Master Photographers", "Martin Parr is mentioned in the description"),
        ]
        for index, (title, description) in enumerate(examples):
            item = {"key": f"ebay:{123456789012 + index}", "title": title, "description": description,
                    "price_gbp": "20", "url": f"https://www.ebay.co.uk/itm/{123456789012 + index}"}
            self.assertFalse(object_in_seller_title(item, score(item)))
            with transaction(self.db):
                listing_id = capture(self.db, item, source_id="ebay", origin_key=f"false-positive-{index}", imported=False)
                enqueue_job(self.db, f"triage:false-positive-{index}", "TRIAGE", listing_id=listing_id)
            run_triage(self.db, claim_job(self.db, "test", kinds=("TRIAGE",)), config)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='RESEARCH_LEAD'").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0], 0)

    def test_plain_work_title_with_photographer_in_opening_is_researched(self):
        item = {"title": "The End Sends Advance Warning",
                "description": "The End Sends Advance Warning by Todd Hido. Limited, signed and numbered photobook.",
                "price_gbp": "700"}
        result = score(item)
        self.assertFalse(object_in_seller_title(item, result))
        self.assertTrue(research_candidate(item, result))

    def test_obsolete_screening_job_cannot_overwrite_newer_capture(self):
        item = {"key": "ebay:123456789012", "title": "The Bikeriders Danny Lyon", "price_gbp": "20"}
        config = Config(data_dir=Path(self.folder.name))
        ids = capture_page(self.db, source_id="ebay", route_id="route", window_id="window", page_number=1, continuation={}, complete=True, items=[item])
        old_job = claim_job(self.db, "old", kinds=("TRIAGE",))
        capture_page(self.db, source_id="ebay", route_id="route", window_id="window2", page_number=1, continuation={}, complete=True, items=[{**item, "title": "Revised camera manual"}])
        self.assertEqual(run_triage(self.db, old_job, config), {"stale_observation": True})
        row = self.db.execute("SELECT processing,title FROM listings WHERE id=?", (ids[0],)).fetchone()
        self.assertEqual((row["processing"], row["title"]), ("CAPTURED", "Revised camera manual"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs WHERE status='PENDING'").fetchone()[0], 1)

    def test_quota_provider_remaining_decreases_for_each_attempt_and_timeout(self):
        opts = dict(bucket="browse", window_start="2026-09-28T00:00:00Z", reset_at="2099-09-29T00:00:00Z", provider_limit=5000, reserve=650, lane_cap=None, route_id="private", reason="search", provider_remaining=652)
        first = reserve_request(self.db, **opts)
        settle_request(self.db, first, response_class="timeout", uncertain=True)
        second = reserve_request(self.db, **opts)
        settle_request(self.db, second, response_class="200")
        with self.assertRaisesRegex(RuntimeError, "reserve protected"):
            reserve_request(self.db, **opts)
        window = self.db.execute("SELECT consumed,reserved,uncertain,provider_remaining FROM api_windows").fetchone()
        self.assertEqual(tuple(window), (1, 0, 1, 650))

    def test_second_connection_cannot_reserve_last_protected_request(self):
        other = connect(Path(self.folder.name) / "radar.db", existing=True)
        opts = dict(bucket="browse", window_start="2026-09-28T00:00:00Z", reset_at="2099-09-29T00:00:00Z", provider_limit=2, reserve=1, lane_cap=None, route_id="private", reason="search")
        try:
            reserve_request(self.db, **opts)
            with self.assertRaisesRegex(RuntimeError, "reserve protected"):
                reserve_request(other, **opts)
        finally:
            other.close()

    def test_endgame_cap_is_shared_across_its_routes(self):
        opts = dict(bucket="browse", window_start="2026-09-28T00:00:00Z", reset_at="2099-09-29T00:00:00Z", provider_limit=5000, reserve=650, lane_cap=1, reason="search")
        reserve_request(self.db, route_id="endgame:GB", **opts)
        with self.assertRaisesRegex(RuntimeError, "Route API cap"):
            reserve_request(self.db, route_id="endgame:US", **opts)

    def test_metered_ebay_transport_records_each_physical_attempt(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True)
        gateway = MeteredEbayBrowseClient(self.db, config, route_id="private", client_id="test-id", client_secret="test-secret")
        gateway._access_token = "fake-token"
        gateway._token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        gateway.browse_window = BrowseWindow(
            "2026-09-28T00:00:00Z", "2099-09-29T00:00:00Z", 5000, config.ebay_reserve + 2,
            datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        )
        reply = MagicMock()
        reply.__enter__.return_value = reply
        reply.status = 200
        reply.read.return_value = b'{"itemSummaries": []}'
        with patch("photobook_radar.ebay_gateway.urllib.request.urlopen", side_effect=[urllib.error.URLError("timeout"), reply]) as opener:
            with self.assertRaisesRegex(Exception, "charged conservatively"):
                gateway.search_page("photobook", limit=1)
            self.assertEqual(gateway.search_page("photobook", limit=1)["itemSummaries"], [])
            with self.assertRaisesRegex(RuntimeError, "reserve protected"):
                gateway.search_page("photobook", limit=1)
            self.assertEqual(opener.call_count, 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM api_requests WHERE status='UNCERTAIN'").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM api_requests WHERE status='CONSUMED'").fetchone()[0], 1)

    def test_ebay_oauth_token_is_shared_across_source_clients(self):
        from photobook_radar import ebay_gateway
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True)
        first = MeteredEbayBrowseClient(self.db, config, route_id="private", client_id="shared-test-id", client_secret="shared-test-secret")
        second = MeteredEbayBrowseClient(self.db, config, route_id="endgame", client_id="shared-test-id", client_secret="shared-test-secret")
        with patch.object(ebay_gateway, "_TOKEN_CACHE", None), patch.object(first, "_json_request", return_value={"access_token": "shared-token", "expires_in": 7200}) as request:
            self.assertEqual(first.access_token(), "shared-token")
            with patch.object(second, "_json_request", side_effect=AssertionError("unnecessary OAuth request")):
                self.assertEqual(second.access_token(), "shared-token")
            self.assertEqual(request.call_count, 1)

    def test_ebay_account_quota_reading_is_shared_across_source_threads(self):
        from concurrent.futures import ThreadPoolExecutor
        from photobook_radar import ebay_gateway
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True)
        window = BrowseWindow("2026-09-28T07:00:00Z", "2026-09-29T07:00:00Z", 5000, 2500, now())
        with (patch.object(ebay_gateway, "_QUOTA_CACHE", None),
              patch.object(ebay_gateway, "load_credentials", return_value=("quota-test-id", "quota-test-secret")),
              patch.object(MeteredEbayBrowseClient, "refresh_browse_quota", return_value=window) as refresh):
            with ThreadPoolExecutor(max_workers=2) as pool:
                clients = list(pool.map(lambda route: ebay_gateway.thread_client(self.db, config, route), ("private", "endgame")))
            self.assertEqual(refresh.call_count, 1)
            self.assertTrue(all(client.browse_window is window for client in clients))

    def test_oxfam_photography_page_uses_sku_and_silent_baseline(self):
        payload = {"searchEventSummary": {"resultsSummary": [{"sort": {"sortKeys": [{"attribute": "product.creationDate", "direction": "desc"}]}, "totalMatchingRecords": 1, "records": [{"sku.listingId": "HD_123", "record.id": "/sku-PRODUCT..1", "product.displayName": "Danny Lyon: The Bikeriders", "sku.activePrice": "20", "product.route": "/books/bikeriders"}]}]}}
        result = capture_photography_page(self.db, window_id="oxfam-test", page_number=1, payload=payload, baseline=True)
        self.assertIsNone(result.next_offset)
        row = self.db.execute("SELECT platform,external_id,imported,canonical_url FROM listings").fetchone()
        self.assertEqual((row["platform"], row["external_id"], row["imported"]), ("oxfam", "HD_123", 1))
        self.assertEqual(row["canonical_url"], "https://onlineshop.oxfam.org.uk/books/bikeriders")
        self.assertEqual(self.db.execute("SELECT complete FROM search_windows WHERE id='oxfam-test'").fetchone()[0], 1)
        with self.assertRaises(RuntimeError):
            parse_photography_page({"searchEventSummary": {"resultsSummary": []}}, 0)

    def test_oxfam_scheduler_baselines_once_and_commits_completion(self):
        self.assertFalse(schedule_oxfam(self.db, Config(data_dir=Path(self.folder.name))))
        self.assertFalse(schedule_oxfam(self.db, Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True)))
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True, source_oxfam_photography=True)
        self.assertTrue(schedule_oxfam(self.db, config))
        self.assertFalse(schedule_oxfam(self.db, config))
        job = claim_job(self.db, "source-test", kinds=("SCAN_OXFAM",))
        payload = {"searchEventSummary": {"resultsSummary": [{"sort": {"sortKeys": [{"attribute": "product.creationDate", "direction": "desc"}]}, "totalMatchingRecords": 1, "records": [{"sku.listingId": "HD_123", "product.displayName": "Photobook by Danny Lyon", "sku.activePrice": "20"}]}]}}
        result = run_oxfam_scan_job(self.db, job, config, fetch=lambda _config, _offset: payload)
        finish_job(self.db, job["id"], job["lease_token"])
        self.assertTrue(result["complete"])
        self.assertEqual(self.db.execute("SELECT imported FROM listings WHERE platform='oxfam'").fetchone()[0], 1)
        self.assertIsNotNone(self.db.execute("SELECT last_success_at FROM source_routes WHERE id='oxfam-photography'").fetchone()[0])
        self.assertFalse(schedule_oxfam(self.db, config))

    def test_oxfam_page_and_followup_job_are_atomic(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True, source_oxfam_photography=True)
        schedule_oxfam(self.db, config)
        job = claim_job(self.db, "source-test", kinds=("SCAN_OXFAM",))
        records = [{"sku.listingId": f"HD_{1000+i}", "product.displayName": f"Photobook {i}", "sku.activePrice": "20"} for i in range(30)]
        payload = {"searchEventSummary": {"resultsSummary": [{"sort": {"sortKeys": [{"attribute": "product.creationDate", "direction": "desc"}]}, "totalMatchingRecords": 31, "records": records}]}}
        result = run_oxfam_scan_job(self.db, job, config, fetch=lambda _config, _offset: payload)
        self.assertFalse(result["complete"])
        window = self.db.execute("SELECT complete,last_durable_page,continuation_json FROM search_windows WHERE route_id='oxfam-photography'").fetchone()
        self.assertEqual((window["complete"], window["last_durable_page"]), (0, 1))
        self.assertIn('"next_offset": 30', window["continuation_json"])
        followup = self.db.execute("SELECT payload_json FROM jobs WHERE kind='SCAN_OXFAM' AND status='PENDING'").fetchone()
        self.assertIn('"offset": 30', followup[0])

    def test_oxfam_routine_frontier_is_bounded_and_coverage_is_explicit(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True, source_oxfam_photography=True)
        self.assertTrue(schedule_oxfam(self.db, config))
        first = claim_job(self.db, "source-test", kinds=("SCAN_OXFAM",))
        with transaction(self.db):
            self.db.execute("UPDATE jobs SET status='DONE',lease_token=NULL,lease_owner=NULL,lease_until=NULL WHERE id=?", (first["id"],))
            self.db.execute("UPDATE source_routes SET last_success_at='2026-09-28T00:00:00Z',next_due_at='2020-01-01T00:00:00Z' WHERE id='oxfam-photography'")
            self.db.execute("INSERT INTO health(key,value,updated_at) VALUES('oxfam_photography_last_deep',?,?)", (datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"), "2026-09-28T00:00:00Z"))
        self.assertTrue(schedule_oxfam(self.db, config))
        def response(offset):
            records = [{"sku.listingId": f"HD_{offset+i+1000}", "product.displayName": f"Photobook {offset+i}", "sku.activePrice": "20"} for i in range(30)]
            return {"searchEventSummary": {"resultsSummary": [{"sort": {"sortKeys": [{"attribute": "product.creationDate", "direction": "desc"}]}, "totalMatchingRecords": 61, "records": records}]}}
        job1 = claim_job(self.db, "source-test", kinds=("SCAN_OXFAM",))
        self.assertFalse(run_oxfam_scan_job(self.db, job1, config, fetch=lambda _config, offset: response(offset))["complete"])
        finish_job(self.db, job1["id"], job1["lease_token"])
        job2 = claim_job(self.db, "source-test", kinds=("SCAN_OXFAM",))
        result = run_oxfam_scan_job(self.db, job2, config, fetch=lambda _config, offset: response(offset))
        self.assertEqual(result["frontier_only"], True)
        self.assertEqual(self.db.execute("SELECT incomplete_reason FROM source_routes WHERE id='oxfam-photography'").fetchone()[0], "Frontier scan: first 60 of 61 newest records; older stock awaits daily full sweep")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='SCAN_OXFAM' AND status='PENDING'").fetchone()[0], 0)

    def test_ebay_pagination_cursor_commits_with_each_page(self):
        source = {"id": "ebay-private", "name": "eBay private", "marketplace": "EBAY_GB"}
        def item(suffix):
            return {"itemId": f"v1|12345678901{suffix}|0", "title": f"Photobook {suffix}", "price": {"value": "12", "currency": "GBP"}, "itemWebUrl": f"https://www.ebay.co.uk/itm/12345678901{suffix}"}
        first = {"itemSummaries": [item(1)], "total": 2, "offset": 0, "next": "https://api.ebay.com/buy/browse/v1/item_summary/search?offset=1"}
        result = capture_browse_page(self.db, source=source, route_id="ebay-private-GB", window_id="ebay-window", page_number=1, payload=first, baseline=True)
        self.assertFalse(result.complete)
        window = self.db.execute("SELECT complete,last_durable_page,continuation_json FROM search_windows WHERE id='ebay-window'").fetchone()
        self.assertEqual((window["complete"], window["last_durable_page"]), (0, 1))
        self.assertIn("offset=1", window["continuation_json"])
        second = {"itemSummaries": [item(2)], "total": 2, "offset": 1}
        self.assertTrue(capture_browse_page(self.db, source=source, route_id="ebay-private-GB", window_id="ebay-window", page_number=2, payload=second, baseline=True).complete)
        self.assertEqual(self.db.execute("SELECT complete,last_durable_page FROM search_windows WHERE id='ebay-window'").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM listings WHERE platform='ebay'").fetchone()[0], 2)
        capture_browse_page(self.db, source=source, route_id="ebay-private-GB", window_id="ebay-window", page_number=1, payload=first, baseline=True)
        window = self.db.execute("SELECT complete,last_durable_page FROM search_windows WHERE id='ebay-window'").fetchone()
        self.assertEqual(tuple(window), (1, 2))
        with self.assertRaises(ValueError):
            parse_browse_page({**first, "next": "https://evil.example/steal"}, source)

    def test_ebay_broad_frontier_marks_partial_coverage_without_extra_request(self):
        self.assertEqual(schedule_ebay_broad(self.db, Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True)), 0)
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True, source_ebay_broad=True)
        self.assertEqual(schedule_ebay_broad(self.db, config), 2)
        job = claim_job(self.db, "source-test", kinds=("SCAN_EBAY_BROAD",))
        item = {"itemId": "v1|123456789012|0", "title": "Danny Lyon photobook", "price": {"value": "20", "currency": "GBP"}, "itemWebUrl": "https://www.ebay.co.uk/itm/123456789012"}
        response = {"itemSummaries": [item], "total": 350, "offset": 0, "next": "https://api.ebay.com/buy/browse/v1/item_summary/search?offset=1"}
        calls = []
        result = run_ebay_broad_job(self.db, job, config, fetch=lambda _db, _config, route, query: (calls.append((route, query)), response)[1])
        self.assertTrue(result["frontier_only"])
        self.assertEqual(len(calls), 1)
        self.assertIn("first 1 of 350", self.db.execute("SELECT incomplete_reason FROM source_routes WHERE id=?", (job["route_id"],)).fetchone()[0])
        self.assertEqual(self.db.execute("SELECT complete FROM search_windows WHERE route_id=?", (job["route_id"],)).fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM listings").fetchone()[0], 1)

    def test_expired_auction_event_is_not_sent(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_real_notifications=True, notification_enabled=True)
        with transaction(self.db):
            listing_id = capture(self.db, {"key": "ebay:123456789012", "title": "Old auction"}, source_id="ebay", origin_key="expired", imported=True)
            enqueue_notification(self.db, listing_id=listing_id, stage="FAST_LEAD", material_version="1", channel="telegram", payload={"title": "Expired"}, expires_at="2020-01-01T00:00:00Z")
        self.assertFalse(send_one(self.db, config, fake=True))
        self.assertEqual(self.db.execute("SELECT status FROM notification_events").fetchone()[0], "SUPPRESSED")

    def test_unknown_delivery_has_one_bounded_disclosed_retry(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_real_notifications=True, notification_enabled=True)
        with transaction(self.db):
            listing_id = capture(self.db, {"key": "ebay:123456789012", "title": "Possible book"}, source_id="ebay", origin_key="possible", imported=False)
            observation = self.db.execute("SELECT current_observation_id FROM listings WHERE id=?", (listing_id,)).fetchone()[0]
            review = self.db.execute("INSERT INTO reviews(listing_id,observation_id,provider,policy_hash,verdict,status,result_json) VALUES(?,?,?,?,?,?,?)",
                                     (listing_id, observation, "codex_cli", "collector-bargains-v8", "GEM", "DONE", '{"bargain_screen":{"accepted":true}}'))
            self.db.execute("INSERT INTO live_checks(listing_id,observation_id,provider,checked_at,availability,price_minor,currency,result_json) VALUES(?,?,?,?,?,?,?,?)",
                            (listing_id, observation, "ebay_browse", now(), "LIVE", 2000, "GBP", "{}"))
            enqueue_notification(self.db, listing_id=listing_id, stage="BARGAIN_FIND", material_version="initial", channel="telegram", payload={"message": "Check listing", "review_id": review.lastrowid, "price_minor": 2000, "shipping_minor": None, "shipping_currency": None})
            self.db.execute("UPDATE notification_events SET status='DELIVERY_UNKNOWN',attempts=1,lease_until='2020-01-01T00:00:00Z'")
        self.assertTrue(send_one(self.db, config, fake=True))
        row = self.db.execute("SELECT status,attempts FROM notification_events").fetchone()
        self.assertEqual(tuple(row), ("PROVIDER_ACCEPTED", 2))

    def test_private_photo_reply_to_researched_alert_gets_image_review(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_real_notifications=True,
                        notification_enabled=True, research_provider="codex_cli", research_recurring_enabled=True)
        with transaction(self.db):
            listing_id = capture(self.db, {"key": "ebay:123456789012", "title": "The Bikeriders Danny Lyon",
                                           "price_gbp": "20", "url": "https://www.ebay.co.uk/itm/123456789012"},
                                 source_id="ebay", origin_key="photo-subject", imported=False)
            observation = self.db.execute("SELECT current_observation_id FROM listings WHERE id=?", (listing_id,)).fetchone()[0]
            review = self.db.execute("INSERT INTO reviews(listing_id,observation_id,provider,policy_hash,verdict,status,result_json) VALUES(?,?,?,?,?,?,?)",
                                     (listing_id, observation, "codex_cli", "collector-research-v3", "PAY_ATTENTION", "DONE", '{"edition_note":"unconfirmed"}'))
            enqueue_notification(self.db, listing_id=listing_id, stage="RESEARCHED_FIND", material_version="initial", channel="telegram",
                                 payload={"message": "A book lead", "review_id": review.lastrowid})
            self.db.execute("UPDATE notification_events SET status='PROVIDER_ACCEPTED',provider_request='42' WHERE stage='RESEARCHED_FIND'")
        message = {"update_id": 50, "message": {"chat": {"id": 123, "type": "private"}, "message_id": 99,
                                                "reply_to_message": {"message_id": 42}, "caption": "Is this signed?",
                                                "photo": [{"file_id": "small", "file_size": 100}, {"file_id": "large", "file_size": 400}]}}
        self.assertEqual(ingest_updates(self.db, [message], "other-chat"), 0)
        self.assertEqual(ingest_updates(self.db, [message], "123"), 0)  # already acknowledged update
        message["update_id"] = 51
        self.assertEqual(ingest_updates(self.db, [message], "123"), 1)
        self.assertEqual(ingest_updates(self.db, [message], "123"), 0)
        job = claim_job(self.db, "photo-test", kinds=("PHOTO_REVIEW",))
        self.assertEqual(__import__("json").loads(job["payload_json"])["file_id"], "large")
        with transaction(self.db):
            self.db.execute("INSERT INTO sources(id,adapter,status) VALUES('telegram-photos','telegram','ACTIVE')")
            self.db.executemany("INSERT INTO research_sweeps(source_id,job_id,started_at,provider,model,status) VALUES('telegram-photos',?,?,'codex_cli','gpt-6-sol','DONE')",
                                [(job["id"], now())] * 48)
        def download(_config, _file_id, folder):
            path = folder / "photo.jpg"
            path.write_bytes(b"\xff\xd8\xff\xd9")
            return path
        result = run_photo_research(self.db, job, config, downloader=download,
                                    provider=lambda _cfg, prompt, path: {
                                        "assessment": "STRONGER", "visible": "An ink signature appears on the title page.",
                                        "edition_condition": "Copyright page is not shown.",
                                        "collector_impact": "The signature may make this copy more interesting; authenticity remains unverified.",
                                        "next_check": "Ask for a clear copyright page image."
                                    })
        self.assertEqual(result["assessment"], "STRONGER")
        finish_job(self.db, job["id"], job["lease_token"])
        event = self.db.execute("SELECT payload_json FROM notification_events WHERE stage='PHOTO_REVIEW_REPLY'").fetchone()
        self.assertEqual(__import__("json").loads(event[0])["reply_to_message_id"], 99)
        self.assertTrue(send_one(self.db, config, fake=True))
        self.assertEqual(self.db.execute("SELECT status FROM notification_events WHERE stage='PHOTO_REVIEW_REPLY'").fetchone()[0], "PROVIDER_ACCEPTED")

    def test_unrelated_telegram_photo_does_not_queue_research(self):
        update = {"update_id": 80, "message": {"chat": {"id": 123, "type": "private"}, "message_id": 22,
                                               "photo": [{"file_id": "image", "file_size": 400}]}}
        self.assertEqual(ingest_updates(self.db, [update], "123"), 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='PHOTO_REVIEW'").fetchone()[0], 0)

    def test_private_photo_reply_to_earlier_quick_lead_is_reviewed(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_real_notifications=True,
                        notification_enabled=True, research_provider="codex_cli", research_recurring_enabled=True)
        with transaction(self.db):
            listing_id = capture(self.db, {"key": "ebay:123456789012", "title": "Henri Cartier-Bresson: Europeans",
                                           "price_gbp": "20", "url": "https://www.ebay.co.uk/itm/123456789012"},
                                 source_id="ebay", origin_key="quick-lead-photo", imported=False)
            enqueue_notification(self.db, listing_id=listing_id, stage="FAST_LEAD", material_version="initial",
                                 channel="telegram", payload={"message": "Quick lead"})
            self.db.execute("UPDATE notification_events SET status='PROVIDER_ACCEPTED',provider_request='15' WHERE stage='FAST_LEAD'")
        update = {"update_id": 81, "message": {"chat": {"id": 123, "type": "private"}, "message_id": 40,
                                                "reply_to_message": {"message_id": 15}, "media_group_id": "album-1",
                                                "photo": [{"file_id": "image", "file_size": 400}]}}
        self.assertEqual(ingest_updates(self.db, [update], "123"), 1)
        job = claim_job(self.db, "photo-test", kinds=("PHOTO_REVIEW",))
        self.assertIsNotNone(job)
        def download(_config, _file_id, folder):
            path = folder / "photo.jpg"
            path.write_bytes(b"\xff\xd8\xff\xd9")
            return path
        result = run_photo_research(self.db, job, config, downloader=download,
                                    provider=lambda *_: {"assessment": "UNCLEAR", "visible": "Cover shown.",
                                                         "edition_condition": "Copyright page absent.",
                                                         "collector_impact": "Edition remains unverified.",
                                                         "next_check": "Request the copyright page."})
        self.assertEqual(result["assessment"], "UNCLEAR")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notification_events WHERE stage='PHOTO_REVIEW_REPLY'").fetchone()[0], 1)
        self.assertIn('"outcome": "QUEUED"', self.db.execute("SELECT detail_json FROM app_events WHERE kind='telegram_inbox'").fetchone()[0])

    def test_researched_alert_is_suppressed_after_listing_changes(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_real_notifications=True, notification_enabled=True)
        with transaction(self.db):
            listing_id = capture(self.db, {"key": "ebay:123456789012", "title": "Danny Lyon The Bikeriders",
                                           "price_gbp": "20", "url": "https://www.ebay.co.uk/itm/123456789012"},
                                 source_id="ebay", origin_key="before", imported=False)
            observation = self.db.execute("SELECT current_observation_id FROM listings WHERE id=?", (listing_id,)).fetchone()[0]
            review = self.db.execute("INSERT INTO reviews(listing_id,observation_id,provider,policy_hash,verdict,status,result_json) VALUES(?,?,?,?,?,?,?)",
                                     (listing_id, observation, "codex_cli", "collector-bargains-v8", "GEM", "DONE", '{"bargain_screen":{"accepted":true}}'))
            self.db.execute("INSERT INTO live_checks(listing_id,observation_id,provider,checked_at,availability,price_minor,currency,result_json) VALUES(?,?,?,?,?,?,?,?)",
                            (listing_id, observation, "ebay_browse", now(), "LIVE", 2000, "GBP", "{}"))
            enqueue_notification(self.db, listing_id=listing_id, stage="BARGAIN_FIND", material_version=str(observation),
                                 channel="telegram", payload={"message": "Potential gem", "review_id": review.lastrowid,
                                                              "price_minor": 2000, "shipping_minor": None, "shipping_currency": None})
            capture(self.db, {"key": "ebay:123456789012", "title": "Different later reprint", "price_gbp": "20",
                              "url": "https://www.ebay.co.uk/itm/123456789012"}, source_id="ebay", origin_key="after", imported=False)
        self.assertFalse(send_one(self.db, config, fake=True))
        self.assertEqual(self.db.execute("SELECT status FROM notification_events").fetchone()[0], "SUPPRESSED")

    def test_owner_dismissal_suppresses_queued_phone_alert(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_real_notifications=True, notification_enabled=True)
        with transaction(self.db):
            listing_id = capture(self.db, {"key": "ebay:123456789012", "title": "Possible book"}, source_id="ebay", origin_key="possible", imported=False)
            enqueue_notification(self.db, listing_id=listing_id, stage="FAST_LEAD", material_version="initial", channel="telegram", payload={"message": "Check listing"})
        decide(self.db, listing_id, "DISMISS")
        self.assertFalse(send_one(self.db, config, fake=True))
        self.assertEqual(self.db.execute("SELECT status FROM notification_events").fetchone()[0], "SUPPRESSED")

    def test_escaped_title_and_rejected_unauthorised_write(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("FastAPI is installed in the locked application environment")
        from photobook_radar import web

        with transaction(self.db):
            listing_id = capture(
                self.db,
                {"key": "ebay:123456789012", "title": '<script>alert("x")</script>', "price_gbp": "12"},
                source_id="ebay", origin_key="hostile", imported=True,
            )
        original = web.config
        web.config = Config(data_dir=Path(self.folder.name))
        try:
            client = TestClient(web.app, base_url="http://127.0.0.1:8765")
            page = client.get(f"/listing/{listing_id}")
            self.assertEqual(page.status_code, 200)
            self.assertNotIn('<script>alert("x")</script>', page.text)
            self.assertIn("&lt;script&gt;", page.text)
            system = client.get("/system")
            self.assertEqual(system.status_code, 200)
            self.assertIn("Oxfam Photography", system.text)
            self.assertIn("OFF (shadow)", system.text)
            self.assertIn("Lead mode; no AI provider", system.text)
            self.assertNotIn("ebay_backfill_ebay_gb", system.text)
            unauthenticated = client.post(f"/listing/{listing_id}/decision", data={"action": "BOUGHT", "csrf": "wrong"})
            self.assertEqual(unauthenticated.status_code, 401)
            cross_origin = client.post(f"/listing/{listing_id}/decision", headers={"Origin": "https://evil.example"}, data={"action": "BOUGHT", "csrf": "wrong"})
            self.assertEqual(cross_origin.status_code, 403)
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM user_decisions").fetchone()[0], 0)
        finally:
            web.config = original


class RadarTriageTests(unittest.TestCase):
    def test_search_query_and_issue_packet_cannot_attribute_a_book(self):
        item = {
            "title": "Newts of the British Isles - Patrick Wisniewski",
            "price_gbp": "31.90",
            "context": "Search: (Jamie Hawkesworth,The British Isles) Priority target Jamie Hawkesworth. Photobook.",
            "url": "https://www.ebay.co.uk/itm/123456789012?_skw=Jamie+Hawkesworth",
        }
        result = score(item)
        self.assertIsNone(result["photographer"])
        self.assertLess(result["score"], 58)

    def test_visible_photobook_match_survives(self):
        result = score({"title": "The Bikeriders Danny Lyon first printing 1968", "price_gbp": "20"})
        self.assertEqual(result["photographer"], "Danny Lyon")
        self.assertGreaterEqual(result["score"], 58)

    def test_internal_urls_are_not_seller_links(self):
        self.assertIsNone(safe_url("https://127.0.0.1/private"))
        self.assertIsNone(safe_url("https://example.local/private"))
        self.assertEqual(safe_url("https://www.ebay.co.uk/itm/123456789012#fragment"), "https://www.ebay.co.uk/itm/123456789012")


if __name__ == "__main__":
    unittest.main()
