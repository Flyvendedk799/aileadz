"""Proactive learning-deadline reminders (plan #17).

There was no scheduled job warning about approaching learning-path due dates,
even though ``employee_learning_progress.due_date`` exists and ``deadline_ics``
already reads it — so deadlines lapsed silently. This activates the loop: per
company the scheduler finds not-yet-completed progress rows whose ``due_date`` is
within N days (or already overdue) and raises:

  1. ONE aggregate, k-anon-safe (counts-only) HR/manager ``company_notifications``
     card — always; and
  2. direct per-learner reminder cards — ONLY when the company has explicitly
     opted in (``company_settings.learning_deadline_reminders_enabled``).

These tests run WITHOUT a real DB or Flask boot, using fakes modelled on
tests/test_compliance_recheck.py / tests/test_scheduler.py. They lock in:

  1. the manager card is aggregate / counts-only — never a learner name/user_id.
  2. the per-learner reminders are GATED behind the opt-in flag (off by default).
  3. recent-duplicate dedupe skips a scope already nudged in the window.
  4. urgency: any overdue -> urgent; due-soon only -> not.
  5. per-run cap bounds learner reminders.
  6. the scheduler job is registered (daily) and isolates a single company's
     failure from the rest of the batch.
"""

import unittest
from unittest import mock

import deadline_service
import scheduler


# ── Fakes (modelled on tests/test_compliance_recheck.py) ─────────────────────

class FakeCursor:
    def __init__(self, *, fetchone=None, fetchall=None, fetchone_seq=None,
                 raise_on_execute=False):
        self.closed = False
        self.executed = []
        self._fetchone = fetchone
        self._fetchall = fetchall if fetchall is not None else []
        self._fetchone_seq = list(fetchone_seq) if fetchone_seq is not None else None
        self._raise = raise_on_execute

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self._raise:
            raise RuntimeError("execute blew up")

    def fetchone(self):
        if self._fetchone_seq is not None:
            return self._fetchone_seq.pop(0) if self._fetchone_seq else None
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

    def cursor(self, *a, **k):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _row(progress_id, user_id, days_left, name="Onboarding"):
    return {
        "progress_id": progress_id,
        "user_id": user_id,
        "content_name": name,
        "due_date": "2026-06-20",
        "days_left": days_left,
        "is_overdue": days_left is not None and days_left < 0,
    }


# ── 1) aggregate manager card: counts only, k-anon-safe ──────────────────────

def _notif(returns=1):
    """Patch the unified notification writer (N-3.2)."""
    return mock.patch("notification_service.insert_company_notification",
                      return_value=returns)


class ManagerCardTests(unittest.TestCase):
    def test_card_quotes_counts_not_names(self):
        cur = FakeCursor(fetchone={"cnt": 0})
        conn = FakeConnection(cur)
        with _notif() as ins:
            n = deadline_service._insert_manager_card(cur, conn, 7, overdue=2, soon=3)
        self.assertEqual(n, 1)
        self.assertEqual(conn.commits, 1)
        self.assertEqual(ins.call_args.args[1], 7)          # company-scoped
        self.assertEqual(ins.call_args.kwargs["target_roles"], deadline_service._HR_ROLES)
        self.assertNotIn("recipient_user_id", ins.call_args.kwargs)  # role broadcast
        message = ins.call_args.kwargs["message"]
        self.assertIn("2 læringsforløb er forfaldne", message)
        self.assertIn("3 har frist", message)
        # dedupe marker lives in dedupe_key, NOT in the visible message (N-3.2)
        self.assertNotIn("learning-deadline", message)
        self.assertEqual(ins.call_args.kwargs["dedupe_key"], "learning-deadline:company-7")
        self.assertTrue(ins.call_args.kwargs["action_url"])
        self.assertNotIn("user", message.lower())

    def test_overdue_is_urgent_soon_only_is_not(self):
        cur = FakeCursor(fetchone={"cnt": 0})
        conn = FakeConnection(cur)
        with _notif() as ins:
            deadline_service._insert_manager_card(cur, conn, 7, overdue=1, soon=0)
            deadline_service._insert_manager_card(cur, conn, 7, overdue=0, soon=4)
        flags = [bool(c.kwargs["is_urgent"]) for c in ins.call_args_list]
        self.assertEqual(flags, [True, False])

    def test_recent_duplicate_skips_card(self):
        cur = FakeCursor(fetchone={"cnt": 1})
        conn = FakeConnection(cur)
        with _notif(returns=0):  # the service dedupes on dedupe_key
            n = deadline_service._insert_manager_card(cur, conn, 7, overdue=1, soon=1)
        self.assertEqual(n, 0)

    def test_no_counts_is_silent(self):
        cur = FakeCursor(fetchone={"cnt": 0})
        conn = FakeConnection(cur)
        self.assertEqual(
            deadline_service._insert_manager_card(cur, conn, 7, 0, 0), 0)
        self.assertEqual(cur.executed, [])


# ── 2) per-learner reminders are gated behind the opt-in flag ────────────────

class OptInGateTests(unittest.TestCase):
    def test_opt_in_default_false(self):
        cur = FakeCursor(fetchone={"en": 0})
        self.assertFalse(deadline_service._learner_reminders_enabled(cur, 7))

    def test_opt_in_true_when_flag_set(self):
        cur = FakeCursor(fetchone={"en": 1})
        self.assertTrue(deadline_service._learner_reminders_enabled(cur, 7))

    def test_opt_in_missing_row_is_false(self):
        cur = FakeCursor(fetchone=None)
        self.assertFalse(deadline_service._learner_reminders_enabled(cur, 7))

    def test_opt_in_error_fails_closed(self):
        cur = FakeCursor(raise_on_execute=True)
        self.assertFalse(deadline_service._learner_reminders_enabled(cur, 7))

    def test_remind_company_skips_learners_when_opted_out(self):
        rows = [_row(11, 101, -2), _row(12, 102, 3)]
        conn = FakeConnection(FakeCursor(fetchone={"cnt": 0}))
        with mock.patch.object(deadline_service, "_get_conn", return_value=conn), \
                mock.patch.object(deadline_service, "_upcoming_deadlines",
                                  return_value=rows), \
                mock.patch.object(deadline_service, "_insert_manager_card",
                                  return_value=1) as card, \
                mock.patch.object(deadline_service, "_learner_reminders_enabled",
                                  return_value=False), \
                mock.patch.object(deadline_service, "_remind_learners") as learners:
            out = deadline_service.remind_company(7)
        card.assert_called_once()
        learners.assert_not_called()  # opted out -> no per-learner nudges
        self.assertEqual(out["manager_cards"], 1)
        self.assertEqual(out["learner_reminders"], 0)

    def test_remind_company_sends_learners_when_opted_in(self):
        rows = [_row(11, 101, -2)]
        conn = FakeConnection(FakeCursor(fetchone={"cnt": 0}))
        with mock.patch.object(deadline_service, "_get_conn", return_value=conn), \
                mock.patch.object(deadline_service, "_upcoming_deadlines",
                                  return_value=rows), \
                mock.patch.object(deadline_service, "_insert_manager_card",
                                  return_value=1), \
                mock.patch.object(deadline_service, "_learner_reminders_enabled",
                                  return_value=True), \
                mock.patch.object(deadline_service, "_remind_learners",
                                  return_value=1) as learners:
            out = deadline_service.remind_company(7)
        learners.assert_called_once_with(mock.ANY, conn, 7, rows)
        self.assertEqual(out["learner_reminders"], 1)


# ── per-learner reminder writer ──────────────────────────────────────────────

class LearnerReminderTests(unittest.TestCase):
    def test_reminders_addressed_to_the_learner_themselves(self):
        cur = FakeCursor(fetchone={"cnt": 0})
        conn = FakeConnection(cur)
        rows = [_row(11, 101, -2), _row(12, 102, 1)]
        with _notif() as ins:
            n = deadline_service._remind_learners(cur, conn, 7, rows)
        self.assertEqual(n, 2)
        self.assertEqual(ins.call_count, 2)
        by_user = {c.kwargs["recipient_user_id"]: c.kwargs for c in ins.call_args_list}
        self.assertEqual(set(by_user), {101, 102})   # addressed to the learner
        self.assertTrue(by_user[101]["is_urgent"])   # overdue -> urgent
        self.assertFalse(by_user[102]["is_urgent"])  # soon -> not urgent
        self.assertEqual(by_user[101]["action_url"], "/min-laering")

    def test_per_row_dedupe_skips_recently_reminded(self):
        cur = FakeCursor(fetchone={"cnt": 1})
        conn = FakeConnection(cur)
        with _notif(returns=0):
            n = deadline_service._remind_learners(cur, conn, 7, [_row(11, 101, -1)])
        self.assertEqual(n, 0)

    def test_rows_without_user_id_are_skipped(self):
        cur = FakeCursor(fetchone={"cnt": 0})
        conn = FakeConnection(cur)
        n = deadline_service._remind_learners(cur, conn, 7, [_row(11, None, -1)])
        self.assertEqual(n, 0)

    def test_per_run_cap_bounds_reminders(self):
        cur = FakeCursor(fetchone={"cnt": 0})
        conn = FakeConnection(cur)
        many = [_row(i, 1000 + i, 2) for i in range(deadline_service._MAX_LEARNER_REMINDERS + 25)]
        with _notif():
            n = deadline_service._remind_learners(cur, conn, 7, many)
        self.assertEqual(n, deadline_service._MAX_LEARNER_REMINDERS)


# ── remind_company guards ────────────────────────────────────────────────────

class RemindCompanyGuardTests(unittest.TestCase):
    def test_no_deadlines_short_circuits(self):
        conn = FakeConnection(FakeCursor())
        with mock.patch.object(deadline_service, "_get_conn", return_value=conn), \
                mock.patch.object(deadline_service, "_upcoming_deadlines",
                                  return_value=[]), \
                mock.patch.object(deadline_service, "_insert_manager_card") as card:
            out = deadline_service.remind_company(7)
        card.assert_not_called()
        self.assertEqual(out, {"company_id": 7, "manager_cards": 0,
                               "learner_reminders": 0})

    def test_no_company_is_silent(self):
        out = deadline_service.remind_company(None)
        self.assertEqual(out["manager_cards"], 0)
        self.assertEqual(out["learner_reminders"], 0)

    def test_manager_card_failure_does_not_block_learners(self):
        rows = [_row(11, 101, -2)]
        conn = FakeConnection(FakeCursor(fetchone={"cnt": 0}))
        with mock.patch.object(deadline_service, "_get_conn", return_value=conn), \
                mock.patch.object(deadline_service, "_upcoming_deadlines",
                                  return_value=rows), \
                mock.patch.object(deadline_service, "_insert_manager_card",
                                  side_effect=RuntimeError("boom")), \
                mock.patch.object(deadline_service, "_learner_reminders_enabled",
                                  return_value=True), \
                mock.patch.object(deadline_service, "_remind_learners",
                                  return_value=1):
            out = deadline_service.remind_company(7)  # must not raise
        self.assertEqual(out["manager_cards"], 0)
        self.assertEqual(out["learner_reminders"], 1)


# ── scheduler job registration + batch isolation ─────────────────────────────

class _FakeAppContext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeApp:
    def __init__(self, connection=None):
        self.mysql = type("M", (), {"connection": connection})()

    def app_context(self):
        return _FakeAppContext()


class SchedulerJobTests(unittest.TestCase):
    def test_job_registered_daily_enabled(self):
        job = next((j for j in scheduler.JOBS
                    if j["name"] == "learning_deadline_reminders"), None)
        self.assertIsNotNone(job)
        self.assertEqual(job["interval_seconds"], 86400)
        self.assertTrue(job["enabled"])
        self.assertTrue(callable(job["fn"]))

    def test_job_aggregates_summaries_and_isolates_failures(self):
        app = FakeApp(FakeConnection())
        with mock.patch.object(scheduler, "active_company_ids",
                               return_value=[1, 2, 3]), \
                mock.patch("deadline_service.remind_company") as rc:
            rc.side_effect = [
                {"company_id": 1, "manager_cards": 1, "learner_reminders": 4},
                RuntimeError("company 2 broke"),
                {"company_id": 3, "manager_cards": 1, "learner_reminders": 0},
            ]
            out = scheduler._job_learning_deadline_reminders(app)
        self.assertEqual(out["companies"], 3)
        self.assertEqual(out["manager_cards"], 2)       # 1 + 0(failed) + 1
        self.assertEqual(out["learner_reminders"], 4)   # 4 + 0(failed) + 0
        self.assertEqual(out["errors"], 1)

    def test_job_import_failure_is_guarded(self):
        app = FakeApp(FakeConnection())
        with mock.patch.dict("sys.modules", {"deadline_service": None}):
            out = scheduler._job_learning_deadline_reminders(app)
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
