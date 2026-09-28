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
from .research_sweeps import run_lead_research, run_research_job, schedule_research
from .store import claim_job, finish_job, now
from .triage import run_triage
from .verification import run_verify


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
            if job["kind"] == "RESEARCH_LEAD":
                run_lead_research(db, job, config)
            else:
                run_research_job(db, job, config)
            finish_job(db, job_id, token)
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
            db.execute("UPDATE jobs SET status='CANCELLED',lease_token=NULL,lease_owner=NULL,lease_until=NULL,last_error='Research deselected or shadow mode' WHERE kind='RESEARCH_LEAD' AND status IN ('PENDING','RUNNING')")
    owner = f"{socket.gethostname()}:{os.getpid()}"
    stopped = False

    def stop(*_: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    source_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="radar-source")
    verify_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="radar-verify")
    research_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="radar-research")
    source_future = None
    ebay_future = None
    shopify_future = None
    verify_future = None
    research_future = None
    next_source_check = 0.0
    try:
        while not stopped:
            if config.production and config.allow_marketplace_network and time.monotonic() >= next_source_check:
                if verify_future is not None and verify_future.done():
                    verify_future.result()
                    verify_future = None
                if verify_future is None:
                    verify_job = claim_job(db, owner, kinds=("VERIFY",))
                    if verify_job is not None:
                        verify_future = verify_pool.submit(_run_verify_job, config, verify_job["id"], verify_job["lease_token"])
                if research_future is not None and research_future.done():
                    research_future.result()
                    research_future = None
                if research_future is None and config.research_recurring_enabled:
                    research_job = claim_job(db, owner, kinds=("RESEARCH_LEAD", "RESEARCH_SWEEP"), lease_seconds=180)
                    if research_job is not None:
                        research_future = research_pool.submit(_run_research_job, config, research_job["id"], research_job["lease_token"])
                if source_future is not None and source_future.done():
                    source_future.result()
                    source_future = None
                if ebay_future is not None and ebay_future.done():
                    ebay_future.result()
                    ebay_future = None
                if shopify_future is not None and shopify_future.done():
                    shopify_future.result()
                    shopify_future = None
                schedule_oxfam(db, config)
                schedule_oxfam_broad(db, config)
                schedule_ebay_broad(db, config)
                schedule_ebay_lanes(db, config)
                schedule_shopify(db, config)
                schedule_abebooks(db, config)
                schedule_research(db, config)
                if source_future is None and (config.source_oxfam_photography or config.source_oxfam_broad):
                    source_job = claim_job(db, owner, kinds=("SCAN_OXFAM", "SCAN_OXFAM_BROAD"))
                    if source_job is not None:
                        source_future = source_pool.submit(_run_source_job, config, source_job["id"], source_job["lease_token"])
                if ebay_future is None and (config.source_ebay_broad or config.source_ebay_private or config.source_ebay_charity or config.source_ebay_endgame):
                    ebay_job = claim_job(db, owner, kinds=("SCAN_EBAY_BROAD", "SCAN_EBAY_PRIVATE", "SCAN_EBAY_CHARITY", "SCAN_EBAY_ENDGAME"))
                    if ebay_job is not None:
                        ebay_future = source_pool.submit(_run_source_job, config, ebay_job["id"], ebay_job["lease_token"])
                if shopify_future is None and (config.source_charity_shops or config.source_specialist_shops or config.source_publishers or config.source_abebooks):
                    shopify_job = claim_job(db, owner, kinds=("SCAN_SHOPIFY", "SCAN_ABEBOOKS"))
                    if shopify_job is not None:
                        shopify_future = source_pool.submit(_run_source_job, config, shopify_job["id"], shopify_job["lease_token"])
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
