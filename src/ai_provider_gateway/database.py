"""SQLAlchemy engine construction without import-time filesystem writes."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url


def ensure_sqlite_parent(database_url: str) -> None:
    """Create the parent directory for a file-backed SQLite database."""

    url = make_url(database_url)
    if url.drivername != "sqlite" or not url.database or url.database == ":memory:":
        return
    Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def create_database_engine(database_url: str) -> Engine:
    """Build a future-style SQLAlchemy engine for the configured URL."""

    ensure_sqlite_parent(database_url)
    return create_engine(database_url)
