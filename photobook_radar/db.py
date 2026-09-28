"""SQLite persistence with explicit migrations and short write transactions."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

MIGRATIONS = Path(__file__).parent / "migrations"


def connect(path: Path, *, existing: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if existing and not path.is_file():
        raise FileNotFoundError(f"Radar database does not exist: {path}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    if path.exists():
        path.chmod(0o600)
    return db


def migrate(db: sqlite3.Connection) -> None:
    current = int(db.execute("PRAGMA user_version").fetchone()[0])
    versions = sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    latest = int(versions[-1].name[:4]) if versions else 0
    if current > latest:
        raise RuntimeError(f"Database schema {current} is newer than this application ({latest})")
    for file in versions:
        version = int(file.name[:4])
        if version > current:
            script = file.read_text()
            try:
                db.executescript("BEGIN IMMEDIATE;\n" + script + f"\nPRAGMA user_version={version};\nCOMMIT;")
            except Exception:
                if db.in_transaction:
                    db.rollback()
                raise


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
