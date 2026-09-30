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
