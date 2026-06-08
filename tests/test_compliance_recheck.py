"""Proactive compliance recertification reminders (plan #16).

The scheduler's ``compliance_recheck`` slot was an explicit no-op. This activates
it: per company it re-derives compliance via the SAME
``hr_tools.derive_company_compliance`` the HR matrix/chatbot use, and raises an
AGGREGATE (k-anon-safe, counts-only) ``company_notifications`` card for every
expiring/overdue/missing requirement — with the recent-duplicate dedupe pattern
catalog_freshness uses — escalating statutory-overdue cases via the branded email
path when MAIL is configured.

These tests run WITHOUT a real DB or Flask boot, using fakes modelled on
tests/test_scheduler.py / tests/test_order_alert_emails.py. They lock in:

  1. ``derive_company_compliance`` is session-independent (takes an explicit
     conn + company_id) and returns a plain dict (k-anon: counts only, no names).
  2. compliance_service raises one deduped in-app card per at-risk requirement,
     skipping requirements with no gap and ones nudged recently.
  3. urgency flag: statutory + overdue -> urgent; otherwise not.
  4. email escalation fires ONLY for statutory-overdue, is ops-gated and
     per-requirement dedupe-gated, and goes to manager recipients.
  5. the scheduler job is registered (daily) and isolates a single company's
     failure from the rest of the batch.
"""

import unittest
from unittest import mock

import compliance_service
import scheduler


# ── Fakes (modelled on tests/test_order_alert_emails.py) ─────────────────────

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


# Helper: a derived per-requirement dict (the shape derive_company_compliance
# returns inside result["requirements"]).
def _req(rid, title, *, overdue=0, missing=0, expiring=0, compliant=0,
         statutory=False, dept=None):
    has_gap = (overdue + missing + expiring) > 0
    return {
        "id": rid, "title": title, "category": "",
        "is_statutory": statutory,
        "applies_to_department": dept or "Alle afdelinger",
        "applies_to_role": "Alle roller", "recurrence_months": 12,
        "applicable_employees": overdue + missing + expiring + compliant,
        "compliant": compliant, "expiring": expiring,
        "overdue": overdue, "missing": missing,
        "compliance_pct": 0.0, "has_gap": has_gap,
    }


# ── 1) session-independent derivation contract ───────────────────────────────

class DeriveContractTests(unittest.TestCase):
    def test_derive_is_session_independent_and_returns_dict(self):
        import hr_tools
        # 4 SELECTs in the derivation: requirements, employees, course_orders,
        # user_completed_courses. We feed a requirement set and one employee with
        # no completions -> the single requirement is "missing".
        cur = FakeCursor(fetchone_seq=None)
        cur._fetchall = []  # default; we drive per-execute below

        # Sequence fetchall results: requirements, employees, orders, completed.
        results = [
            [{"id": 1, "title": "GDPR", "category": "compliance",
              "applies_to_department": None, "applies_to_role": None,
              "required_course_handle": "", "recurrence_months": 12,
              "is_statutory": 1}],
            [{"user_id": 10, "username": "ada", "department": "IT", "role": "dev"}],
            [],   # course_orders
            [],   # user_completed_courses
        ]

        class SeqFetchCursor(FakeCursor):
            def __init__(self, seq):
                super().__init__()
                self._seq = list(seq)

            def fetchall(self):
                return self._seq.pop(0) if self._seq else []

        cur = SeqFetchCursor(results)
        conn = FakeConnection(cur)
        out = hr_tools.derive_company_compliance(conn, 7)
        self.assertIsInstance(out, dict)
        reqs = out["requirements"]
        self.assertEqual(len(reqs), 1)
        # Aggregate counts only — NO usernames / people-level rows leak out.
        self.assertEqual(reqs[0]["missing"], 1)
        flat = repr(out)
        self.assertNotIn("ada", flat)  # the employee name must not appear

    def test_derive_no_company_returns_error_dict(self):
        import hr_tools
        out = hr_tools.derive_company_compliance(FakeConnection(), None)
        self.assertIn("error", out)


# ── 2/3) in-app card raising (dedupe + urgency) ──────────────────────────────

class CardRaisingTests(unittest.TestCase):
    def _patch_conn(self, conn):
        return mock.patch.object(compliance_service, "_get_conn",
                                 return_value=conn)

    def test_raises_one_card_per_at_risk_requirement(self):
        # dedupe COUNT(*) returns 0 -> not a recent duplicate -> insert.
        cur = FakeCursor(fetchone={"cnt": 0})
        conn = FakeConnection(cur)
        gaps = [
            _req(1, "Arbejdsmiljø", overdue=2, statutory=True),
            _req(2, "Brandøvelse", missing=3),
        ]
        with self._patch_conn(conn):
            n = compliance_service._insert_cards(7, gaps)
        self.assertEqual(n, 2)
        self.assertEqual(conn.commits, 1)
        # Two INSERTs into company_notifications, both company-scoped.
        inserts = [e for e in cur.executed if "INSERT INTO company_notifications" in e[0]]
        self.assertEqual(len(inserts), 2)
        for _sql, params in inserts:
            self.assertEqual(params[0], 7)  # company_id first

    def test_recent_duplicate_is_skipped(self):
        cur = FakeCursor(fetchone={"cnt": 1})  # already nudged recently
        conn = FakeConnection(cur)
        with self._patch_conn(conn):
            n = compliance_service._insert_cards(7, [_req(1, "GDPR", overdue=1)])
        self.assertEqual(n, 0)
        inserts = [e for e in cur.executed if "INSERT INTO company_notifications" in e[0]]
        self.assertEqual(inserts, [])

    def test_statutory_overdue_is_urgent_others_are_not(self):
        cur = FakeCursor(fetchone={"cnt": 0})
        conn = FakeConnection(cur)
        gaps = [
            _req(1, "Arbejdsmiljø", overdue=1, statutory=True),  # urgent
            _req(2, "Onboarding", missing=1, statutory=False),   # not urgent
            _req(3, "ISO", overdue=1, statutory=False),          # overdue but not statutory
            _req(4, "GDPR", expiring=2, statutory=True),         # statutory but only expiring
        ]
        with self._patch_conn(conn):
            compliance_service._insert_cards(7, gaps)
        inserts = [e for e in cur.executed if "INSERT INTO company_notifications" in e[0]]
        # is_urgent is the 5th param in the VALUES tuple (company, roles, title,
        # message, is_urgent).
        urgents = [params[4] for _sql, params in inserts]
        self.assertEqual(urgents, [1, 0, 0, 0])

    def test_message_quotes_counts_not_names(self):
        cur = FakeCursor(fetchone={"cnt": 0})
        conn = FakeConnection(cur)
        with self._patch_conn(conn):
            compliance_service._insert_cards(
                7, [_req(9, "Førstehjælp", overdue=2, missing=1, expiring=4)])
        insert = next(e for e in cur.executed
                      if "INSERT INTO company_notifications" in e[0])
        message = insert[1][3]  # 4th value = message
        self.assertIn("2 udløbet", message)
        self.assertIn("1 mangler", message)
        self.assertIn("4 udløber", message)
        self.assertIn("compliance-recert:req-9", message)  # dedupe marker present

    def test_no_gaps_is_silent(self):
        cur = FakeCursor(fetchone={"cnt": 0})
        conn = FakeConnection(cur)
        with self._patch_conn(conn):
            self.assertEqual(compliance_service._insert_cards(7, []), 0)
        self.assertEqual(cur.executed, [])

    def test_db_error_rolls_back_and_returns_zero(self):
        cur = FakeCursor(raise_on_execute=True)
        conn = FakeConnection(cur)
        with self._patch_conn(conn):
            n = compliance_service._insert_cards(7, [_req(1, "X", overdue=1)])
        self.assertEqual(n, 0)  # guarded, never raises


# ── 4) email escalation (statutory-overdue only, ops + dedupe gated) ──────────

class EscalationTests(unittest.TestCase):
    def test_only_statutory_overdue_escalates(self):
        sent = []
        gaps = [
            _req(1, "Arbejdsmiljø", overdue=2, statutory=True),  # -> escalate
            _req(2, "ISO", overdue=5, statutory=False),          # overdue, not statutory
            _req(3, "GDPR", expiring=9, statutory=True),         # statutory, not overdue
        ]
        with mock.patch.object(compliance_service, "_get_conn",
                               return_value=FakeConnection(FakeCursor(fetchall=[]))), \
                mock.patch.object(compliance_service, "_manager_recipients",
                                  return_value=[{"email": "a@acme.dk", "name": "Ann"},
                                                {"email": "b@acme.dk", "name": "Bo"}]), \
                mock.patch("email_service.email_recently_sent", return_value=False), \
                mock.patch("email_service.send_branded_email",
                           side_effect=lambda *a, **k: sent.append((a, k)) or True), \
                mock.patch("email_service._resolve_branding", return_value={}):
            n = compliance_service._escalate_critical(7, gaps)
        # Exactly one critical requirement, two recipients -> two emails.
        self.assertEqual(n, 2)
        self.assertEqual(len(sent), 2)
        for args, kw in sent:
            self.assertEqual(args[2], "compliance_recert_alert")
            self.assertEqual(kw["requirement_title"], "Arbejdsmiljø")
            self.assertEqual(kw["overdue_count"], 2)
            self.assertTrue(kw["dedupe_key"].startswith("compliance_recert:7:"))

    def test_recent_email_suppresses_escalation(self):
        sent = []
        with mock.patch.object(compliance_service, "_get_conn",
                               return_value=FakeConnection(FakeCursor(fetchall=[]))), \
                mock.patch.object(compliance_service, "_manager_recipients",
                                  return_value=[{"email": "a@acme.dk", "name": "Ann"}]), \
                mock.patch("email_service.email_recently_sent", return_value=True), \
                mock.patch("email_service.send_branded_email",
                           side_effect=lambda *a, **k: sent.append(a) or True), \
                mock.patch("email_service._resolve_branding", return_value={}):
            n = compliance_service._escalate_critical(
                7, [_req(1, "Arbejdsmiljø", overdue=1, statutory=True)])
        self.assertEqual(n, 0)
        self.assertEqual(sent, [])

    def test_no_critical_requirements_sends_nothing(self):
        with mock.patch("email_service.send_branded_email") as send:
            n = compliance_service._escalate_critical(
                7, [_req(1, "X", missing=4, statutory=False)])
        self.assertEqual(n, 0)
        send.assert_not_called()

    def test_no_recipients_is_silent(self):
        with mock.patch.object(compliance_service, "_get_conn",
                               return_value=FakeConnection(FakeCursor(fetchall=[]))), \
                mock.patch.object(compliance_service, "_manager_recipients",
                                  return_value=[]), \
                mock.patch("email_service._resolve_branding", return_value={}), \
                mock.patch("email_service.send_branded_email") as send:
            n = compliance_service._escalate_critical(
                7, [_req(1, "Arbejdsmiljø", overdue=1, statutory=True)])
        self.assertEqual(n, 0)
        send.assert_not_called()


# ── recheck_company orchestration ────────────────────────────────────────────

class RecheckCompanyTests(unittest.TestCase):
    def test_runs_cards_then_escalation_and_summarises(self):
        gaps = [_req(1, "Arbejdsmiljø", overdue=2, statutory=True)]
        with mock.patch.object(compliance_service, "_gap_requirements",
                               return_value=gaps), \
                mock.patch.object(compliance_service, "_insert_cards",
                                  return_value=1) as cards, \
                mock.patch.object(compliance_service, "_escalate_critical",
                                  return_value=2) as esc:
            out = compliance_service.recheck_company(7)
        self.assertEqual(out, {"company_id": 7, "notifications": 1, "emails": 2})
        cards.assert_called_once_with(7, gaps)
        esc.assert_called_once_with(7, gaps)

    def test_no_gaps_short_circuits(self):
        with mock.patch.object(compliance_service, "_gap_requirements",
                               return_value=[]), \
                mock.patch.object(compliance_service, "_insert_cards") as cards, \
                mock.patch.object(compliance_service, "_escalate_critical") as esc:
            out = compliance_service.recheck_company(7)
        self.assertEqual(out, {"company_id": 7, "notifications": 0, "emails": 0})
        cards.assert_not_called()
        esc.assert_not_called()

    def test_no_company_is_silent(self):
        out = compliance_service.recheck_company(None)
        self.assertEqual(out["notifications"], 0)
        self.assertEqual(out["emails"], 0)

    def test_card_failure_does_not_block_escalation(self):
        gaps = [_req(1, "X", overdue=1, statutory=True)]
        with mock.patch.object(compliance_service, "_gap_requirements",
                               return_value=gaps), \
                mock.patch.object(compliance_service, "_insert_cards",
                                  side_effect=RuntimeError("boom")), \
                mock.patch.object(compliance_service, "_escalate_critical",
                                  return_value=1) as esc:
            out = compliance_service.recheck_company(7)  # must not raise
        self.assertEqual(out["notifications"], 0)
        self.assertEqual(out["emails"], 1)
        esc.assert_called_once()


# ── 5) scheduler job registration + batch isolation ──────────────────────────

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
    def test_compliance_recheck_registered_daily_enabled(self):
        job = next((j for j in scheduler.JOBS
                    if j["name"] == "compliance_recheck"), None)
        self.assertIsNotNone(job)
        self.assertEqual(job["interval_seconds"], 86400)
        self.assertTrue(job["enabled"])
        self.assertTrue(callable(job["fn"]))

    def test_job_aggregates_summaries_and_isolates_failures(self):
        app = FakeApp(FakeConnection())
        with mock.patch.object(scheduler, "active_company_ids",
                               return_value=[1, 2, 3]), \
                mock.patch("compliance_service.recheck_company") as rc:
            # company 2 explodes -> isolated; 1 and 3 contribute counts.
            rc.side_effect = [
                {"company_id": 1, "notifications": 2, "emails": 1},
                RuntimeError("company 2 broke"),
                {"company_id": 3, "notifications": 1, "emails": 0},
            ]
            out = scheduler._job_compliance_recheck(app)
        self.assertEqual(out["companies"], 3)
        self.assertEqual(out["notifications"], 3)  # 2 + 0(failed) + 1
        self.assertEqual(out["emails"], 1)
        self.assertEqual(out["errors"], 1)

    def test_job_import_failure_is_guarded(self):
        # If compliance_service can't import, the job returns an error dict, not
        # a crash (the runner stamps it as an error).
        app = FakeApp(FakeConnection())
        with mock.patch.dict("sys.modules", {"compliance_service": None}):
            out = scheduler._job_compliance_recheck(app)
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
