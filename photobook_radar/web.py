"""Read-mostly local dashboard. The web process never schedules scans."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Config, load_config
from .db import connect
from .store import decide, now, safe_url, undo_decision

config = load_config()
app = FastAPI(title="Photobook Radar", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
LONDON = ZoneInfo("Europe/London")
SOURCE_LANES = (
    (("oxfam-photography",), "Oxfam Photography", "source_oxfam_photography"),
    (("oxfam-broad",), "Oxfam Art & Photography", "source_oxfam_broad"),
    (("charity",), "Shelter and Crisis", "source_charity_shops"),
    (("specialist",), "Specialist bookshops", "source_specialist_shops"),
    (("ebay-broad",), "eBay broad books", "source_ebay_broad"),
    (("ebay-private",), "eBay private sellers", "source_ebay_private"),
    (("ebay-charity",), "eBay charity sellers", "source_ebay_charity"),
    (("ebay-endgame",), "eBay Endgame auctions", "source_ebay_endgame"),
    (("abebooks",), "AbeBooks targets", "source_abebooks"),
    (("research-wider",), "Wider web", "source_wider_web"),
    (("publisher", "research-publishers"), "Publishers and future canon", "source_publishers"),
    (("research-prizes",), "Photography prizes", "source_prizes"),
)


def _secret() -> bytes:
    target = config.data_dir / "web-session.key"
    if not target.exists():
        target.write_bytes(secrets.token_bytes(32))
        target.chmod(0o600)
    return target.read_bytes()


def _session(request: Request) -> str | None:
    value = request.cookies.get("radar_session", "")
    try:
        nonce, created, sig = value.split(".")
        if int(created) < time.time() - 60 * 60 * 24 * 14:
            return None
        expected = hmac.new(_secret(), f"{nonce}.{created}".encode(), "sha256").hexdigest()
        return value if hmac.compare_digest(expected, sig) else None
    except (ValueError, TypeError):
        return None


def _csrf(session: str) -> str:
    return hmac.new(_secret(), (session + ":csrf").encode(), "sha256").hexdigest()


def _authenticated(request: Request) -> bool:
    return _session(request) is not None


def _db() -> sqlite3.Connection:
    return connect(config.database, existing=True)


def _money(minor, currency) -> str:
    if minor is None:
        return "Unknown"
    symbol = "£" if currency == "GBP" else (currency or "") + " "
    return f"{symbol}{minor / 100:,.2f}"


def _local(value) -> str:
    if not value:
        return "Unknown"
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(LONDON).strftime("%d %b %Y, %H:%M %Z")
    except ValueError:
        return "Unknown"


templates.env.filters["money"] = _money
templates.env.filters["localtime"] = _local


def _context(request: Request, title: str, **kwargs):
    session = _session(request)
    return {"request": request, "title": title, "auth": bool(session), "csrf": _csrf(session) if session else "", "mode": config.mode, **kwargs}


@app.middleware("http")
async def guard(request: Request, call_next):
    host = request.headers.get("host", "")
    allowed = {f"127.0.0.1:{config.port}", f"localhost:{config.port}"}
    if host not in allowed:
        return HTMLResponse("Invalid host", status_code=400)
    if request.method == "POST":
        origin = request.headers.get("origin")
        if origin and origin not in {f"http://{value}" for value in allowed}:
            return HTMLResponse("Invalid origin", status_code=403)
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/healthz")
def healthz():
    db = _db()
    try:
        heartbeat = db.execute("SELECT value FROM health WHERE key='worker_heartbeat'").fetchone()
        return {"web": "up", "mode": config.mode, "worker_heartbeat": heartbeat[0] if heartbeat else None}
    finally:
        db.close()


def _cards(db: sqlite3.Connection, section: str, query: str, page: int) -> tuple[list[dict], int]:
    where = ["1=1"]
    args: list = []
    if section == "finds":
        where.append("l.imported=0")
        where.append("l.triage_score>=58 AND l.availability NOT IN ('ENDED','SOLD','REMOVED')")
        where.append("o.currency='GBP' AND o.price_minor<=15000")
        where.append("(o.auction_end_at IS NULL OR o.auction_end_at>?)")
        args.append(now())
        where.append("COALESCE(d.action,'') NOT IN ('DISMISS','BOUGHT','OWNED')")
    elif section == "urgent":
        where.append("l.imported=0")
        where.append("l.listing_type='AUCTION' AND o.auction_end_at>? AND l.triage_score>=58 AND o.currency='GBP' AND o.price_minor<=15000")
        args.append(now())
    elif section == "saved":
        where.append("d.action IN ('SAVE','WATCH')")
    if query:
        where.append("(l.title LIKE ? OR l.photographer LIKE ? OR l.external_id LIKE ?)")
        args.extend([f"%{query}%"] * 3)
    join = "LEFT JOIN user_decisions d ON d.id=(SELECT id FROM user_decisions WHERE listing_id=l.id AND undone_at IS NULL ORDER BY id DESC LIMIT 1)"
    base = " FROM listings l JOIN observations o ON o.id=l.current_observation_id " + join + " WHERE " + " AND ".join(where)
    fresh_after = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(timespec="seconds").replace("+00:00", "Z")
    sort = "o.auction_end_at ASC" if section == "urgent" else "CASE WHEN l.imported=0 AND o.observed_at>=? THEN 0 ELSE 1 END,COALESCE(l.triage_score,0) DESC,l.id DESC" if section == "finds" else "l.id DESC"
    if section == "finds":
        args.append(fresh_after)
    # ORDER BY placeholders must follow those in WHERE, and the count query
    # has no ORDER BY placeholder.
    count_args = args[:-1] if section == "finds" else args
    count = int(db.execute("SELECT COUNT(*)" + base, count_args).fetchone()[0])
    rows = db.execute("SELECT l.*,o.price_minor,o.currency,o.shipping_minor,o.shipping_currency,o.auction_end_at,o.observed_at,o.id AS observation_id,d.action AS user_action" + base + f" ORDER BY {sort} LIMIT 30 OFFSET ?", (*args, (page - 1) * 30)).fetchall()
    cards = [dict(row) for row in rows]
    for card in cards:
        card["older"] = bool(card["imported"] or not card["observed_at"] or card["observed_at"] < fresh_after)
        card["total_minor"] = card["price_minor"] + card["shipping_minor"] if card["price_minor"] is not None and card["shipping_minor"] is not None and card["currency"] == card["shipping_currency"] else None
    return cards, count


@app.get("/", response_class=HTMLResponse)
def home(request: Request, q: str = "", page: int = 1):
    return _listing_page(request, "finds", q, page)


@app.get("/finds", response_class=HTMLResponse)
@app.get("/urgent", response_class=HTMLResponse)
@app.get("/saved", response_class=HTMLResponse)
@app.get("/archive", response_class=HTMLResponse)
def listing_page(request: Request, q: str = "", page: int = 1):
    return _listing_page(request, request.url.path.strip("/"), q, page)


def _listing_page(request: Request, section: str, q: str, page: int):
    if section not in {"finds", "urgent", "saved", "archive"}:
        raise HTTPException(404)
    page = max(1, min(page, 100000))
    db = _db()
    try:
        cards, count = _cards(db, section, q[:100], page)
        pending = db.execute("SELECT COUNT(*) FROM jobs j JOIN listings l ON l.id=j.listing_id WHERE j.kind='TRIAGE' AND j.status='PENDING' AND l.imported=0").fetchone()[0]
        scanned = db.execute("SELECT COUNT(*) FROM listings WHERE processing='TRIAGED' AND imported=0").fetchone()[0]
        fresh = db.execute("SELECT value FROM health WHERE key='fresh_start_at'").fetchone()
    finally:
        db.close()
    return templates.TemplateResponse("listings.html", _context(request, section.title(), section=section, cards=cards, count=count, page=page, q=q, pending=pending, scanned=scanned, fresh_start=fresh[0] if fresh else None))


@app.get("/listing/{listing_id}", response_class=HTMLResponse)
def detail(request: Request, listing_id: int):
    db = _db()
    try:
        row = db.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
        if row is None:
            raise HTTPException(404)
        observations = [dict(x) for x in db.execute("SELECT * FROM observations WHERE listing_id=? ORDER BY id DESC LIMIT 20", (listing_id,))]
        live_checks = [dict(x) for x in db.execute("SELECT * FROM live_checks WHERE listing_id=? ORDER BY id DESC LIMIT 5", (listing_id,))]
        matches = [dict(x) for x in db.execute("SELECT * FROM recognition_matches WHERE listing_id=? ORDER BY score DESC LIMIT 10", (listing_id,))]
        reviews = [dict(x) for x in db.execute("SELECT * FROM reviews WHERE listing_id=? ORDER BY finished_at DESC LIMIT 10", (listing_id,))]
        for review in reviews:
            try:
                review["excerpt"] = json.loads(review["result_json"] or "{}").get("excerpt", "")
            except (json.JSONDecodeError, AttributeError):
                review["excerpt"] = ""
        decisions = [dict(x) for x in db.execute("SELECT * FROM user_decisions WHERE listing_id=? ORDER BY id DESC LIMIT 10", (listing_id,))]
        events = [dict(x) for x in db.execute("SELECT * FROM notification_events WHERE listing_id=? ORDER BY id DESC LIMIT 10", (listing_id,))]
    finally:
        db.close()
    return templates.TemplateResponse("detail.html", _context(request, row["title"], listing=dict(row), observations=observations, live_checks=live_checks, matches=matches, reviews=reviews, decisions=decisions, events=events, seller_url=safe_url(row["canonical_url"])))


@app.get("/system", response_class=HTMLResponse)
def system(request: Request):
    db = _db()
    try:
        counts = {key: db.execute(f"SELECT COUNT(*) FROM {key}").fetchone()[0] for key in ("listings", "observations", "imports", "search_windows")}
        counts["triaged"] = db.execute("SELECT COUNT(*) FROM listings WHERE processing='TRIAGED' AND imported=0").fetchone()[0]
        counts["fresh"] = db.execute("SELECT COUNT(*) FROM listings WHERE imported=0").fetchone()[0]
        counts["pending_jobs"] = db.execute("SELECT COUNT(*) FROM jobs WHERE status='PENDING'").fetchone()[0]
        counts["unfinished_windows"] = db.execute("SELECT COUNT(*) FROM search_windows WHERE complete=0").fetchone()[0]
        counts["quarantined_rows"] = db.execute("SELECT COUNT(*) FROM legacy_objects WHERE parse_status='QUARANTINED'").fetchone()[0]
        counts["live_checks"] = db.execute("SELECT COUNT(*) FROM live_checks").fetchone()[0]
        counts["browse_attempts"] = db.execute("SELECT COUNT(*) FROM api_requests r JOIN api_windows w ON w.id=r.window_id WHERE w.bucket='ebay_browse'").fetchone()[0]
        actual = {row["id"]: dict(row) for row in db.execute("SELECT * FROM sources WHERE status!='MANUAL_ONLY'")}
        sources = []
        for source_ids, label, field in SOURCE_LANES:
            rows = [actual.get(source_id, {}) for source_id in source_ids]
            routes = [route for source_id in source_ids for route in db.execute("SELECT last_attempt_at,last_success_at,incomplete_reason FROM source_routes WHERE source_id=? ORDER BY id", (source_id,))]
            selected = bool(getattr(config, field))
            statuses = {row.get("status") for row in rows if row.get("status")}
            status = "OFF (shadow)" if config.mode == "shadow" else "OFF (selection)" if not selected else "DEGRADED" if "DEGRADED" in statuses else "PARTIAL" if "PARTIAL" in statuses or sum(bool(row) for row in rows) < len(source_ids) else "ACTIVE" if statuses == {"ACTIVE"} else next(iter(statuses), "NOT CONFIGURED")
            sources.append({
                "id": label,
                "status": status,
                "last_success_at": max([row["last_success_at"] for row in rows if row.get("last_success_at")] + [route["last_success_at"] for route in routes if route["last_success_at"]], default=None),
                "last_attempt_at": max((route["last_attempt_at"] for route in routes if route["last_attempt_at"]), default=None),
                "last_error": "; ".join([row["last_error"] for row in rows if row.get("last_error")] + [route["incomplete_reason"] for route in routes if route["incomplete_reason"]])[:500],
            })
        heartbeat = db.execute("SELECT value FROM health WHERE key='worker_heartbeat'").fetchone()
        phone_test = db.execute("SELECT value FROM health WHERE key='telegram_phone_test_confirmed'").fetchone()
        fresh_start = db.execute("SELECT value FROM health WHERE key='fresh_start_at'").fetchone()
    finally:
        db.close()
    return templates.TemplateResponse("system.html", _context(request, "System", counts=counts, sources=sources, worker_heartbeat=heartbeat[0] if heartbeat else None, phone_test_confirmed=bool(phone_test), fresh_start=fresh_start[0] if fresh_start else None, alerts_enabled=config.production and config.allow_real_notifications and config.notification_enabled, research_provider=config.research_provider))


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", _context(request, "Sign in", configured=(config.data_dir / "access.json").exists(), error=None))


@app.post("/login")
def login(request: Request, passphrase: str = Form(...)):
    path = config.data_dir / "access.json"
    if not path.exists():
        raise HTTPException(403, "Dashboard passphrase has not been set")
    record = json.loads(path.read_text())
    computed = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), bytes.fromhex(record["salt"]), int(record["iterations"]))
    if not hmac.compare_digest(computed.hex(), record["hash"]):
        return templates.TemplateResponse("login.html", _context(request, "Sign in", configured=True, error="Incorrect passphrase"), status_code=403)
    nonce = secrets.token_hex(16)
    created = str(int(time.time()))
    signature = hmac.new(_secret(), f"{nonce}.{created}".encode(), "sha256").hexdigest()
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("radar_session", f"{nonce}.{created}.{signature}", httponly=True, samesite="strict", secure=False, max_age=60 * 60 * 24 * 14)
    return response


def _check_action(request: Request, csrf: str) -> None:
    session = _session(request)
    if not session:
        raise HTTPException(401, "Sign in to change a listing")
    if not hmac.compare_digest(_csrf(session), csrf):
        raise HTTPException(403, "Invalid form token")


@app.post("/listing/{listing_id}/decision")
def action(request: Request, listing_id: int, action: str = Form(...), note: str = Form(""), csrf: str = Form(...)):
    _check_action(request, csrf)
    db = _db()
    try:
        decide(db, listing_id, action, note)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        db.close()
    return RedirectResponse(f"/listing/{listing_id}", status_code=303)


@app.post("/decision/{decision_id}/undo")
def undo(request: Request, decision_id: int, csrf: str = Form(...)):
    _check_action(request, csrf)
    db = _db()
    try:
        row = db.execute("SELECT listing_id FROM user_decisions WHERE id=?", (decision_id,)).fetchone()
        if row is None:
            raise HTTPException(404)
        undo_decision(db, decision_id)
        listing_id = row[0]
    finally:
        db.close()
    return RedirectResponse(f"/listing/{listing_id}", status_code=303)
