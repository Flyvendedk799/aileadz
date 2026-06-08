"""Tests for the nightly company_analytics rollup (plan #18).

Covers:
  * ``scheduler.company_analytics_snapshot`` builds the right column set and
    issues an idempotent ``INSERT ... ON DUPLICATE KEY UPDATE`` upsert,
  * it is fully guarded (never raises; rolls back on a DB error),
  * the ``company_analytics_rollup`` job is registered, daily, and isolates a
    single company's failure from the rest of the batch,
  * ``multitenant_reports._daily_usage_from_snapshot`` serves the daily series
    from a fresh snapshot but falls back (returns None) when absent/stale.

No live DB or Flask app — fakes mirror tests/test_scheduler.py. No SANDBOX/MySQL
is required.
"""

import datetime
import unittest

import run  # noqa: F401  installs the pymysql->MySQLdb shim used by multitenant_reports
import scheduler


# ── Sequenced fake cursor: returns a different fetchone per execute ──────────

class SeqCursor:
    """Fake cursor that returns successive ``fetchone`` rows as execute() is
    called, and records every (sql, params) pair so the test can assert shape.
    """

    def __init__(self, fetchone_seq=None, fetchall_seq=None, raise_on_execute=False):
        self.executed = []
        self.closed = False
        self.rowcount = 1
        self._fetchone_seq = list(fetchone_seq or [])
        self._fetchall_seq = list(fetchall_seq or [])
        self._raise = raise_on_execute

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self._raise:
            raise RuntimeError("execute blew up")

    def fetchone(self):
        return self._fetchone_seq.pop(0) if self._fetchone_seq else None

    def fetchall(self):
        return self._fetchall_seq.pop(0) if self._fetchall_seq else []

    def close(self):
        self.closed = True


class SeqConnection:
    def __init__(self, cursor):
        self.commits = 0
        self.rollbacks = 0
        self._cursor = cursor

    def cursor(self, *a, **k):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


# ── company_analytics_snapshot ───────────────────────────────────────────────

class SnapshotTests(unittest.TestCase):
    def _ok_cursor(self):
        # 3 SELECTs: (queries+feedback), (started/completed), (active_users).
        return SeqCursor(fetchone_seq=[
            {'total_queries': 42, 'avg_feedback': 4.5},
            {'started': 7, 'completed': 3},
            {'active_users': 11},
        ])

    def test_upsert_is_idempotent_and_well_shaped(self):
        cur = self._ok_cursor()
        conn = SeqConnection(cur)
        ok = scheduler.company_analytics_snapshot(conn, 5, day='2026-06-07')
        self.assertTrue(ok)
        self.assertEqual(conn.commits, 1)

        # The final statement must be an idempotent upsert into company_analytics
        # touching exactly the table's KPI columns.
        upsert_sql, upsert_params = cur.executed[-1]
        self.assertIn("INSERT INTO company_analytics", upsert_sql)
        self.assertIn("ON DUPLICATE KEY UPDATE", upsert_sql)
        for col in ("employee_satisfaction_score", "active_users", "total_queries",
                    "courses_started", "courses_completed"):
            self.assertIn(col, upsert_sql)
        # company_id, date, satisfaction(4.5), active_users(11), queries(42),
        # started(7), completed(3) all flow through as params.
        self.assertIn(5, upsert_params)
        self.assertIn('2026-06-07', upsert_params)
        self.assertIn(42, upsert_params)
        self.assertIn(11, upsert_params)
        self.assertIn(7, upsert_params)
        self.assertIn(3, upsert_params)
        self.assertIn(4.5, upsert_params)

    def test_every_query_is_company_scoped(self):
        cur = self._ok_cursor()
        conn = SeqConnection(cur)
        scheduler.company_analytics_snapshot(conn, 9, day='2026-06-07')
        # The three read passes (all but the final INSERT) must scope to the
        # company id we passed; the upsert also carries it as the first VALUE.
        for sql, params in cur.executed:
            self.assertIn(9, params)
        for sql, _params in cur.executed[:-1]:
            self.assertIn("company_id = %s", sql)

    def test_null_feedback_yields_none_satisfaction(self):
        cur = SeqCursor(fetchone_seq=[
            {'total_queries': 0, 'avg_feedback': None},
            {'started': 0, 'completed': 0},
            {'active_users': 0},
        ])
        conn = SeqConnection(cur)
        ok = scheduler.company_analytics_snapshot(conn, 1, day='2026-06-07')
        self.assertTrue(ok)
        _, upsert_params = cur.executed[-1]
        self.assertIn(None, upsert_params)  # satisfaction stays NULL, not 0

    def test_default_day_uses_yesterday_sql_not_a_bound_date(self):
        cur = self._ok_cursor()
        conn = SeqConnection(cur)
        scheduler.company_analytics_snapshot(conn, 2)  # no day -> yesterday
        # When day is None the SQL must compute the day server-side.
        joined = " ".join(sql for sql, _ in cur.executed)
        self.assertIn("DATE_SUB(CURDATE(), INTERVAL 1 DAY)", joined)

    def test_none_conn_returns_false(self):
        self.assertFalse(scheduler.company_analytics_snapshot(None, 1))

    def test_none_company_returns_false(self):
        conn = SeqConnection(self._ok_cursor())
        self.assertFalse(scheduler.company_analytics_snapshot(conn, None))

    def test_db_error_rolls_back_and_returns_false(self):
        cur = SeqCursor(raise_on_execute=True)
        conn = SeqConnection(cur)
        ok = scheduler.company_analytics_snapshot(conn, 1, day='2026-06-07')
        self.assertFalse(ok)
        self.assertEqual(conn.rollbacks, 1)
        self.assertTrue(cur.closed)  # cursor still closed in finally


# ── Job registration + batch isolation ───────────────────────────────────────

class JobTests(unittest.TestCase):
    def test_rollup_job_registered_daily(self):
        job = next((j for j in scheduler.JOBS
                    if j['name'] == 'company_analytics_rollup'), None)
        self.assertIsNotNone(job, "company_analytics_rollup job not registered")
        self.assertEqual(job['interval_seconds'], 86400)
        self.assertTrue(job['enabled'])
        self.assertTrue(callable(job['fn']))


# ── _daily_usage_from_snapshot (multitenant_reports) ─────────────────────────

class DailyUsageFromSnapshotTests(unittest.TestCase):
    def _import_helper(self):
        import multitenant_reports
        return multitenant_reports._daily_usage_from_snapshot

    def test_fresh_snapshot_is_served(self):
        helper = self._import_helper()
        today = datetime.date.today()
        recent = today - datetime.timedelta(days=1)
        cur = SeqCursor(
            fetchone_seq=[{'max_date': recent}],
            fetchall_seq=[[
                {'day': recent, 'cnt': 12},
                {'day': today, 'cnt': 5},
            ]],
        )
        out = helper(cur, company_id=42, days_back=7)
        self.assertIsNotNone(out)
        self.assertEqual(out[recent.strftime('%Y-%m-%d')], 12)
        self.assertEqual(out[today.strftime('%Y-%m-%d')], 5)

    def test_no_snapshot_returns_none(self):
        helper = self._import_helper()
        cur = SeqCursor(fetchone_seq=[{'max_date': None}])
        self.assertIsNone(helper(cur, company_id=42, days_back=7))

    def test_stale_snapshot_returns_none(self):
        helper = self._import_helper()
        stale = datetime.date.today() - datetime.timedelta(days=30)
        cur = SeqCursor(fetchone_seq=[{'max_date': stale}])
        self.assertIsNone(helper(cur, company_id=42, days_back=7))

    def test_db_error_returns_none(self):
        helper = self._import_helper()
        cur = SeqCursor(raise_on_execute=True)
        self.assertIsNone(helper(cur, company_id=42, days_back=7))


if __name__ == "__main__":
    unittest.main()
