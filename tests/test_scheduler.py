"""Tests for scheduler.py.

These run WITHOUT a real DB or a real Flask app (the host's MySQL is not
available in CI/sandbox). They cover:

  * the pure ``is_due`` due-logic (no DB, no app context),
  * the job registry shape (the four expected jobs are registered),
  * that ``run_due_jobs`` / ``run_due_jobs_safe`` NEVER raise — even with a
    broken app, a missing DB, or a job whose body throws — using a fake app +
    fake MySQL connection modelled on tests/test_db_tx.py.

No SANDBOX/MySQL is required; the fakes record commit/rollback/close so we can
assert the guarded lifecycle without touching a database.
"""

import time
import unittest

import scheduler


# ── Pure due-logic (no DB) ───────────────────────────────────────────────────

class IsDueTests(unittest.TestCase):
    def test_never_run_is_due(self):
        self.assertTrue(scheduler.is_due(None, 86400))

    def test_force_is_always_due(self):
        self.assertTrue(scheduler.is_due(time.time(), 86400, force=True))

    def test_within_interval_not_due(self):
        now = 1_000_000.0
        # last run 10s ago, interval 120s -> NOT due
        self.assertFalse(scheduler.is_due(now - 10, 120, now_epoch=now))

    def test_past_interval_is_due(self):
        now = 1_000_000.0
        # last run 200s ago, interval 120s -> due
        self.assertTrue(scheduler.is_due(now - 200, 120, now_epoch=now))

    def test_exactly_at_interval_is_due(self):
        now = 1_000_000.0
        self.assertTrue(scheduler.is_due(now - 120, 120, now_epoch=now))

    def test_nonpositive_interval_always_due(self):
        now = 1_000_000.0
        self.assertTrue(scheduler.is_due(now, 0, now_epoch=now))
        self.assertTrue(scheduler.is_due(now, -5, now_epoch=now))

    def test_bad_interval_is_safe(self):
        now = 1_000_000.0
        # garbage interval -> treated as <=0 -> due (never crashes)
        self.assertTrue(scheduler.is_due(now, "not-a-number", now_epoch=now))


# ── Registry shape ───────────────────────────────────────────────────────────

class RegistryTests(unittest.TestCase):
    def test_expected_jobs_registered(self):
        names = {j['name'] for j in scheduler.JOBS}
        self.assertEqual(
            names,
            {'outbox_drain', 'daily_company_insights',
             'daily_agreement_alerts', 'compliance_recheck'},
        )

    def test_each_job_is_well_formed(self):
        for j in scheduler.JOBS:
            self.assertIn('name', j)
            self.assertIsInstance(j['interval_seconds'], int)
            self.assertTrue(callable(j['fn']))
            self.assertIn('enabled', j)

    def test_outbox_drain_is_frequent(self):
        outbox = next(j for j in scheduler.JOBS if j['name'] == 'outbox_drain')
        self.assertLessEqual(outbox['interval_seconds'], 300)

    def test_list_jobs_is_readonly_view(self):
        view = scheduler.list_jobs()
        self.assertEqual(len(view), len(scheduler.JOBS))
        # mutating the view must not leak into the registry
        view[0]['name'] = 'mutated'
        self.assertNotEqual(scheduler.JOBS[0]['name'], 'mutated')


# ── Fakes: DB connection + Flask-MySQL + app (modelled on test_db_tx.py) ─────

class FakeCursor:
    def __init__(self, rowcount=1, fetchone=None, fetchall=None, raise_on_execute=False):
        self.closed = False
        self.executed = []
        self.rowcount = rowcount
        self._fetchone = fetchone
        self._fetchall = fetchall if fetchall is not None else []
        self._raise = raise_on_execute

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self._raise:
            raise RuntimeError("execute blew up")

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor=None):
        self.commits = 0
        self.rollbacks = 0
        self._cursor = cursor or FakeCursor()

    def cursor(self, *args, **kwargs):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakeMySQL:
    def __init__(self, connection=None):
        self.connection = connection or FakeConnection()


class _FakeAppContext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeApp:
    """Minimal Flask-app stand-in: app_context() + .mysql.connection."""

    def __init__(self, connection=None, raise_context=False):
        self.mysql = FakeMySQL(connection)
        self._raise_context = raise_context

    def app_context(self):
        if self._raise_context:
            raise RuntimeError("no app context")
        return _FakeAppContext()


# ── Guarded-runner behaviour ─────────────────────────────────────────────────

class RunDueJobsGuardTests(unittest.TestCase):
    def test_none_app_returns_empty_and_does_not_raise(self):
        out = scheduler.run_due_jobs(None)
        self.assertEqual(out['ran'], [])
        self.assertEqual(out['errors'], {})

    def test_broken_app_context_never_raises(self):
        app = FakeApp(raise_context=True)
        out = scheduler.run_due_jobs(app)  # must not raise
        self.assertIsInstance(out, dict)
        # nothing could run, but the call returned a well-formed result
        self.assertIn('ran', out)
        self.assertIn('skipped', out)

    def test_run_due_jobs_safe_swallows_everything(self):
        # Even a totally bogus app object must not escape run_due_jobs_safe.
        class Exploding:
            def app_context(self):
                raise ValueError("boom")
            mysql = property(lambda self: (_ for _ in ()).throw(ValueError("boom")))

        out = scheduler.run_due_jobs_safe(Exploding())
        self.assertIsInstance(out, dict)
        self.assertIn('ran', out)

    def test_failing_job_is_isolated_and_stamped_error(self):
        # A registered job whose body throws must be caught, recorded as an
        # error, and must NOT prevent run_due_jobs from returning normally.
        # Claim succeeds (rowcount=1) and last_run is None (never run) so the
        # single job we restrict to is attempted.
        cur = FakeCursor(rowcount=1, fetchone={'ts': None})
        app = FakeApp(FakeConnection(cur))

        original = scheduler._jobs_by_name

        def boom_fn(_app):
            raise RuntimeError("job exploded")

        # Temporarily swap in a one-job registry via monkeypatch of JOBS lookup.
        saved_jobs = scheduler.JOBS
        scheduler.JOBS = [{
            'name': 'fake_job', 'interval_seconds': 86400,
            'fn': boom_fn, 'enabled': True,
        }]
        try:
            out = scheduler.run_due_jobs(app, only='fake_job', force=True)
        finally:
            scheduler.JOBS = saved_jobs

        self.assertIn('fake_job', out['errors'])
        self.assertNotIn('fake_job', out['ran'])
        # the runner returned a dict and never raised
        self.assertIsInstance(out, dict)

    def test_successful_fake_job_runs_and_records(self):
        cur = FakeCursor(rowcount=1, fetchone={'ts': None})
        app = FakeApp(FakeConnection(cur))

        def ok_fn(_app):
            return {'did': 'work'}

        saved_jobs = scheduler.JOBS
        scheduler.JOBS = [{
            'name': 'fake_ok', 'interval_seconds': 86400,
            'fn': ok_fn, 'enabled': True,
        }]
        try:
            out = scheduler.run_due_jobs(app, only='fake_ok', force=True)
        finally:
            scheduler.JOBS = saved_jobs

        self.assertIn('fake_ok', out['ran'])
        self.assertEqual(out['results']['fake_ok'], {'did': 'work'})

    def test_disabled_job_is_skipped(self):
        cur = FakeCursor(rowcount=1, fetchone={'ts': None})
        app = FakeApp(FakeConnection(cur))

        saved_jobs = scheduler.JOBS
        scheduler.JOBS = [{
            'name': 'fake_disabled', 'interval_seconds': 86400,
            'fn': lambda _a: {}, 'enabled': False,
        }]
        try:
            out = scheduler.run_due_jobs(app, force=True)
        finally:
            scheduler.JOBS = saved_jobs

        self.assertIn('fake_disabled', out['skipped'])
        self.assertNotIn('fake_disabled', out['ran'])

    def test_not_claimed_is_skipped(self):
        # rowcount=0 -> another worker already claimed this window.
        cur = FakeCursor(rowcount=0, fetchone={'ts': None})
        app = FakeApp(FakeConnection(cur))

        saved_jobs = scheduler.JOBS
        scheduler.JOBS = [{
            'name': 'fake_contended', 'interval_seconds': 86400,
            'fn': lambda _a: {'ran': True}, 'enabled': True,
        }]
        try:
            out = scheduler.run_due_jobs(app, force=True)
        finally:
            scheduler.JOBS = saved_jobs

        self.assertIn('fake_contended', out['skipped'])
        self.assertNotIn('fake_contended', out['ran'])


# ── _ensure_table / active_company_ids guards ────────────────────────────────

class HelperGuardTests(unittest.TestCase):
    def test_ensure_table_no_conn_returns_false(self):
        # No app, no app context -> _get_conn() returns None -> False, no raise.
        self.assertFalse(scheduler._ensure_table())

    def test_active_company_ids_no_conn_returns_empty(self):
        self.assertEqual(scheduler.active_company_ids(), [])


if __name__ == "__main__":
    unittest.main()
