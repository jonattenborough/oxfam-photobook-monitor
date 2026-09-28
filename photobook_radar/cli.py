"""Installation, import, backup, and diagnostic entry point."""
from __future__ import annotations

import argparse
import getpass
import gzip
import hashlib
import json
import os
import platform
import secrets
import shutil
import sqlite3
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import load_config
from .db import connect, migrate, transaction
from .store import enqueue_job, now


def _db():
    config = load_config()
    db = connect(config.database, existing=True)
    migrate(db)
    return config, db


def init() -> None:
    config = load_config()
    config.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    config.data_dir.chmod(0o700)
    target = config.data_dir / "config.toml"
    if not target.exists():
        source = Path(__file__).parent.parent / "config.example.toml"
        shutil.copyfile(source, target)
        target.chmod(0o600)
    db = connect(config.database)
    migrate(db)
    db.close()
    print(json.dumps({"mode": config.mode, "data_dir": str(config.data_dir), "database": str(config.database), "version": __version__}, indent=2))


def set_passphrase() -> None:
    config = load_config()
    first = getpass.getpass("New dashboard passphrase: ")
    second = getpass.getpass("Repeat passphrase: ")
    if first != second or len(first) < 12:
        raise SystemExit("Passphrases must match and have at least 12 characters")
    salt = secrets.token_bytes(32)
    derived = hashlib.pbkdf2_hmac("sha256", first.encode(), salt, 600_000)
    target = config.data_dir / "access.json"
    target.write_text(json.dumps({"salt": salt.hex(), "hash": derived.hex(), "iterations": 600_000}))
    target.chmod(0o600)
    print("Dashboard passphrase saved locally")


def doctor() -> None:
    config = load_config()
    secrets_path = config.data_dir / "secrets.toml"
    secrets_data = tomllib.loads(secrets_path.read_text()) if secrets_path.exists() and not secrets_path.stat().st_mode & 0o077 else {}
    report = {
        "version": __version__, "host": platform.node(), "os": platform.platform(), "python": sys.version.split()[0],
        "mode": config.mode, "data_dir": str(config.data_dir), "database_exists": config.database.exists(),
        "free_gib": round(shutil.disk_usage(config.data_dir if config.data_dir.exists() else Path.home()).free / 1024**3, 2),
        "dashboard_passphrase_set": (config.data_dir / "access.json").exists(),
        "secrets_file_present": secrets_path.exists(),
        "secrets_owner_only": secrets_path.exists() and not bool(secrets_path.stat().st_mode & 0o077),
        "ebay_credentials_stored": bool(secrets_data.get("ebay", {}).get("client_id") and secrets_data.get("ebay", {}).get("client_secret")),
        "telegram_bot_stored": bool(secrets_data.get("telegram", {}).get("bot_token")),
        "telegram_chat_selected": bool(secrets_data.get("telegram", {}).get("chat_id")),
        "network_scanning_allowed": config.production and config.allow_marketplace_network,
        "phone_delivery_allowed": config.production and config.allow_real_notifications and config.notification_enabled,
    }
    if config.database.exists():
        db = connect(config.database, existing=True)
        report["schema_version"] = db.execute("PRAGMA user_version").fetchone()[0]
        report["integrity"] = db.execute("PRAGMA quick_check").fetchone()[0]
        report["listings"] = db.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        report["triage_pending"] = db.execute("SELECT COUNT(*) FROM jobs WHERE kind='TRIAGE' AND status='PENDING'").fetchone()[0]
        report["unfinished_windows"] = db.execute("SELECT COUNT(*) FROM search_windows WHERE complete=0").fetchone()[0]
        report["phone_outbox_pending"] = db.execute("SELECT COUNT(*) FROM notification_events WHERE status IN ('QUEUED','DELIVERY_UNKNOWN')").fetchone()[0]
        fresh = db.execute("SELECT value FROM health WHERE key='fresh_start_at'").fetchone()
        report["fresh_start_at"] = fresh[0] if fresh else None
        db.close()
    print(json.dumps(report, indent=2))


def backup() -> None:
    config, db = _db()
    folder = config.data_dir / "backups"
    folder.mkdir(mode=0o700, exist_ok=True)
    folder.chmod(0o700)
    name = datetime.now(timezone.utc).strftime("radar-%Y%m%dT%H%M%SZ.db")
    path = folder / name
    archive = folder / (name + ".gz")
    target = sqlite3.connect(path)
    db.backup(target)
    check = target.execute("PRAGMA integrity_check").fetchone()[0]
    target.close()
    db.close()
    path.chmod(0o600)
    if check != "ok":
        path.unlink(missing_ok=True)
        raise RuntimeError("Backup integrity check failed")
    try:
        with path.open("rb") as source, gzip.open(archive, "wb", compresslevel=3) as compressed:
            shutil.copyfileobj(source, compressed, length=1024 * 1024)
        archive.chmod(0o600)
    finally:
        path.unlink(missing_ok=True)
    # Retain hourly recovery points and spaced daily/weekly points without
    # duplicating large database files on a nearly full local disk.
    candidates = sorted(folder.glob("radar-*.db.gz"), reverse=True)
    keep = set(candidates[:24])
    days = set()
    weeks = set()
    for candidate in candidates:
        date = candidate.name[6:14]
        week = datetime.strptime(date, "%Y%m%d").strftime("%G-W%V")
        if date not in days and len(days) < 7:
            keep.add(candidate); days.add(date)
        if week not in weeks and len(weeks) < 4:
            keep.add(candidate); weeks.add(week)
    for candidate in candidates:
        if candidate not in keep:
            candidate.unlink()
    print(json.dumps({"backup": str(archive), "integrity": check, "bytes": archive.stat().st_size}, indent=2))


def restore_test(path: Path) -> None:
    config = load_config()
    folder = config.data_dir / "restore-tests"
    folder.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="isolated-", dir=folder) as temporary:
        isolated = Path(temporary)
        if path.suffix == ".gz":
            source_copy = isolated / "source.db"
            with gzip.open(path, "rb") as compressed, source_copy.open("wb") as target_file:
                shutil.copyfileobj(compressed, target_file, length=1024 * 1024)
        else:
            source_copy = path
        source = sqlite3.connect(f"file:{source_copy}?mode=ro", uri=True)
        target = sqlite3.connect(isolated / "restored.db")
        source.backup(target)
        source.close()
        result = target.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {name: target.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in ("listings", "observations", "jobs", "notification_events", "user_decisions", "search_windows")}
        target.close()
    if result != "ok":
        raise RuntimeError("Restore test failed integrity check")
    print(json.dumps({"isolated_restore": "verified and cleaned up", "network_and_notifications": "disabled", "integrity": result, "counts": counts}, indent=2))


def enqueue_backlog(limit: int) -> None:
    _, db = _db()
    rows = db.execute("SELECT l.id FROM listings l WHERE l.imported=0 AND l.processing='CAPTURED' AND l.title!='' AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.job_key='triage:'||l.id) ORDER BY l.id LIMIT ?", (limit,)).fetchall()
    created = 0
    with transaction(db):
        for row in rows:
            created += int(enqueue_job(db, f"triage:{row['id']}", "TRIAGE", listing_id=row["id"], priority=10))
    db.close()
    print(json.dumps({"selected": len(rows), "new_jobs": created}))


def start_fresh() -> None:
    """Retain the archive, stop stale work, and date the new monitoring epoch."""
    _, db = _db()
    with transaction(db):
        earlier = db.execute("SELECT value FROM health WHERE key='fresh_start_at'").fetchone()
        if earlier:
            result = {"fresh_start_at": earlier[0], "already_started": True, "historical_jobs_cancelled": 0}
        else:
            timestamp = now()
            cancelled = db.execute("UPDATE jobs SET status='CANCELLED',lease_token=NULL,lease_owner=NULL,lease_until=NULL,last_error='Historical archive; fresh monitoring requested' WHERE kind IN ('TRIAGE','VERIFY') AND status IN ('PENDING','RUNNING') AND listing_id IN (SELECT id FROM listings WHERE imported=1)").rowcount
            db.execute("INSERT INTO health(key,value,updated_at) VALUES('fresh_start_at',?,?)", (timestamp, timestamp))
            db.execute("INSERT INTO app_events(at,kind,detail_json) VALUES(?,?,?)", (timestamp, "FRESH_START", json.dumps({"historical_jobs_cancelled": cancelled, "archive_retained": True})))
            result = {"fresh_start_at": timestamp, "already_started": False, "historical_jobs_cancelled": cancelled}
    db.close()
    print(json.dumps(result))


def main() -> None:
    parser = argparse.ArgumentParser(prog="photobook-radar")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "doctor", "set-passphrase", "setup-telegram", "test-telegram", "setup-ebay", "link-identity-gaps", "start-fresh", "backup", "worker", "web", "stats"):
        commands.add_parser(name)
    imp = commands.add_parser("import-repo")
    imp.add_argument("repository", type=Path)
    github = commands.add_parser("import-github")
    github.add_argument("snapshot", type=Path)
    github_uncovered = commands.add_parser("import-uncovered-issues")
    github_uncovered.add_argument("snapshot", type=Path)
    github_reviews = commands.add_parser("import-github-reviews")
    github_reviews.add_argument("snapshot", type=Path)
    backlog = commands.add_parser("enqueue-backlog")
    backlog.add_argument("--limit", type=int, default=50000)
    restore = commands.add_parser("restore-test")
    restore.add_argument("backup", type=Path)
    args = parser.parse_args()
    if args.command == "init": init()
    elif args.command == "doctor": doctor()
    elif args.command == "set-passphrase": set_passphrase()
    elif args.command == "setup-telegram":
        from .telegram_setup import setup
        setup()
    elif args.command == "test-telegram":
        from .telegram_setup import send_test
        print(json.dumps(send_test(), indent=2))
    elif args.command == "setup-ebay":
        from .ebay_setup import setup
        setup()
    elif args.command == "link-identity-gaps":
        from .importer import link_identity_only_gaps
        _, db = _db()
        report = link_identity_only_gaps(db)
        db.close()
        print(json.dumps(report, indent=2))
    elif args.command == "start-fresh": start_fresh()
    elif args.command == "backup": backup()
    elif args.command == "restore-test": restore_test(args.backup)
    elif args.command == "enqueue-backlog": enqueue_backlog(args.limit)
    elif args.command == "import-repo":
        from .importer import import_repository
        _, db = _db()
        report = import_repository(db, args.repository.resolve())
        db.close()
        output = load_config().data_dir / "last-import-report.json"
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        output.chmod(0o600)
        print(json.dumps({"report": str(output), "commit": report["commit"], "counts": report["counts"], "reconciliation": report["reconciliation"]}, indent=2))
    elif args.command == "import-github":
        from .github_import import import_snapshot
        _, db = _db()
        report = import_snapshot(db, args.snapshot.resolve())
        db.close()
        print(json.dumps(report, indent=2))
    elif args.command == "import-github-reviews":
        from .github_import import import_historical_reviews
        _, db = _db()
        report = import_historical_reviews(db, args.snapshot.resolve())
        db.close()
        print(json.dumps(report, indent=2))
    elif args.command == "import-uncovered-issues":
        from .github_import import import_uncovered_issues
        _, db = _db()
        report = import_uncovered_issues(db, args.snapshot.resolve())
        db.close()
        print(json.dumps(report, indent=2))
    elif args.command == "worker":
        from .worker import run
        run()
    elif args.command == "web":
        import uvicorn
        config = load_config()
        uvicorn.run("photobook_radar.web:app", host=config.bind_host, port=config.port, workers=1, access_log=False)
    elif args.command == "stats":
        _, db = _db()
        for table in ("listings", "observations", "jobs", "notification_events", "imports", "legacy_objects", "search_windows", "book_records"):
            print(f"{table}: {db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]}")
        db.close()


if __name__ == "__main__":
    main()
