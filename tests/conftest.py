"""pytest configuration: project paths, isolated PostgreSQL DBs + gateway factory.

Tests need a live PostgreSQL (gateway stores are no longer SQLite files): by
default it is the container from docker-compose.test.yml (127.0.0.1:55433).
Each test gets its own DB: it is created in the ``pg_dsn`` fixture and dropped
afterwards (``WITH (FORCE)`` severs forgotten connections). The instance can be
overridden with the ``OPENROUTE_TEST_DSN`` env var.
"""

import os
import sys
import uuid
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
for _p in (str(PROJECT_ROOT), str(TESTS_DIR)):
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
    """An isolated DB per test: the DSN the gateway connects to PostgreSQL with."""
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
    """Close the reqlog pool created when app.main was imported (DSN from .env).

    Otherwise psycopg_pool workers "say goodbye" via __del__ on an already
    closed stderr at interpreter exit — a flood of Logging errors in pytest's
    output. By then monkeypatch is undone, so main.reqlog is the import-time
    instance.
    """
    yield
    import app.main as main
    try:
        main.reqlog.close()
    except Exception:
        pass


@pytest.fixture()
def make_gateway(monkeypatch, pg_dsn):
    """Gateway factory; every TestClient is closed carefully after the test."""
    made = []

    def _make(**kwargs):
        gw = Gateway(monkeypatch, pg_dsn, **kwargs)
        made.append(gw)
        return gw

    yield _make

    for gw in reversed(made):
        try:
            gw.close()
        except Exception:
            pass
