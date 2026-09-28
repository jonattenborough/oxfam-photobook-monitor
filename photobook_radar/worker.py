"""Single scheduler authority. Shadow mode performs local work only."""
from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import Config, load_config
from .db import connect, migrate, transaction
from .ebay_scheduler import SOURCE as EBAY_BROAD_SOURCE, run_ebay_broad_job, schedule_ebay_broad
from .ebay_lanes import LANES as EBAY_LANES, run_ebay_lane_job, schedule_ebay_lanes
from .notifications import send_one
from .source_scheduler import ROUTE as OXFAM_ROUTE, run_oxfam_scan_job, schedule_oxfam
from .oxfam_broad import SOURCE as OXFAM_BROAD_SOURCE, run_oxfam_broad_job, schedule_oxfam_broad
from .shopify_scheduler import run_shopify_job, schedule_shopify
from .abebooks_scheduler import run_abebooks_job, schedule_abebooks
from .research_sweeps import ResearchDeferred, _later, run_lead_research, run_research_job, schedule_research
from .telegram_photos import poll_updates, run_photo_research
from .store import claim_job, finish_job, now
from .triage import run_triage
from .verification import run_verify

RESEARCH_CONCURRENCY = 3
VERIFY_CONCURRENCY = 2


def tick(db: sqlite3.Connection, config: Config, owner: str) -> bool:
    with transaction(db):
        db.execute("INSERT INTO health(key,value,updated_at) VALUES('worker_heartbeat',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (now(), now()))
    if send_one(db, config):
        return True
    job = claim_job(db, owner, kinds=("TRIAGE",))
    if job is None:
        return False
    try:
        run_triage(db, job, config)
    except Exception as exc:
        try:
            finish_job(db, job["id"], job["lease_token"], error=str(exc))
        except RuntimeError:
            pass
    return True


def _run_source_job(config: Config, job_id: int, token: str) -> None:
    # The source thread owns its own SQLite connection. A slow marketplace
    # request cannot block the main loop's triage and phone outbox work.
    db = connect(config.database, existing=True)
    try:
        job = db.execute("SELECT * FROM jobs WHERE id=? AND lease_token=? AND status='RUNNING'", (job_id, token)).fetchone()
        if job is None:
            return
        try:
            if job["kind"] == "SCAN_OXFAM":
                run_oxfam_scan_job(db, job, config)
            elif job["kind"] == "SCAN_OXFAM_BROAD":
                run_oxfam_broad_job(db, job, config)
            elif job["kind"] == "SCAN_EBAY_BROAD":
                run_ebay_broad_job(db, job, config)
            elif job["kind"] == "SCAN_SHOPIFY":
                run_shopify_job(db, job, config)
            elif job["kind"] == "SCAN_ABEBOOKS":
                run_abebooks_job(db, job, config)
            elif job["kind"] in {definition[1] for definition in EBAY_LANES.values()}:
                run_ebay_lane_job(db, job, config)
            else:
                raise ValueError("Unknown source job")
            finish_job(db, job_id, token)
        except Exception as exc:
            with transaction(db):
                source_id = OXFAM_ROUTE if job["kind"] == "SCAN_OXFAM" else OXFAM_BROAD_SOURCE if job["kind"] == "SCAN_OXFAM_BROAD" else "abebooks" if job["kind"] == "SCAN_ABEBOOKS" else str(job["route_id"]).split(":", 1)[0] if job["kind"] == "SCAN_SHOPIFY" else next((definition[0] for definition in EBAY_LANES.values() if definition[1] == job["kind"]), EBAY_BROAD_SOURCE)
                db.execute("UPDATE sources SET status='DEGRADED',last_error=? WHERE id=?", (str(exc)[:300], source_id))
            try:
                finish_job(db, job_id, token, error=str(exc), retry_seconds=120)
            except RuntimeError:
                pass
    finally:
        db.close()


def _run_verify_job(config: Config, job_id: int, token: str) -> None:
    db = connect(config.database, existing=True)
    try:
        job = db.execute("SELECT * FROM jobs WHERE id=? AND lease_token=? AND status='RUNNING'", (job_id, token)).fetchone()
        if job is None:
            return
        try:
            run_verify(db, job, config)
        except Exception as exc:
            try:
                finish_job(db, job_id, token, error=str(exc), retry_seconds=120)
            except RuntimeError:
                pass
    finally:
        db.close()


def _run_research_job(config: Config, job_id: int, token: str) -> None:
    db = connect(config.database, existing=True)
    try:
        job = db.execute("SELECT * FROM jobs WHERE id=? AND lease_token=? AND status='RUNNING'", (job_id, token)).fetchone()
        if job is None:
            return
        try:
            if job["kind"] == "PHOTO_REVIEW":
                run_photo_research(db, job, config)
            elif job["kind"] == "RESEARCH_LEAD":
                run_lead_research(db, job, config)
            else:
                run_research_job(db, job, config)
            finish_job(db, job_id, token)
            with transaction(db):
                db.execute("DELETE FROM health WHERE key='codex_research_backoff_until'")
        except ResearchDeferred as exc:
            with transaction(db):
                # Waiting for source verification or the provider is not a
                # failed research attempt and must not exhaust job retries.
                db.execute("UPDATE jobs SET attempts=MAX(0,attempts-1) WHERE id=? AND lease_token=? AND status='RUNNING'", (job_id, token))
                if exc.provider_limited:
                    db.execute("INSERT INTO health(key,value,updated_at) VALUES('codex_research_backoff_until',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                               (_later(exc.retry_seconds), now()))
            finish_job(db, job_id, token, error=str(exc), retry_seconds=exc.retry_seconds)
        except Exception as exc:
            with transaction(db):
                db.execute("UPDATE sources SET status='DEGRADED',last_error=? WHERE id=?", (f"{type(exc).__name__}: {str(exc)[:180]}", job["route_id"] or "research-leads"))
            try:
                finish_job(db, job_id, token, error=f"{type(exc).__name__}: {str(exc)[:180]}", retry_seconds=900)
            except RuntimeError:
                pass
    finally:
        db.close()


def run(config: Config | None = None, *, once: bool = False) -> None:
    config = config or load_config()
    db = connect(config.database, existing=True)
    migrate(db)
    with transaction(db):
        for enabled, kind in ((config.source_oxfam_photography, "SCAN_OXFAM"), (config.source_oxfam_broad, "SCAN_OXFAM_BROAD"), (config.source_ebay_broad, "SCAN_EBAY_BROAD"), (config.source_charity_shops or config.source_specialist_shops or config.source_publishers, "SCAN_SHOPIFY"), (config.source_ebay_private, "SCAN_EBAY_PRIVATE"), (config.source_ebay_charity, "SCAN_EBAY_CHARITY"), (config.source_ebay_endgame, "SCAN_EBAY_ENDGAME"), (config.source_abebooks, "SCAN_ABEBOOKS"), (config.research_recurring_enabled and (config.source_wider_web or config.source_publishers or config.source_prizes), "RESEARCH_SWEEP")):
            if not (config.production and config.allow_marketplace_network and enabled):
                db.execute("UPDATE jobs SET status='CANCELLED',lease_token=NULL,lease_owner=NULL,lease_until=NULL,last_error='Source deselected or shadow mode' WHERE kind=? AND status IN ('PENDING','RUNNING')", (kind,))
        if not (config.production and config.research_recurring_enabled and config.research_provider == "codex_cli"):
            db.execute("UPDATE jobs SET status='CANCELLED',lease_token=NULL,lease_owner=NULL,lease_until=NULL,last_error='Research deselected or shadow mode' WHERE kind IN ('RESEARCH_LEAD','PHOTO_REVIEW') AND status IN ('PENDING','RUNNING')")
    owner = f"{socket.gethostname()}:{os.getpid()}"
    stopped = False

    def stop(*_: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    source_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="radar-source")
    verify_pool = ThreadPoolExecutor(max_workers=VERIFY_CONCURRENCY, thread_name_prefix="radar-verify")
    research_pool = ThreadPoolExecutor(max_workers=RESEARCH_CONCURRENCY, thread_name_prefix="radar-research")
    source_futures = {name: None for name in ("oxfam", "oxfam_broad", "ebay_endgame", "ebay_private", "ebay_charity", "ebay_broad", "shopify", "abebooks")}
    source_groups = {
        "oxfam": ("SCAN_OXFAM",),
        "oxfam_broad": ("SCAN_OXFAM_BROAD",),
        "ebay_endgame": ("SCAN_EBAY_ENDGAME",),
        "ebay_private": ("SCAN_EBAY_PRIVATE",),
        "ebay_charity": ("SCAN_EBAY_CHARITY",),
        "ebay_broad": ("SCAN_EBAY_BROAD",),
        "shopify": ("SCAN_SHOPIFY",),
        "abebooks": ("SCAN_ABEBOOKS",),
    }
    verify_futures = set()
    research_futures = set()
    next_source_check = 0.0
    next_telegram_check = 0.0
    try:
        while not stopped:
            if (config.production and config.notification_enabled and config.notification_primary == "telegram"
                    and config.research_recurring_enabled and config.research_provider == "codex_cli"
                    and time.monotonic() >= next_telegram_check):
                try:
                    poll_updates(db, config)
                    with transaction(db):
                        db.execute("INSERT INTO health(key,value,updated_at) VALUES('telegram_inbox_poll',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (now(), now()))
                        db.execute("DELETE FROM health WHERE key='telegram_inbox_error'")
                except Exception as exc:
                    with transaction(db):
                        db.execute("INSERT INTO health(key,value,updated_at) VALUES('telegram_inbox_error',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (f"{type(exc).__name__}: {str(exc)[:150]}", now()))
                next_telegram_check = time.monotonic() + 15
            if config.production and config.allow_marketplace_network and time.monotonic() >= next_source_check:
                for future in tuple(verify_futures):
                    if future.done():
                        future.result()
                        verify_futures.remove(future)
                while len(verify_futures) < VERIFY_CONCURRENCY:
                    verify_job = claim_job(db, owner, kinds=("VERIFY",))
                    if verify_job is None:
                        break
                    verify_futures.add(verify_pool.submit(_run_verify_job, config, verify_job["id"], verify_job["lease_token"]))
                for future in tuple(research_futures):
                    if future.done():
                        future.result()
                        research_futures.remove(future)
                provider_pause = db.execute("SELECT value FROM health WHERE key='codex_research_backoff_until'").fetchone()
                while (len(research_futures) < RESEARCH_CONCURRENCY and config.research_recurring_enabled
                       and (not provider_pause or provider_pause[0] <= now())):
                    research_job = claim_job(db, owner, kinds=("PHOTO_REVIEW", "RESEARCH_LEAD", "RESEARCH_SWEEP"), lease_seconds=180)
                    if research_job is None:
                        break
                    research_futures.add(research_pool.submit(_run_research_job, config, research_job["id"], research_job["lease_token"]))
                for group, future in source_futures.items():
                    if future is not None and future.done():
                        future.result()
                        source_futures[group] = None
                schedule_oxfam(db, config)
                schedule_oxfam_broad(db, config)
                schedule_ebay_broad(db, config)
                schedule_ebay_lanes(db, config)
                schedule_shopify(db, config)
                schedule_abebooks(db, config)
                schedule_research(db, config)
                for group, kinds in source_groups.items():
                    if source_futures[group] is None:
                        source_job = claim_job(db, owner, kinds=kinds)
                        if source_job is not None:
                            source_futures[group] = source_pool.submit(_run_source_job, config, source_job["id"], source_job["lease_token"])
                next_source_check = time.monotonic() + 15
            worked = tick(db, config, owner)
            if once:
                break
            time.sleep(0.003 if worked else config.tick_seconds)
    finally:
        verify_pool.shutdown(wait=True)
        research_pool.shutdown(wait=True)
        source_pool.shutdown(wait=True)
        db.close()
