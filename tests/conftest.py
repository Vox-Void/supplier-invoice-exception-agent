import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from invoice_agent.fixtures import headline_snapshot
from invoice_agent.store import Store


@pytest.fixture
def seeded_store() -> Iterator[Store]:
    dsn = os.environ.get("INVOICE_AGENT_TEST_DSN")
    if not dsn:
        pytest.skip("Set INVOICE_AGENT_TEST_DSN to run PostgreSQL tests")
    database = f"invoice_test_{uuid4().hex}"
    identifier = sql.Identifier(database)
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(identifier))
        try:
            store = Store(make_conninfo(dsn, dbname=database))
            store.migrate(Path("db/schema.sql"))
            store.seed_snapshot(headline_snapshot())
            yield store
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(identifier))
