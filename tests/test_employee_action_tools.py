"""AI Tooler 2 Phase 7: employee-facing action tools.

save_course_for_later + set_course_reminder save immediately to the user memory
store. manage_my_order + request_manager_approval are confirm-gated and strictly
self-scoped (order_service enforces ownership). Services mocked — no live DB/mail.
"""
import json
import unittest
from unittest import mock

import app1.tools as tools
from ai_tool_registry import get_employee_tool_selection, tool_name, is_parallel_safe, get_tool_meta


class WishlistAndReminderTests(unittest.TestCase):
    def test_save_course_for_later_requires_login(self):
        out = json.loads(tools._execute_save_course_for_later({"product_title": "X"}, username=None))
        self.assertEqual(out.get("status"), "error")

    def test_save_course_for_later_saves_memory(self):
        with mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.add_memory") as add:
            out = json.loads(tools._execute_save_course_for_later(
                {"product_title": "Python Basis", "product_handle": "py-basis"}, username="eva"))
        self.assertEqual(out.get("status"), "memory_saved")
        self.assertEqual(out.get("category"), "wishlist")
        add.assert_called_once()
        self.assertEqual(add.call_args.kwargs.get("category"), "wishlist")

    def test_set_course_reminder_needs_date(self):
        out = json.loads(tools._execute_set_course_reminder({"product_title": "X"}, username="eva"))
        self.assertEqual(out.get("status"), "error")

    def test_set_course_reminder_saves(self):
        with mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.add_memory") as add:
            out = json.loads(tools._execute_set_course_reminder(
                {"product_title": "GDPR", "remind_on": "på mandag"}, username="eva"))
        self.assertEqual(out.get("status"), "memory_saved")
        self.assertEqual(add.call_args.kwargs.get("category"), "reminder")


class ManageMyOrderTests(unittest.TestCase):
    def _ctx(self):
        return mock.Mock(user_id=7, company_id=42, is_manager=False, is_employee=True)

    def test_requires_login(self):
        out = json.loads(tools._execute_manage_my_order({"order_id": "o1"}, username=None))
        self.assertEqual(out.get("status"), "error")

    def test_preview_without_confirm(self):
        with mock.patch("order_service.OrderContext.from_session", return_value=self._ctx()), \
                mock.patch("order_service.get_order",
                           return_value={"success": True, "order": {"product_title": "Python", "order_id": "o1"}}), \
                mock.patch("order_service.cancel_order") as cancel:
            out = json.loads(tools._execute_manage_my_order({"order_id": "o1"}, username="eva"))
        self.assertTrue(out.get("needs_confirmation"))
        self.assertEqual(out.get("action"), "manage_my_order")
        cancel.assert_not_called()

    def test_foreign_order_not_found(self):
        with mock.patch("order_service.OrderContext.from_session", return_value=self._ctx()), \
                mock.patch("order_service.get_order", return_value={"success": False, "error": "not_found"}):
            out = json.loads(tools._execute_manage_my_order({"order_id": "foreign", "confirm": True}, username="eva"))
        self.assertEqual(out.get("status"), "error")

    def test_confirm_cancels(self):
        with mock.patch("order_service.OrderContext.from_session", return_value=self._ctx()), \
                mock.patch("order_service.get_order",
                           return_value={"success": True, "order": {"product_title": "Python", "order_id": "o1"}}), \
                mock.patch("order_service.cancel_order",
                           return_value={"success": True, "refunded": True}) as cancel:
            out = json.loads(tools._execute_manage_my_order({"order_id": "o1", "confirm": True}, username="eva"))
        cancel.assert_called_once()
        self.assertEqual(out.get("status"), "success")
        self.assertTrue(out.get("refunded"))


class RequestManagerApprovalTests(unittest.TestCase):
    def _ctx(self):
        return mock.Mock(user_id=7, company_id=42, is_manager=False, is_employee=True)

    def test_preview_then_send(self):
        sess = {"company_id": 42, "user_id": 7}
        with mock.patch.object(tools, "flask_session", sess, create=True), \
                mock.patch("flask.session", sess), \
                mock.patch("order_service.OrderContext.from_session", return_value=self._ctx()), \
                mock.patch("order_service.get_order",
                           return_value={"success": True, "order": {"product_title": "Lederkursus", "order_id": "o1"}}):
            preview = json.loads(tools._execute_request_manager_approval({"order_id": "o1"}, username="eva"))
            self.assertTrue(preview.get("needs_confirmation"))
            with mock.patch("order_service._manager_recipient_emails", return_value=["m@x.dk", "n@x.dk"]), \
                    mock.patch("email_service.send_branded_email", return_value=True) as send:
                out = json.loads(tools._execute_request_manager_approval({"order_id": "o1", "confirm": True}, username="eva"))
        self.assertEqual(out.get("status"), "success")
        self.assertEqual(out.get("sent"), 2)
        self.assertEqual(send.call_count, 2)


class RegistrationTests(unittest.TestCase):
    def test_metadata_and_gating(self):
        # confirm-gated self writes are not parallel-safe
        self.assertFalse(is_parallel_safe("manage_my_order"))
        self.assertTrue(get_tool_meta("manage_my_order").confirm_required)
        self.assertTrue(get_tool_meta("request_manager_approval").confirm_required)
        # wishlist/reminder are simple auth-only reads-ish (no confirm)
        self.assertFalse(get_tool_meta("save_course_for_later").confirm_required)
        # tools stay off the menu when logged out
        sel, _ = get_employee_tool_selection(logged_in=False, company_id=None, intent="discovery",
                                             user_query="annullér min bestilling")
        self.assertNotIn("manage_my_order", [tool_name(t) for t in sel])


if __name__ == "__main__":
    unittest.main()
