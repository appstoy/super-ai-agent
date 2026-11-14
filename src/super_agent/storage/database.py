"""Database engine and session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from super_agent.storage.models import Base


def create_engine_for_url(database_url: str) -> Engine:
    """Create a synchronous engine for local and worker-side transactions."""

    if not database_url.startswith("sqlite"):
        return create_engine(database_url, future=True)
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=30000")
        if database_url != "sqlite:///:memory:":
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create explicit sessions with no hidden global transaction."""

    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def create_schema(engine: Engine) -> None:
    """Create metadata for tests and local smoke checks.

    ``create_all`` creates missing tables but never adds an index to a table
    that already exists, so an upgraded checkout would keep the old unindexed
    queue. The indexes are therefore also created idempotently here; Alembic
    remains the authority for managed deployments.
    """

    Base.metadata.create_all(engine)
    _ensure_indexes(engine)


def _ensure_indexes(engine: Engine) -> None:
    """Add metadata-declared indexes that an existing database is missing."""

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present = {index["name"] for index in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if index.name in present:
                continue
            try:
                index.create(engine, checkfirst=True)
            except Exception:  # noqa: BLE001 - a missing index must not block startup
                pass


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Yield a transaction-scoped session and roll back failures."""

    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

