"""Failure cases for the local radar persistence and screening boundary."""
from __future__ import annotations

import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from photobook_radar.config import Config
from photobook_radar.db import connect, migrate, transaction
from photobook_radar.ebay_gateway import BrowseWindow, MeteredEbayBrowseClient
from photobook_radar.ebay_lanes import run_ebay_lane_job, schedule_ebay_lanes
from photobook_radar.ebay_scheduler import run_ebay_broad_job, schedule_ebay_broad
from photobook_radar.notifications import send_one
from photobook_radar.source_scheduler import run_oxfam_scan_job, schedule_oxfam
from photobook_radar.oxfam_broad import run_oxfam_broad_job, schedule_oxfam_broad
from photobook_radar.research_sweeps import run_lead_research, run_research_job, schedule_research
from photobook_radar.shopify_scheduler import parse_products, run_shopify_job, schedule_shopify
from photobook_radar.sources.ebay import capture_browse_page, parse_browse_page
from photobook_radar.sources.oxfam import capture_photography_page, parse_photography_page
from photobook_radar.store import capture, capture_page, claim_job, decide, enqueue_job, enqueue_notification, finish_job, reserve_request, safe_url, settle_request
from photobook_radar.triage import run_triage, score
from photobook_radar.verification import run_verify


class RadarPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.folder.name) / "radar.db")
        migrate(self.db)

    def tearDown(self):
        self.db.close()
        self.folder.cleanup()

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

    def test_three_ebay_lanes_schedule_with_one_durable_budget(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True,
                        source_ebay_private=True, source_ebay_charity=True, source_ebay_endgame=True)
        counts = schedule_ebay_lanes(self.db, config)
        self.assertGreaterEqual(counts["private"], 10)
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

    def test_research_sweeps_have_three_schedules_and_a_daily_cap(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True,
                        research_provider="codex_cli", research_recurring_enabled=True,
                        source_wider_web=True, source_publishers=True, source_prizes=True)
        self.assertEqual(schedule_research(self.db, config), 3)
        self.assertEqual(schedule_research(self.db, config), 0)
        job = claim_job(self.db, "research-test", kinds=("RESEARCH_SWEEP",), lease_seconds=180)
        result = {"items": [{"title": "New photobook", "url": "https://www.biblio.com/book/123456789",
                             "source_name": "Biblio", "why": "Photographer's book", "published_at": "", "price_amount": None, "currency": ""}]}
        outcome = run_research_job(self.db, job, config, provider=lambda *_: result, link_check=lambda *_: True)
        self.assertEqual(outcome["validated"], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM research_sweeps WHERE status='DONE'").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT imported FROM listings").fetchone()[0], 1)

    def test_researched_lead_update_requires_verified_reference_link(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_marketplace_network=True,
                        allow_real_notifications=True, notification_enabled=True,
                        research_provider="codex_cli", research_recurring_enabled=True)
        with transaction(self.db):
            listing_id = capture(self.db, {"key": "ebay:123456789012", "title": "Danny Lyon The Bikeriders", "price_gbp": "20",
                                           "url": "https://www.ebay.co.uk/itm/123456789012"}, source_id="ebay", origin_key="lead")
            observation = self.db.execute("SELECT current_observation_id FROM listings WHERE id=?", (listing_id,)).fetchone()[0]
            enqueue_job(self.db, "research:lead", "RESEARCH_LEAD", listing_id=listing_id, payload={"observation_id": observation})
        job = claim_job(self.db, "research-test", kinds=("RESEARCH_LEAD",), lease_seconds=180)
        result = {"context": "Danny Lyon's documentary book is a key work.", "edition_note": "Seller has not proved the edition.",
                  "risk": "Inspect title page and condition.", "source_urls": ["https://www.moma.org/books/bikeriders"]}
        self.assertEqual(run_lead_research(self.db, job, config, provider=lambda *_: result, link_check=lambda *_: True)["status"], "DONE")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notification_events WHERE stage='RESEARCH_UPDATE'").fetchone()[0], 1)

    def test_codex_lead_schema_accepts_context_response(self):
        from photobook_radar import research_sweeps
        response = {"context": "A short verified context.", "edition_note": "Edition unknown.", "risk": "Inspect condition.", "source_urls": []}
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

    def test_fresh_capture_to_durable_fake_phone_event(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_real_notifications=True, notification_enabled=True)
        item = {"key": "ebay:123456789012", "title": "The Bikeriders Danny Lyon first printing 1968", "price_gbp": "20", "url": "https://www.ebay.co.uk/itm/123456789012"}
        with transaction(self.db):
            listing_id = capture(self.db, item, source_id="ebay", origin_key="fresh", imported=False)
            enqueue_job(self.db, f"triage:{listing_id}", "TRIAGE", listing_id=listing_id)
        job = claim_job(self.db, "test", kinds=("TRIAGE",))
        run_triage(self.db, job, config)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notification_events WHERE status='QUEUED'").fetchone()[0], 1)
        self.assertTrue(send_one(self.db, config, fake=True))
        event = self.db.execute("SELECT status,provider_request,payload_json FROM notification_events").fetchone()
        self.assertEqual((event["status"], event["provider_request"]), ("PROVIDER_ACCEPTED", "FAKE-REPLAY-ONLY"))
        self.assertIn("Live status and exact edition are not yet verified", event["payload_json"])

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
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0], 1)

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
            "2026-09-28T00:00:00Z", "2099-09-29T00:00:00Z", 5000, 652,
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
        self.assertEqual(self.db.execute("SELECT status FROM notification_events").fetchone()[0], "EXPIRED")

    def test_unknown_delivery_has_one_bounded_disclosed_retry(self):
        config = Config(data_dir=Path(self.folder.name), mode="production", allow_real_notifications=True, notification_enabled=True)
        with transaction(self.db):
            listing_id = capture(self.db, {"key": "ebay:123456789012", "title": "Possible book"}, source_id="ebay", origin_key="possible", imported=False)
            enqueue_notification(self.db, listing_id=listing_id, stage="FAST_LEAD", material_version="initial", channel="telegram", payload={"message": "Check listing"})
            self.db.execute("UPDATE notification_events SET status='DELIVERY_UNKNOWN',attempts=1,lease_until='2020-01-01T00:00:00Z'")
        self.assertTrue(send_one(self.db, config, fake=True))
        row = self.db.execute("SELECT status,attempts FROM notification_events").fetchone()
        self.assertEqual(tuple(row), ("PROVIDER_ACCEPTED", 2))

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
