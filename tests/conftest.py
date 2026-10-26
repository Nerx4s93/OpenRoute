"""pytest configuration: project paths, isolated PostgreSQL DBs + gateway factory.

Gateway tests are hermetic: every persistent domain goes through the in-memory
``FakeStorage``, so no database is needed for them. Only the real-SQL suite for
``storage-service`` (``test_storage_service.py``) uses the ``pg_dsn`` fixture:
by default it is the container from docker-compose.test.yml
(127.0.0.1:55433). Each such test gets its own DB: it is created in the fixture
and dropped afterwards (``WITH (FORCE)`` severs forgotten connections). The
instance can be overridden with the ``OPENROUTE_TEST_DSN`` env var.
"""

import os
import sys
import uuid
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
# storage-service/ is on PYTHONPATH so tests import openroute_storage exactly
# like the container does (WORKDIR /srv, package at /srv/openroute_storage).
for _p in (str(PROJECT_ROOT), str(TESTS_DIR), str(PROJECT_ROOT / "storage-service")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from gw import Gateway

TEST_DSN = os.environ.get(
    "OPENROUTE_TEST_DSN",
    # 127.0.0.1, not localhost: on Windows libpq may resolve localhost to ::1
    # (IPv6), while the test container listens on IPv4 only.
    "postgresql://openroute:openroute@127.0.0.1:55433/openroute_test",
)


def _admin_connect():
    return psycopg.connect(TEST_DSN, autocommit=True, connect_timeout=5)


@pytest.fixture()
def pg_dsn():
    """An isolated DB per test (storage-service real-SQL suite only)."""
    dbname = f"openroute_test_{uuid.uuid4().hex[:12]}"
    try:
        with _admin_connect() as admin:
            admin.execute(f'CREATE DATABASE "{dbname}"')
    except psycopg.OperationalError as exc:
        pytest.fail(
            f"No PostgreSQL access for tests: {exc}\n"
            "Run: docker compose -f docker-compose.test.yml up -d "
            "(or point OPENROUTE_TEST_DSN at a live DB)."
        )
    yield make_conninfo(TEST_DSN, dbname=dbname)
    with _admin_connect() as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


@pytest.fixture(scope="session", autouse=True)
def _close_import_time_pools():
    """Nothing is opened at import time: the request log is storage-backed and
    created in the lifespan.

    Kept as a safety net: if a test leaked a facade bound to a closed loop, mark
    it closed here so no fire-and-forget task is left pending at interpreter
    exit. ``main.reqlog`` is ``None`` after every clean shutdown.
    """
    yield
    import asyncio

    import gateway.main as main
    log = main.reqlog
    if log is not None:
        try:
            asyncio.run(log.close())
        except Exception:
            pass


@pytest.fixture()
def make_gateway(monkeypatch):
    """Gateway factory; every TestClient is closed carefully after the test."""
    made = []

    def _make(**kwargs):
        gw = Gateway(monkeypatch, **kwargs)
        made.append(gw)
        return gw

    yield _make

    for gw in reversed(made):
        try:
            gw.close()
        except Exception:
            pass
