"""Shared pytest setup.

* Forces UTF-8 text IO assumptions for the suite (templates contain æøå).
* When no MySQL is reachable (local dev without the sandbox container, or a CI
  job whose service is not up), ``pymysql.connect`` fails instantly instead of
  blocking on the OS connect timeout.  That was what hung
  ``test_fabricated_price_triggers_grounding_disclaimer`` until the 6 h CI limit:
  every best-effort DB write in the SSE pipeline waited for a TCP timeout.
  Tests that need a real database already skip or mock it.
"""

import os
import socket

import pytest

os.environ.setdefault("SANDBOX", "1")
os.environ.setdefault("AI_WARMUP_ON_IMPORT", "0")
os.environ.setdefault("SCHEDULER_OPPORTUNISTIC", "0")
os.environ.setdefault("AI_MEMORY_BACKEND", "sqlite")  # AI store tests opt in to MySQL explicitly
# create_app() must not run the full enterprise-table sync on the first request: against
# the CI MySQL service it takes minutes and tripped the per-test timeout. The schema is
# built explicitly by tests/test_schema_baseline.py.
os.environ["ENTERPRISE_TABLE_SYNC_SKIP"] = "1"
os.environ.setdefault("CATALOG_AUTO_EMBED", "0")  # never call the embedding API from tests


def _mysql_reachable() -> bool:
    host = os.environ.get("MYSQL_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("MYSQL_PORT", "3306"))
    except ValueError:
        port = 3306
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session", autouse=True)
def _fail_fast_without_mysql():
    if _mysql_reachable():
        yield
        return
    import pymysql

    original = pymysql.connect

    def _refuse(*args, **kwargs):
        raise pymysql.err.OperationalError(2003, "MySQL unavailable in this test session")

    pymysql.connect = _refuse
    try:
        yield
    finally:
        pymysql.connect = original


@pytest.fixture(autouse=True)
def _session_liveness_isolated(monkeypatch):
    """Tests build signed-in sessions by hand, without matching ``company_users``
    rows. Treat every membership as active (role taken from the session) and start
    each test with an empty liveness cache, so results never depend on test order.
    Tests of the liveness gate itself patch ``_lookup_membership_status`` again."""
    import auth_decorators

    monkeypatch.setattr(auth_decorators, "_lookup_membership_status", lambda *a, **k: "active")
    auth_decorators.invalidate_session_cache()
    yield
    auth_decorators.invalidate_session_cache()


@pytest.fixture(autouse=True)
def _confirm_store_isolated():
    """confirm_store caches 'table ready' and pending tokens at module level; both
    must not leak between tests that each bring their own database."""
    from app1 import confirm_store

    confirm_store._table_ready = False
    confirm_store._STORE.clear()
    yield
    confirm_store._table_ready = False
    confirm_store._STORE.clear()
