"""Approval-needed + budget-overrun manager emails (plan #3).

Two of the most time-sensitive money moments an HR manager owns (an order
awaiting approval, a department crossing its annual budget) only surfaced as
in-app cards. The branded email_service templates ``order_approval_needed`` and
``budget_overrun_alert`` were fully built but had NO sender. This wires them to
fire to manager-level recipients, reusing the SAME recipient helper
digest_service uses and the existing ops-gated ``send_branded_email`` path.

These tests run WITHOUT a real DB or Flask boot, using fakes modelled on
tests/test_scheduler.py. They lock in:

  1. email_service.email_recently_sent recent-duplicate guard semantics.
  2. send_branded_email threads dedupe_key into the email_log row.
  3. order_service approval-needed emails go to manager recipients, are
     ops-gated/no-recipient-safe, and are recent-duplicate guarded per order.
  4. order_service budget-overrun emails are per-department recent-duplicate
     guarded.
  5. create_order fires approval-needed emails ONLY when the order needs
     approval; set_status fires the overrun email when a charge crosses budget.
"""

import unittest
from unittest import mock

import order_service
import email_service


# ── Fakes (modelled on tests/test_scheduler.py) ──────────────────────────────

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


class FakeLogger:
    def debug(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class FakeAppConfig(dict):
    pass


class FakeApp:
    """Minimal current_app stand-in: .mysql.connection + .config + .logger."""

    def __init__(self, connection=None, config=None):
        self.mysql = type("M", (), {"connection": connection})()
        self.config = FakeAppConfig(config or {})
        self.logger = FakeLogger()


# ── 1) email_recently_sent semantics ─────────────────────────────────────────

class EmailRecentlySentTests(unittest.TestCase):
    def test_no_dedupe_key_is_never_a_duplicate(self):
        self.assertFalse(email_service.email_recently_sent(""))
        self.assertFalse(email_service.email_recently_sent(None))

    def test_no_db_returns_false(self):
        # No current_app -> guarded -> "not a known duplicate".
        self.assertFalse(
            email_service.email_recently_sent("budget_overrun_alert:1:Sales")
        )

    def test_recent_row_is_a_duplicate(self):
        cur = FakeCursor(fetchone={"n": 1})
        app = FakeApp(FakeConnection(cur))
        with mock.patch.object(email_service, "current_app", app), \
                mock.patch.object(email_service, "_ensure_email_log_table",
                                  return_value=True):
            self.assertTrue(
                email_service.email_recently_sent("order_approval_needed:abc")
            )

    def test_no_recent_row_is_not_a_duplicate(self):
        cur = FakeCursor(fetchone={"n": 0})
        app = FakeApp(FakeConnection(cur))
        with mock.patch.object(email_service, "current_app", app), \
                mock.patch.object(email_service, "_ensure_email_log_table",
                                  return_value=True):
            self.assertFalse(
                email_service.email_recently_sent("order_approval_needed:abc")
            )


# ── 2) send_branded_email threads dedupe_key into the log ─────────────────────

class SendBrandedEmailDedupeKeyTests(unittest.TestCase):
    def test_skipped_no_backend_records_dedupe_key(self):
        recorded = {}

        def _capture(to_email, template, status, **kw):
            recorded["to"] = to_email
            recorded["template"] = template
            recorded["status"] = status
            recorded["dedupe_key"] = kw.get("dedupe_key")

        app = FakeApp(FakeConnection())
        with mock.patch.object(email_service, "current_app", app), \
                mock.patch.object(email_service, "render_branded_email",
                                  return_value="<html>ok</html>"), \
                mock.patch.object(email_service, "_mail_configured",
                                  return_value=False), \
                mock.patch.object(email_service, "_record_email_attempt",
                                  side_effect=_capture):
            ok = email_service.send_branded_email(
                "boss@acme.dk", "Subj", "order_approval_needed", {},
                company_id=1, dedupe_key="order_approval_needed:xyz",
                product_title="Kursus",
            )
        self.assertFalse(ok)  # ops-gated: no backend
        self.assertEqual(recorded["status"], "skipped_no_backend")
        self.assertEqual(recorded["dedupe_key"], "order_approval_needed:xyz")


# ── 3) approval-needed emails ────────────────────────────────────────────────

class ApprovalNeededEmailTests(unittest.TestCase):
    def test_sends_to_each_manager_recipient(self):
        sent = []
        with mock.patch("email_service.email_recently_sent",
                        return_value=False), \
                mock.patch.object(order_service, "_manager_recipient_emails",
                                  return_value=["a@acme.dk", "b@acme.dk"]), \
                mock.patch.object(order_service, "_app_base_url",
                                  return_value="https://app.acme.dk"), \
                mock.patch.object(order_service, "_send_email_safe",
                                  side_effect=lambda *a, **k: sent.append((a, k))):
            order_service._send_approval_needed_emails_safe(
                7, order_id="ord-1", product_title="GDPR-kursus",
                price=1500, department="Sales", requester="Mette",
            )
        self.assertEqual(len(sent), 2)
        # template + approvals_url + requester carried through
        first_args, first_kw = sent[0]
        self.assertEqual(first_args[2], "order_approval_needed")
        self.assertIn("/hr/approvals", first_kw["approvals_url"])
        self.assertEqual(first_kw["requester"], "Mette")
        self.assertEqual(first_kw["dedupe_key"], "order_approval_needed:ord-1")

    def test_recent_duplicate_suppresses_send(self):
        sent = []
        with mock.patch("email_service.email_recently_sent", return_value=True), \
                mock.patch.object(order_service, "_manager_recipient_emails",
                                  return_value=["a@acme.dk"]), \
                mock.patch.object(order_service, "_send_email_safe",
                                  side_effect=lambda *a, **k: sent.append(a)):
            order_service._send_approval_needed_emails_safe(
                7, order_id="ord-1", product_title="X", price=10,
                department="Sales", requester="Mette",
            )
        self.assertEqual(sent, [])  # guarded — already alerted recently

    def test_no_recipients_is_silent(self):
        sent = []
        with mock.patch("email_service.email_recently_sent", return_value=False), \
                mock.patch.object(order_service, "_manager_recipient_emails",
                                  return_value=[]), \
                mock.patch.object(order_service, "_send_email_safe",
                                  side_effect=lambda *a, **k: sent.append(a)):
            order_service._send_approval_needed_emails_safe(
                7, order_id="ord-1", product_title="X", price=10,
                department="Sales", requester="Mette",
            )
        self.assertEqual(sent, [])

    def test_no_company_is_silent(self):
        sent = []
        with mock.patch.object(order_service, "_send_email_safe",
                               side_effect=lambda *a, **k: sent.append(a)):
            order_service._send_approval_needed_emails_safe(
                None, order_id="ord-1", product_title="X", price=10,
                department="Sales", requester="Mette",
            )
        self.assertEqual(sent, [])


# ── 4) budget-overrun emails ─────────────────────────────────────────────────

class BudgetOverrunEmailTests(unittest.TestCase):
    def test_sends_per_department_with_dedupe_key(self):
        sent = []
        with mock.patch("email_service.email_recently_sent", return_value=False), \
                mock.patch.object(order_service, "_manager_recipient_emails",
                                  return_value=["a@acme.dk", "b@acme.dk"]), \
                mock.patch.object(order_service, "_send_email_safe",
                                  side_effect=lambda *a, **k: sent.append((a, k))):
            order_service._send_budget_overrun_emails_safe(
                7, department="Sales", spent=120000, annual_budget=100000,
                order_id="ord-9",
            )
        self.assertEqual(len(sent), 2)
        _, kw = sent[0]
        self.assertEqual(_[2], "budget_overrun_alert")
        self.assertEqual(kw["dedupe_key"], "budget_overrun_alert:7:Sales")
        self.assertEqual(kw["department"], "Sales")

    def test_recent_duplicate_suppresses_overrun_send(self):
        sent = []
        with mock.patch("email_service.email_recently_sent", return_value=True), \
                mock.patch.object(order_service, "_manager_recipient_emails",
                                  return_value=["a@acme.dk"]), \
                mock.patch.object(order_service, "_send_email_safe",
                                  side_effect=lambda *a, **k: sent.append(a)):
            order_service._send_budget_overrun_emails_safe(
                7, department="Sales", spent=120000, annual_budget=100000,
                order_id="ord-9",
            )
        self.assertEqual(sent, [])


# ── 5) create_order / set_status integration ─────────────────────────────────

class CreateOrderFiresApprovalEmailTests(unittest.TestCase):
    def _employee_ctx(self):
        return order_service.OrderContext(
            company_id=7, user_id=42, username="medarbejder",
            company_role="employee", department="Sales", source="web",
        )

    def _manager_ctx(self):
        return order_service.OrderContext(
            company_id=7, user_id=1, username="chef",
            company_role="hr_manager", department="Sales", source="web",
        )

    def test_employee_order_fires_approval_email(self):
        # No department budget row -> fetchone returns None for the budget
        # lookup; approval-policy lookup also returns None.
        cur = FakeCursor(fetchone=None)
        conn = FakeConnection(cur)
        calls = []
        with mock.patch.object(order_service, "_get_connection",
                               return_value=conn), \
                mock.patch.object(order_service, "_resolve_approval_policy",
                                  return_value=None), \
                mock.patch.object(order_service, "_emit_event_safe"), \
                mock.patch.object(order_service,
                                  "_send_approval_needed_emails_safe",
                                  side_effect=lambda *a, **k: calls.append((a, k))):
            res = order_service.create_order(
                self._employee_ctx(),
                product_handle="h", product_title="GDPR-kursus", price=1500,
            )
        self.assertTrue(res["success"])
        self.assertTrue(res["needs_approval"])
        self.assertEqual(len(calls), 1)
        _, kw = calls[0]
        self.assertEqual(kw["product_title"], "GDPR-kursus")
        self.assertEqual(kw["requester"], "medarbejder")

    def test_manager_order_does_not_fire_approval_email(self):
        cur = FakeCursor(fetchone=None)
        conn = FakeConnection(cur)
        calls = []
        with mock.patch.object(order_service, "_get_connection",
                               return_value=conn), \
                mock.patch.object(order_service, "_resolve_approval_policy",
                                  return_value=None), \
                mock.patch.object(order_service, "_emit_event_safe"), \
                mock.patch.object(order_service, "_send_email_safe"), \
                mock.patch.object(order_service,
                                  "_send_approval_needed_emails_safe",
                                  side_effect=lambda *a, **k: calls.append((a, k))):
            res = order_service.create_order(
                self._manager_ctx(),
                product_handle="h", product_title="X", price=1500,
            )
        self.assertTrue(res["success"])
        self.assertFalse(res["needs_approval"])
        self.assertEqual(calls, [])


class SetStatusFiresOverrunEmailTests(unittest.TestCase):
    def test_charge_crossing_budget_fires_overrun_email(self):
        # Order leaving pending_approval -> approved triggers _maybe_charge.
        # First fetchone: the order row. Second fetchone (inside _maybe_charge):
        # the department_budgets row that, once charged, crosses the budget.
        order_row = {
            "order_id": "ord-9", "company_id": 7, "user_id": 1,
            "username": "chef", "department": "Sales", "price": 60000,
            "status": "pending_approval", "budget_charged": 0,
            "created_at": None, "product_title": "Lederkursus",
            "user_email": None,
        }
        budget_row = {"id": 3, "annual_budget": 100000, "spent": 80000}
        cur = FakeCursor(fetchone_seq=[order_row, budget_row])
        conn = FakeConnection(cur)
        overrun_calls = []
        with mock.patch.object(order_service, "_get_connection",
                               return_value=conn), \
                mock.patch.object(order_service, "_emit_event_safe"), \
                mock.patch.object(order_service, "_notify_company_admins_safe"), \
                mock.patch.object(order_service, "_send_email_safe"), \
                mock.patch.object(order_service,
                                  "_send_budget_overrun_emails_safe",
                                  side_effect=lambda *a, **k: overrun_calls.append((a, k))):
            res = order_service.set_status(
                self_ctx := order_service.OrderContext(
                    company_id=7, user_id=1, username="chef",
                    company_role="hr_manager", source="web"),
                "ord-9", "approved",
            )
        self.assertTrue(res["success"])
        self.assertTrue(res["charged"])
        self.assertEqual(len(overrun_calls), 1)
        _, kw = overrun_calls[0]
        self.assertEqual(kw["department"], "Sales")
        self.assertEqual(kw["spent"], 140000)        # 80000 + 60000
        self.assertEqual(kw["annual_budget"], 100000)

    def test_charge_within_budget_does_not_fire_overrun_email(self):
        order_row = {
            "order_id": "ord-10", "company_id": 7, "user_id": 1,
            "username": "chef", "department": "Sales", "price": 5000,
            "status": "pending_approval", "budget_charged": 0,
            "created_at": None, "product_title": "Kursus", "user_email": None,
        }
        budget_row = {"id": 3, "annual_budget": 100000, "spent": 10000}
        cur = FakeCursor(fetchone_seq=[order_row, budget_row])
        conn = FakeConnection(cur)
        overrun_calls = []
        with mock.patch.object(order_service, "_get_connection",
                               return_value=conn), \
                mock.patch.object(order_service, "_emit_event_safe"), \
                mock.patch.object(order_service, "_notify_company_admins_safe"), \
                mock.patch.object(order_service, "_send_email_safe"), \
                mock.patch.object(order_service,
                                  "_send_budget_overrun_emails_safe",
                                  side_effect=lambda *a, **k: overrun_calls.append(a)):
            res = order_service.set_status(
                order_service.OrderContext(
                    company_id=7, user_id=1, username="chef",
                    company_role="hr_manager", source="web"),
                "ord-10", "approved",
            )
        self.assertTrue(res["success"])
        self.assertTrue(res["charged"])
        self.assertEqual(overrun_calls, [])


if __name__ == "__main__":
    unittest.main()
