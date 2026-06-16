"""Unit tests for the shared confirm/auth/audit helpers (AI Tooler 2, Phase 2).

These primitives back every new platform-control tool, so they are exercised in
isolation — no DB, no OpenAI. The HR write tools delegate their guard + audit to
these helpers (see tests/test_hr_write_tools.py for the end-to-end behavior).
"""
import unittest
from unittest import mock

import tool_confirm


class _FakeCursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))


class _Ctx:
    def __init__(self, is_manager):
        self.is_manager = is_manager
        self.user_id = 7


class NeedsConfirmationPayloadTests(unittest.TestCase):
    def test_minimal_payload(self):
        p = tool_confirm.needs_confirmation_payload(action="do_x", summary_da="Bekræft?")
        self.assertTrue(p["needs_confirmation"])
        self.assertEqual(p["action"], "do_x")
        self.assertEqual(p["message_da"], "Bekræft?")
        self.assertNotIn("details", p)

    def test_details_and_extra_merged(self):
        p = tool_confirm.needs_confirmation_payload(
            action="send_company_email",
            summary_da="Send til 12?",
            details={"subject": "Hej"},
            recipient_count=12,
        )
        self.assertEqual(p["details"], {"subject": "Hej"})
        self.assertEqual(p["recipient_count"], 12)


class ManagerGuardTests(unittest.TestCase):
    def test_manager_passes(self):
        with mock.patch("order_service.OrderContext") as OC:
            OC.from_session.return_value = _Ctx(is_manager=True)
            ctx, err = tool_confirm.manager_guard(source="hr_chat")
        self.assertIsNone(err)
        self.assertTrue(ctx.is_manager)

    def test_non_manager_refused_with_custom_message(self):
        with mock.patch("order_service.OrderContext") as OC:
            OC.from_session.return_value = _Ctx(is_manager=False)
            ctx, err = tool_confirm.manager_guard(source="hr_chat", message="Kun ledere.")
        self.assertIsNotNone(err)
        self.assertEqual(err["error"], "not_authorized")
        self.assertEqual(err["message"], "Kun ledere.")
        # ctx is still returned so the attempt can be logged
        self.assertIsNotNone(ctx)

    def test_import_failure_is_safe(self):
        with mock.patch("order_service.OrderContext") as OC:
            OC.from_session.side_effect = RuntimeError("no session")
            ctx, err = tool_confirm.manager_guard()
        self.assertIsNone(ctx)
        self.assertIn("error", err)


class AuditChatMutationTests(unittest.TestCase):
    def test_writes_audit_row(self):
        cur = _FakeCursor()
        tool_confirm.audit_chat_mutation(
            cur, company_id=1, user_id=7, action="schedule_report",
            resource_id="job-9", description="weekly", resource_type="hr_chat",
        )
        self.assertEqual(len(cur.executed), 1)
        sql, params = cur.executed[0]
        self.assertIn("INSERT INTO audit_log", sql)
        # action used for both action + action_type; resource_id coerced to str
        self.assertEqual(params, (1, 7, "schedule_report", "schedule_report", "hr_chat", "job-9", "weekly"))

    def test_never_raises(self):
        class _Boom:
            def execute(self, *a, **k):
                raise RuntimeError("db down")
        # Must swallow the error so the mutation it records is never broken.
        tool_confirm.audit_chat_mutation(
            _Boom(), company_id=1, user_id=2, action="x", resource_id=3,
        )


if __name__ == "__main__":
    unittest.main()
