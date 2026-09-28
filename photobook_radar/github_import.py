"""Import a verified, complete paginated GitHub snapshot without remote writes."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from .db import transaction
from .store import capture, now

PREFIXES = ("ENDGAME_4H:", "ENDGAME_EARLY:", "EBAY_PRIVATE_NEW:", "CHARITY_NEW:", "EXTERNAL_NEW:", "OXFAM_NEW:", "OXFAM_ART_NEW:", "EBAY_PRIVATE_BACKFILL:", "EBAY_PRIVATE_GLOBAL_BACKFILL:", "EBAY_BACKFILL:", "EBAY_PRIVATE_FORENSIC_READY:")
UNCOVERED_PREFIXES = ("ENDGAME_15:", "ENDGAME_90:", "INITIAL_SWEEP:", "OXFAM_CATALOGUE_AUDIT:", "OXFAM_ART_SCAN:", "OXFAM_FULL_SCAN_READY:")
URL = re.compile(r"https://[^\s)<>]+", re.I)
EBAY_ID = re.compile(r"ebay\.[a-z.]+/itm/(?:[^/\s)]+/)?(\d{9,15})", re.I)
SKU = re.compile(r"HD_\d+")
DETECTED = re.compile(r"Detected at\s*\*\*?([^*\s]+)", re.I)
MONEY = re.compile(r"(?:GBP\s*|£|USD\s*|EUR\s*|CHF\s*|\$|€)\s*[\d,]+(?:\.\d{1,2})?", re.I)
END = re.compile(r"\*\*Ends:\*\*\s*(\d{4}-\d\d-\d\dT[\d:.]+Z)")


def _money(section: str) -> tuple[str | None, str | None]:
    line = next((line for line in section.splitlines() if any(term in line for term in ("Observed price:", "Bid/price:", "Oxfam price:", "**Price:**"))), "")
    match = MONEY.search(line)
    if not match:
        return None, None
    value = match.group().replace(",", "").strip()
    for prefix, currency in (("GBP", "GBP"), ("£", "GBP"), ("USD", "USD"), ("$", "USD"), ("EUR", "EUR"), ("€", "EUR"), ("CHF", "CHF")):
        if value.upper().startswith(prefix):
            return value[len(prefix):].strip(), currency
    return None, None


def _sections(issue: dict):
    body = str(issue.get("body") or "")
    detected = DETECTED.search(body)
    timestamp = detected.group(1) if detected else issue.get("created_at")
    for index, section in enumerate(re.split(r"(?m)^###\s+", body)[1:]):
        heading = section.splitlines()[0].strip()
        title = re.sub(r"^\[[^]]+\]\([^)]*\)$", lambda m: m.group()[1:].split("](", 1)[0], heading)
        title = re.sub(r"^(?:URGENT|REVIEW|HOT|CHECK)\s+\d+/100\s+-\s+", "", title)
        urls = URL.findall(section)
        listing_url = next((url.rstrip(".,;~") for url in urls if "/itm/" in url or "/product/" in url or "/products/" in url or "abebooks." in url), None)
        if not listing_url:
            match = re.match(r"\[[^]]+\]\((https://[^)]+)\)", heading)
            if match:
                listing_url = match.group(1)
        if not listing_url:
            continue
        listing_url = listing_url.replace("&amp;", "&")
        price, currency = _money(section)
        item = {"title": title[:350], "url": listing_url, "first_seen": timestamp, "observed_at": timestamp, "context": section[:5000]}
        if price is not None:
            item.update({"price_value": price, "price_currency": currency})
            if currency == "GBP":
                item["price_gbp"] = price
        end = END.search(section)
        if end:
            item["item_end_date"] = end.group(1)
            item["buying_options"] = ["AUCTION"]
        ebay = EBAY_ID.search(listing_url)
        sku = SKU.search(section)
        if ebay:
            item["external_id"] = ebay.group(1)
            item["key"] = "ebay:" + ebay.group(1)
        elif sku:
            item["sku"] = sku.group()
        source = issue["title"].split(":", 1)[0].lower()
        yield index, item, source


def _pages(snapshot: Path, label: str, manifest: dict):
    endpoint = manifest["endpoints"].get(label, {})
    if not endpoint.get("complete"):
        raise ValueError(f"GitHub {label} export is incomplete")
    for meta in endpoint["pages"]:
        path = snapshot / label / f"page-{meta['page']:05d}.json"
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != meta["sha256"]:
            raise RuntimeError(f"GitHub export hash mismatch: {path}")
        yield path, meta, json.loads(raw)


def _register_import(db: sqlite3.Connection, origin: str, digest: str, byte_size: int, total: int) -> int | None:
    with transaction(db):
        db.execute("INSERT OR IGNORE INTO imports(origin,sha256,byte_size,rows_total) VALUES(?,?,?,?)", (origin, digest, byte_size, total))
        row = db.execute("SELECT id,completed_at FROM imports WHERE origin=?", (origin,)).fetchone()
        return None if row["completed_at"] else row["id"]


def _record(db: sqlite3.Connection, import_id: int, key: str, kind: str, status: str, listing_id: int | None, raw: dict, error: str | None = None) -> None:
    inserted = db.execute("INSERT OR IGNORE INTO legacy_objects(import_id,object_key,listing_id,object_type,parse_status,source_timestamp,raw_json,error) VALUES(?,?,?,?,?,?,?,?)", (import_id, key, listing_id, kind, status, raw.get("created_at") or raw.get("first_seen"), json.dumps(raw, ensure_ascii=False), error))
    if inserted.rowcount:
        column = {"MAPPED": "rows_mapped", "QUARANTINED": "rows_quarantined", "NON_LISTING": "rows_nonlisting"}[status]
        db.execute(f"UPDATE imports SET {column}={column}+1 WHERE id=?", (import_id,))


def _finish(db: sqlite3.Connection, import_id: int, total: int) -> None:
    with transaction(db):
        count = db.execute("SELECT rows_mapped+rows_quarantined+rows_nonlisting FROM imports WHERE id=?", (import_id,)).fetchone()[0]
        if count != total:
            raise RuntimeError(f"GitHub import accounting mismatch: {count} / {total}")
        db.execute("UPDATE imports SET completed_at=? WHERE id=?", (now(), import_id))


def import_snapshot(db: sqlite3.Connection, snapshot: Path) -> dict:
    manifest = json.loads((snapshot / "manifest.json").read_text())
    if not manifest.get("complete"):
        raise ValueError("GitHub export is incomplete; resume it before import")
    result = {"issues": 0, "pull_requests": 0, "comments": 0, "candidate_references": 0, "mapped": 0, "quarantined": 0}
    for path, meta, issues in _pages(snapshot, "issues", manifest):
        rows = []
        for issue in issues:
            if "pull_request" in issue:
                rows.append(("pull_request", issue, []))
            else:
                sections = list(_sections(issue)) if issue.get("title", "").startswith(PREFIXES) else []
                rows.append(("issue", issue, sections))
        total = sum(1 + len(sections) for _, _, sections in rows)
        origin = f"github:issues:{meta['sha256']}"
        import_id = _register_import(db, origin, meta["sha256"], meta["bytes"], total)
        if import_id is not None:
            for kind, issue, sections in rows:
                number = issue["number"]
                with transaction(db):
                    _record(db, import_id, f"{origin}:issue:{number}", kind, "NON_LISTING", None, issue, "pull request excluded" if kind == "pull_request" else "container; candidate sections imported separately")
                    for index, item, source in sections:
                        key = f"{origin}:issue:{number}:section:{index}"
                        try:
                            db.execute("INSERT OR IGNORE INTO sources(id,adapter,status) VALUES(?,?,'MANUAL_ONLY')", (source, source))
                            listing_id = capture(db, item, source_id=source, origin_key=key, imported=True)
                            status, error = "MAPPED", None
                        except (ValueError, sqlite3.IntegrityError) as exc:
                            listing_id, status, error = None, "QUARANTINED", str(exc)[:300]
                        _record(db, import_id, key, "issue_candidate", status, listing_id, item, error)
            _finish(db, import_id, total)
        result["pull_requests"] += sum(kind == "pull_request" for kind, _, _ in rows)
        result["issues"] += sum(kind == "issue" for kind, _, _ in rows)
        result["candidate_references"] += sum(len(sections) for _, _, sections in rows)
    for path, meta, comments in _pages(snapshot, "comments", manifest):
        origin = f"github:comments:{meta['sha256']}"
        import_id = _register_import(db, origin, meta["sha256"], meta["bytes"], len(comments))
        if import_id is not None:
            for comment in comments:
                with transaction(db):
                    _record(db, import_id, f"{origin}:comment:{comment['id']}", "issue_comment", "NON_LISTING", None, comment, "historical comment, not a fresh appraisal")
            _finish(db, import_id, len(comments))
        result["comments"] += len(comments)
    result["mapped"] = db.execute("SELECT SUM(rows_mapped) FROM imports WHERE origin LIKE 'github:%'").fetchone()[0]
    result["quarantined"] = db.execute("SELECT SUM(rows_quarantined) FROM imports WHERE origin LIKE 'github:%'").fetchone()[0]
    result["unique_listings_total"] = db.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    return result


def import_uncovered_issues(db: sqlite3.Connection, snapshot: Path) -> dict:
    """Recover candidate sections from legacy Issue types omitted by v1."""
    manifest = json.loads((snapshot / "manifest.json").read_text())
    report = {"candidate_references": 0, "mapped": 0, "quarantined": 0}
    for _, meta, issues in _pages(snapshot, "issues", manifest):
        rows = [(issue, list(_sections(issue))) for issue in issues if "pull_request" not in issue and str(issue.get("title") or "").startswith(UNCOVERED_PREFIXES)]
        total = sum(len(sections) for _, sections in rows)
        if not total:
            continue
        origin = f"github:uncovered:v2:{meta['sha256']}"
        import_id = _register_import(db, origin, meta["sha256"], meta["bytes"], total)
        if import_id is not None:
            for issue, sections in rows:
                with transaction(db):
                    for index, item, source in sections:
                        key = f"{origin}:issue:{issue['number']}:section:{index}"
                        try:
                            db.execute("INSERT OR IGNORE INTO sources(id,adapter,status) VALUES(?,?,'MANUAL_ONLY')", (source, source))
                            listing_id = capture(db, item, source_id=source, origin_key=key, imported=True)
                            status, error = "MAPPED", None
                        except (ValueError, sqlite3.IntegrityError) as exc:
                            listing_id, status, error = None, "QUARANTINED", str(exc)[:300]
                        _record(db, import_id, key, "issue_candidate", status, listing_id, item, error)
            _finish(db, import_id, total)
        report["candidate_references"] += total
    report["mapped"] = db.execute("SELECT COALESCE(SUM(rows_mapped),0) FROM imports WHERE origin LIKE 'github:uncovered:%'").fetchone()[0]
    report["quarantined"] = db.execute("SELECT COALESCE(SUM(rows_quarantined),0) FROM imports WHERE origin LIKE 'github:uncovered:%'").fetchone()[0]
    return report


def import_historical_reviews(db: sqlite3.Connection, snapshot: Path) -> dict:
    """Link per-listing owner comments as dated claims, never current appraisals."""
    manifest = json.loads((snapshot / "manifest.json").read_text())
    issue_candidates: dict[int, list[dict]] = {}
    for row in db.execute(
        "SELECT x.object_key,x.listing_id,l.external_id,l.canonical_url FROM legacy_objects x "
        "JOIN listings l ON l.id=x.listing_id WHERE x.object_type='issue_candidate' AND x.parse_status='MAPPED'"
    ):
        match = re.search(r":issue:(\d+):section:\d+$", row["object_key"])
        if match:
            issue_candidates.setdefault(int(match.group(1)), []).append(dict(row))
    report = {"comments_seen": 0, "comments_with_review_marker": 0, "claims_linked": 0, "unlinked_review_comments": 0}
    for _, _, comments in _pages(snapshot, "comments", manifest):
        for comment in comments:
            report["comments_seen"] += 1
            body = str(comment.get("body") or "")
            if not (body.startswith("CHATGPT_GEM_REVIEWED:") or body.startswith("CHATGPT_EXTERNAL_REVIEWED:")):
                continue
            report["comments_with_review_marker"] += 1
            issue_match = re.search(r"/(\d+)$", str(comment.get("issue_url") or ""))
            candidates = issue_candidates.get(int(issue_match.group(1)), []) if issue_match else []
            links: dict[int, tuple[str | None, str]] = {}
            for line in body.splitlines():
                if "https://" not in line:
                    continue
                verdict_match = re.search(r"\b(PASS|BUY NOW|INVESTIGATE|WATCH|MAKE OFFER|BID NOW)\b", line, re.I)
                urls = [value.rstrip(".,;)~") for value in URL.findall(line)]
                for candidate in candidates:
                    seller_url = str(candidate["canonical_url"] or "").split("?", 1)[0]
                    external = str(candidate["external_id"] or "")
                    matched = any(seller_url and seller_url in url or external and len(external) >= 8 and external in url for url in urls)
                    if matched:
                        links[candidate["listing_id"]] = (verdict_match.group(1).upper().replace(" ", "_") if verdict_match else None, line[:2000])
            if not links and len(candidates) == 1:
                links[candidates[0]["listing_id"]] = (None, body[:2000])
            if not links:
                report["unlinked_review_comments"] += 1
                continue
            with transaction(db):
                for listing_id, (verdict, excerpt) in links.items():
                    origin = f"github:comment:{comment['id']}:listing:{listing_id}"
                    outcome = db.execute(
                        "INSERT OR IGNORE INTO reviews(listing_id,observation_id,provider,policy_hash,verdict,confidence,status,started_at,finished_at,result_json,origin_key) "
                        "VALUES(?,NULL,'legacy_github_comment','legacy:unverified',?,NULL,'HISTORICAL_CLAIM',?,?,?,?)",
                        (listing_id, verdict, comment.get("created_at"), comment.get("updated_at") or comment.get("created_at"), json.dumps({"comment_id": comment["id"], "issue": int(issue_match.group(1)) if issue_match else None, "excerpt": excerpt, "warning": "Historical owner comment; current listing and valuation not verified"}, ensure_ascii=False), origin),
                    )
                    report["claims_linked"] += outcome.rowcount
    return report
