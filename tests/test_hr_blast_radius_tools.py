"""AI Tooler 2 Phase 6: high blast-radius HR write tools.

send_company_email (fan-out), send_deadline_reminders, create_order_for_employee
(charges budget). All confirm+manager+company-scoped+audited; services mocked so no
real mail/DB/budget side effects occur.
"""
import json
import unittest
from unittest import mock

import db_compat  # noqa: F401
import hr_tools
from ai_tool_registry import get_hr_tool_selection, tool_name, is_parallel_safe, get_tool_meta


class _FakeCursor:
    def __init__(self, fetchone_queue=None, fetchall_result=None):
        self._one = list(fetchone_queue or [])
        self._all = fetchall_result if fetchall_result is not None else []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return self

    def fetchone(self):
        return self._one.pop(0) if self._one else None

    def fetchall(self):
        return self._all

    def close(self):
        pass


class _FakeConn:
    def __init__(self):
        self.committed = False

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


class _FakeApp:
    def __init__(self, cursor):
        self.mysql = mock.Mock()
        self._conn = _FakeConn()
        self.mysql.connection = self._conn
        self.mysql.connection.cursor = mock.Mock(return_value=cursor)

    def _get_current_object(self):
        return self


def _run(fn, args, *, company_id=42, is_manager=True, fetchone_queue=None, fetchall_result=None):
    cursor = _FakeCursor(fetchone_queue=fetchone_queue, fetchall_result=fetchall_result)
    app = _FakeApp(cursor)
    sess = {"company_id": company_id, "user_id": 7} if company_id else {}
    ctx = mock.Mock(is_manager=is_manager, user_id=7, company_id=company_id,
                    is_employee=False, department="")
    with mock.patch.object(hr_tools, "session", sess), \
            mock.patch.object(hr_tools, "current_app", app), \
            mock.patch("order_service.OrderContext.from_session", return_value=ctx):
        out = json.loads(fn(args))
    return out, cursor, app._conn


def _writes(cursor, verb):
    return [(s, p) for (s, p) in cursor.executed if s.strip().upper().startswith(verb)]


RECIPIENTS = [
    {"user_id": 1, "full_name": "A", "email": "a@x.dk", "role": "employee"},
    {"user_id": 2, "full_name": "B", "email": "b@x.dk", "role": "employee"},
    {"user_id": 3, "full_name": "C", "email": "c@x.dk", "role": "manager"},
]


class SendCompanyEmailTests(unittest.TestCase):
    def test_non_manager_refused(self):
        out, _, _ = _run(hr_tools._execute_send_company_email,
                         {"subject": "Hej", "message": "Body", "confirm": True},
                         is_manager=False, fetchall_result=RECIPIENTS)
        self.assertEqual(out.get("error"), "not_authorized")

    def test_preview_echoes_recipient_count(self):
        out, cur, _ = _run(hr_tools._execute_send_company_email,
                           {"subject": "Kursus", "message": "Kom til kursus"},
                           fetchall_result=RECIPIENTS)
        self.assertTrue(out.get("needs_confirmation"))
        self.assertEqual(out.get("recipient_count"), 3)
        self.assertIn("3 modtager", out.get("message_da", ""))
        # no audit write on preview
        self.assertEqual(_writes(cur, "INSERT"), [])

    def test_no_recipients_errors(self):
        out, _, _ = _run(hr_tools._execute_send_company_email,
                         {"subject": "x", "message": "y", "confirm": True},
                         fetchall_result=[])
        self.assertIn("error", out)

    def test_confirm_sends_and_audits(self):
        with mock.patch("email_service.send_branded_email", return_value=True) as send, \
                mock.patch("branding_service.get_branding", return_value={}):
            out, cur, conn = _run(hr_tools._execute_send_company_email,
                                  {"subject": "Kursus", "message": "Body", "confirm": True},
                                  fetchall_result=RECIPIENTS)
        self.assertTrue(out.get("success"))
        self.assertEqual(out.get("sent"), 3)
        self.assertEqual(send.call_count, 3)
        self.assertTrue(any("audit_log" in s for s, _ in _writes(cur, "INSERT")))
        self.assertTrue(conn.committed)

    def test_partial_failure_flagged(self):
        with mock.patch("email_service.send_branded_email", side_effect=[True, False, True]), \
                mock.patch("branding_service.get_branding", return_value={}):
            out, _, _ = _run(hr_tools._execute_send_company_email,
                             {"subject": "K", "message": "B", "confirm": True},
                             fetchall_result=RECIPIENTS)
        self.assertEqual(out.get("sent"), 2)
        self.assertTrue(out.get("partial_failure"))


class SendDeadlineRemindersTests(unittest.TestCase):
    def test_preview_then_confirm(self):
        out, _, _ = _run(hr_tools._execute_send_deadline_reminders, {})
        self.assertTrue(out.get("needs_confirmation"))
        with mock.patch("deadline_service.remind_company",
                        return_value={"company_id": 42, "manager_cards": 2, "learner_reminders": 5}) as svc:
            out2, cur, conn = _run(hr_tools._execute_send_deadline_reminders, {"confirm": True})
        svc.assert_called_once_with(42)
        self.assertTrue(out2.get("success"))
        self.assertEqual(out2.get("learner_reminders"), 5)
        self.assertTrue(any("audit_log" in s for s, _ in _writes(cur, "INSERT")))


class CreateOrderForEmployeeTests(unittest.TestCase):
    EMP = {"user_id": 11, "full_name": "Eva", "email": "eva@x.dk"}

    def test_cross_tenant_employee_rejected(self):
        out, _, _ = _run(hr_tools._execute_create_order_for_employee,
                         {"employee_id": 999, "product_handle": "h", "product_title": "T", "price": 5000, "confirm": True},
                         fetchone_queue=[None])  # not found in company
        self.assertIn("error", out)
        self.assertEqual(out.get("rejected_user_id"), 999)

    def test_missing_price_rejected(self):
        out, _, _ = _run(hr_tools._execute_create_order_for_employee,
                         {"employee_id": 11, "product_handle": "h", "product_title": "T"})
        self.assertIn("error", out)

    def test_preview_shows_price_and_employee(self):
        out, cur, conn = _run(hr_tools._execute_create_order_for_employee,
                              {"employee_id": 11, "product_handle": "h", "product_title": "Lederkursus", "price": 8000},
                              fetchone_queue=[self.EMP])
        self.assertTrue(out.get("needs_confirmation"))
        self.assertEqual(out.get("action"), "create_order_for_employee")
        self.assertEqual(out.get("price"), 8000)
        self.assertIn("Eva", out.get("message_da", ""))
        self.assertEqual(_writes(cur, "INSERT"), [])

    def test_confirm_creates_via_order_service_and_audits(self):
        with mock.patch("order_service.create_order",
                        return_value={"success": True, "order_id": "ord-1", "status": "pending"}) as create:
            out, cur, conn = _run(hr_tools._execute_create_order_for_employee,
                                  {"employee_id": 11, "product_handle": "h", "product_title": "Lederkursus",
                                   "price": 8000, "confirm": True},
                                  fetchone_queue=[self.EMP])
        create.assert_called_once()
        # employee identity threaded into the order
        _, kwargs = create.call_args
        self.assertEqual(kwargs.get("user_email"), "eva@x.dk")
        self.assertEqual(kwargs.get("user_name"), "Eva")
        self.assertTrue(out.get("success"))
        self.assertEqual(out.get("order_id"), "ord-1")
        self.assertTrue(any("audit_log" in s for s, _ in _writes(cur, "INSERT")))

    def test_non_manager_refused(self):
        out, _, _ = _run(hr_tools._execute_create_order_for_employee,
                         {"employee_id": 11, "product_handle": "h", "product_title": "T", "price": 1, "confirm": True},
                         is_manager=False)
        self.assertEqual(out.get("error"), "not_authorized")


class RegistrationTests(unittest.TestCase):
    def test_gated_and_metadata(self):
        sel, _ = get_hr_tool_selection(company_id=42, user_query="send mail til medarbejderne om det nye kursus")
        self.assertIn("send_company_email", [tool_name(t) for t in sel])
        sel2, _ = get_hr_tool_selection(company_id=42, user_query="bestil kursus til medarbejder 11")
        self.assertIn("create_order_for_employee", [tool_name(t) for t in sel2])
        for name in ("send_company_email", "send_deadline_reminders", "create_order_for_employee"):
            m = get_tool_meta(name, "hr")
            self.assertTrue(m.confirm_required and m.manager_only and m.side_effect, name)
            self.assertFalse(is_parallel_safe(name), name)


if __name__ == "__main__":
    unittest.main()
