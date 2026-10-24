"""Shared fixtures.

DB tests need a Postgres with the pgvector extension. Set TEST_DATABASE_URL to any database
on that server (a superuser or CREATEDB role); each test run gets its own throwaway database.
Without it, DB tests are skipped and everything else still runs.

    TEST_DATABASE_URL=postgresql+psycopg://refund:change-me@localhost:5432/postgres
"""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

BACKEND_DIR = Path(__file__).resolve().parents[1]


def alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest.fixture(scope="session")
def admin_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set; skipping database tests")
    return url


@pytest.fixture
def empty_db_url(admin_url: str) -> Iterator[str]:
    """A fresh, empty database (no tables), dropped afterwards."""
    name = f"rm_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def migrated_db_url(empty_db_url: str) -> str:
    """A fresh database migrated to head."""
    command.upgrade(alembic_config(empty_db_url), "head")
    return empty_db_url
