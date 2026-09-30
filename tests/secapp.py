"""
Shared helpers for the Part A security regression tests.

Boots the REAL app via ``run.create_app()`` under the SANDBOX no-MySQL env (the
same pattern as test_conversation_resume / test_calendar_surfacing) so the tests
exercise the production route table, decorators and before_request hooks, not a
copy. The database is replaced by ``FakeMySQL`` whose cursor records every SQL
statement, so tests can also assert "this request never touched the DB".
"""

import os
import sys
from unittest import mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_SAFE_ENV = {
    "SANDBOX": "1",
    "AI_WARMUP_ON_IMPORT": "0",
    "SCHEDULER_OPPORTUNISTIC": "0",
    "MYSQL_HOST": "127.0.0.1",
    "MYSQL_PORT": "3306",
    "MYSQL_USER": "none",
    "MYSQL_PASSWORD": "none",
    "MYSQL_DB": "none",
    "OPENAI_API_KEY": "sk-test",
}
for _k, _v in _SAFE_ENV.items():
    os.environ.setdefault(_k, _v)

_APP = None


def get_app():
    """One real app per test process (create_app is expensive)."""
    global _APP
    if _APP is None:
        from run import create_app
        _APP = create_app()
        _APP.config["TESTING"] = True
        # No real MySQL in tests: the session-liveness gate must not try to
        # connect. Individual tests override this to exercise the gate.
        import auth_decorators
        auth_decorators._lookup_membership_status = lambda *a, **k: "active"
        # enterprise_api lazily creates its security tables on the first request
        # (a 2 s refused-connect on every request without MySQL): mark them ready.
        import enterprise_api
        enterprise_api._SECURITY_SCHEMA_READY = True
        for flag in (
            "_ai_subsystems_warmed",
            "_branding_schema_ensured",
            "_enterprise_tables_created",
            "_perf_indexes_ensured",
        ):
            setattr(_APP, flag, True)
    return _APP


class FakeCursor:
    """Records SQL. ``responder(sql, params)`` may return a row / list of rows."""

    def __init__(self, log, responder=None):
        self.log = log
        self.responder = responder
        self._rows = []
        self.rowcount = 1
        self.lastrowid = 1

    def execute(self, sql, params=None):
        self.log.append((" ".join(str(sql).split()), params))
        self._rows = []
        if self.responder:
            out = self.responder(sql, params)
            if out is None:
                self._rows = []
            elif isinstance(out, list):
                self._rows = list(out)
            else:
                self._rows = [out]
        return len(self._rows)

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def close(self):
        pass

    def __iter__(self):
        return iter(self._rows)


class FakeConnection:
    def __init__(self, log, responder=None):
        self.log = log
        self.responder = responder
        self.commits = 0

    def cursor(self, *a, **k):
        return FakeCursor(self.log, self.responder)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class FakeMySQL:
    def __init__(self, responder=None):
        self.log = []
        self.connection = FakeConnection(self.log, responder)

    def queries(self, needle=""):
        return [q for q in self.log if needle.lower() in q[0].lower()]


def patch_mysql(app, responder=None):
    """Context manager: swap ``app.mysql`` for a recording FakeMySQL."""
    fake = FakeMySQL(responder)
    return fake, mock.patch.object(app, "mysql", fake)


def login(client, **session_values):
    with client.session_transaction() as s:
        s.clear()
        s.update(session_values)


ROLES = {
    "anon": {},
    "employee": {"user": "emp", "user_id": 11, "role": "user",
                 "company_id": 7, "company_role": "employee"},
    "dept_head": {"user": "dh", "user_id": 12, "role": "user",
                  "company_id": 7, "company_role": "department_head"},
    "hr_manager": {"user": "hr", "user_id": 13, "role": "user",
                   "company_id": 7, "company_role": "hr_manager"},
    "company_admin": {"user": "ca", "user_id": 14, "role": "user",
                      "company_id": 7, "company_role": "company_admin"},
    "admin": {"user": "root", "user_id": 1, "role": "admin"},
}


def client_as(app, role_name):
    c = app.test_client()
    if ROLES[role_name]:
        login(c, **ROLES[role_name])
    return c
