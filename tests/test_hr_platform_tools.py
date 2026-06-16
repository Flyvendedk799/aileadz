"""AI Tooler 2 Phase 5: safe HR platform-control tools.

Each tool is exercised with a patched session + OrderContext + fake cursor (no live
DB / OpenAI). The two mutating tools (schedule_recurring_report, recheck_compliance)
reuse the proven confirm+manager+company-scope+audit contract; the two read tools
write no rows.
"""
import json
import unittest
from unittest import mock

import db_compat  # noqa: F401
import hr_tools
from ai_tool_registry import get_hr_tool_selection, tool_name, is_parallel_safe, get_tool_meta


class _FakeCursor:
    def __init__(self, fetch_results=None):
        self._fetch = list(fetch_results or [])
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if sql.strip().upper().startswith("SELECT"):
            self._last = self._fetch.pop(0) if self._fetch else None
        return self

    def fetchone(self):
        return getattr(self, "_last", None)

    def close(self):
        pass


class _FakeConn:
    def __init__(self):
        self.committed = False
        self.rolled_back = False

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class _FakeApp:
    def __init__(self, cursor):
        self.mysql = mock.Mock()
        self._conn = _FakeConn()
        self.mysql.connection = self._conn
        self.mysql.connection.cursor = mock.Mock(return_value=cursor)

    def _get_current_object(self):
        return self


def _run(fn, args, *, company_id=42, is_manager=True, fetch_results=None):
    cursor = _FakeCursor(fetch_results=fetch_results)
    app = _FakeApp(cursor)
    sess = {"company_id": company_id, "user_id": 7} if company_id else {}
    ctx = mock.Mock(is_manager=is_manager, user_id=7)
    with mock.patch.object(hr_tools, "session", sess), \
            mock.patch.object(hr_tools, "current_app", app), \
            mock.patch("order_service.OrderContext.from_session", return_value=ctx):
        out = json.loads(fn(args))
    return out, cursor, app._conn


def _writes(cursor, verb):
    return [(s, p) for (s, p) in cursor.executed if s.strip().upper().startswith(verb)]


class ScheduleRecurringReportTests(unittest.TestCase):
    def test_no_company_errors(self):
        out, _, _ = _run(hr_tools._execute_schedule_recurring_report,
                         {"report_type": "roi", "cadence": "weekly"}, company_id=None)
        self.assertIn("error", out)

    def test_non_manager_refused_no_write(self):
        out, cur, conn = _run(hr_tools._execute_schedule_recurring_report,
                              {"report_type": "roi", "cadence": "weekly", "confirm": True},
                              is_manager=False)
        self.assertEqual(out.get("error"), "not_authorized")
        self.assertEqual(_writes(cur, "INSERT"), [])
        self.assertFalse(conn.committed)

    def test_invalid_type_rejected(self):
        out, _, _ = _run(hr_tools._execute_schedule_recurring_report,
                         {"report_type": "nonsense", "cadence": "weekly", "confirm": True})
        self.assertIn("error", out)

    def test_without_confirm_is_preview(self):
        out, cur, conn = _run(hr_tools._execute_schedule_recurring_report,
                              {"report_type": "skill_gaps", "cadence": "monthly", "department": "IT"})
        self.assertTrue(out.get("needs_confirmation"))
        self.assertEqual(out.get("action"), "schedule_recurring_report")
        self.assertEqual(_writes(cur, "INSERT"), [])
        self.assertFalse(conn.committed)

    def test_confirm_writes_and_audits(self):
        out, cur, conn = _run(hr_tools._execute_schedule_recurring_report,
                              {"report_type": "compliance", "cadence": "weekly", "confirm": True})
        self.assertTrue(out.get("success"))
        inserts = _writes(cur, "INSERT")
        self.assertTrue(any("company_report_schedules" in s for s, _ in inserts))
        self.assertTrue(any("audit_log" in s for s, _ in inserts))
        # company is bound first and pinned to the session company
        sched = [p for s, p in inserts if "company_report_schedules" in s][0]
        self.assertEqual(sched[0], 42)
        self.assertTrue(conn.committed)


class RecheckComplianceTests(unittest.TestCase):
    def test_non_manager_refused(self):
        out, _, _ = _run(hr_tools._execute_recheck_compliance, {"confirm": True}, is_manager=False)
        self.assertEqual(out.get("error"), "not_authorized")

    def test_without_confirm_is_preview(self):
        out, cur, _ = _run(hr_tools._execute_recheck_compliance, {})
        self.assertTrue(out.get("needs_confirmation"))
        self.assertEqual(out.get("action"), "recheck_compliance")

    def test_confirm_calls_service_and_audits(self):
        with mock.patch("compliance_service.recheck_company",
                        return_value={"company_id": 42, "notifications": 3, "emails": 1}) as svc:
            out, cur, conn = _run(hr_tools._execute_recheck_compliance, {"confirm": True, "note": "kvartalstjek"})
        svc.assert_called_once_with(42)
        self.assertTrue(out.get("success"))
        self.assertEqual(out.get("notifications"), 3)
        self.assertTrue(any("audit_log" in s for s, _ in _writes(cur, "INSERT")))


class GenerateFreshInsightsTests(unittest.TestCase):
    def test_recompute_no_writes(self):
        with mock.patch("insights_engine.generate_company_insights",
                        return_value={"count": 5, "insights": [1, 2, 3, 4, 5]}) as gen:
            out, cur, conn = _run(hr_tools._execute_generate_fresh_insights, {})
        gen.assert_called_once()
        self.assertTrue(out.get("success"))
        self.assertEqual(out.get("insights_count"), 5)
        self.assertEqual(_writes(cur, "INSERT"), [])
        self.assertFalse(conn.committed)

    def test_no_manager_required(self):
        # read tool: a non-manager may run it (no auth refusal)
        with mock.patch("insights_engine.generate_company_insights", return_value=[]):
            out, _, _ = _run(hr_tools._execute_generate_fresh_insights, {}, is_manager=False)
        self.assertTrue(out.get("success"))


class BulkCalendarInvitesTests(unittest.TestCase):
    def test_requires_title_and_start(self):
        out, _, _ = _run(hr_tools._execute_bulk_calendar_invites, {"title": "x"})
        self.assertIn("error", out)

    def test_builds_downloadable_ics(self):
        out, cur, conn = _run(hr_tools._execute_bulk_calendar_invites,
                              {"title": "GDPR-kursus", "start_date": "2026-09-01 09:00", "location": "Online"})
        self.assertEqual(out.get("status"), "ui_card")
        self.assertEqual(out.get("ui_type"), "download")
        self.assertTrue(out.get("filename", "").endswith(".ics"))
        self.assertIn("BEGIN:VCALENDAR", out.get("content", ""))
        self.assertEqual(_writes(cur, "INSERT"), [])


class RegistrationTests(unittest.TestCase):
    def test_tools_keyword_gated_and_metadata(self):
        # gated: appear only when the keyword matches
        sel, _ = get_hr_tool_selection(company_id=42, user_query="planlæg en ugentlig rapport om roi")
        self.assertIn("schedule_recurring_report", [tool_name(t) for t in sel])
        sel2, _ = get_hr_tool_selection(company_id=42, user_query="hvordan går det med træningen")
        self.assertNotIn("schedule_recurring_report", [tool_name(t) for t in sel2])
        # side-effect tools are not parallel-safe
        self.assertFalse(is_parallel_safe("schedule_recurring_report"))
        self.assertFalse(is_parallel_safe("recheck_compliance"))
        # confirm/manager metadata is set on the mutating tools
        m = get_tool_meta("recheck_compliance", "hr")
        self.assertTrue(m.confirm_required and m.manager_only and m.side_effect)
        # insights tool carries a progress label
        self.assertEqual(get_tool_meta("generate_fresh_insights", "hr").progress_label, "Analyserer samtaler…")

    def test_company_required_gate(self):
        sel, _ = get_hr_tool_selection(company_id=None, user_query="gentjek compliance nu")
        self.assertNotIn("recheck_compliance", [tool_name(t) for t in sel])


if __name__ == "__main__":
    unittest.main()
