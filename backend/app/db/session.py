"""Sync engine and session helpers (used by migrations, seeding and scripts).

The async engine for FastAPI is added in Phase 8; it uses the same psycopg 3 URL.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings, runtime_database_url


def make_engine(url: str | None = None) -> Engine:
    return create_engine(
        url or runtime_database_url(get_settings()),
        pool_pre_ping=True,
    )


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Commit on success, roll back on error."""
    factory = sessionmaker(engine or make_engine(), expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def read_only_session(engine: Engine) -> Iterator[Session]:
    """A session whose transaction is READ ONLY. Postgres itself rejects any write, so a
    read-only tool cannot change data even through a bug. Always rolls back."""
    session = sessionmaker(engine, expire_on_commit=False)()
    try:
        session.execute(text("SET TRANSACTION READ ONLY"))
        yield session
    finally:
        session.rollback()
        session.close()
