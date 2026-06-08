"""Confirm-gated HR write tools: set_skill_target + create_compliance_requirement (plan #13).

Until now only 2 of the HR tools mutated (approve_order_from_chat,
assign_learning_path_to_team): the advisor could diagnose skill gaps and
compliance misses but could not ACT on them — every fix dead-ended at a separate
UI form. This initiative adds two small write tools that close the loop in chat,
reusing the EXACT guard contract proven in approve_order_from_chat:

  * company-scoped — the write only ever touches the session company's rows,
  * manager-only    — OrderContext.from_session().is_manager (a non-manager is
                      refused, never silently downgraded),
  * confirm-gated   — without confirm=true the tool returns a preview only
                      (needs_confirmation), so no row is written until the human
                      explicitly confirms,
  * audited         — a best-effort audit_log row records who changed what.

Tests run WITHOUT a live DB / OpenAI: the executors are exercised with a patched
session, a patched OrderContext (manager flag), and a fake mysql cursor that
records the SQL it was handed; the registry checks are pure-selection.
"""

import json
import unittest
from unittest import mock

import db_compat  # noqa: F401  (installs pymysql->MySQLdb shim)
import hr_tools
from ai_tool_registry import get_hr_tool_selection, tool_name, is_parallel_safe


# ── A fake DictCursor that records executed SQL and answers SELECTs from a queue.

class _FakeCursor:
    def __init__(self, fetch_results=None):
        # fetch_results: list of dicts (or None) returned by successive fetchone()
        self._fetch = list(fetch_results or [])
        self.executed = []          # [(sql, params), ...]
        self.lastrowid = 4242
        self._last_fetch = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        # Pop the next queued fetchone result when this looks like a SELECT.
        if sql.strip().upper().startswith("SELECT"):
            self._last_fetch = self._fetch.pop(0) if self._fetch else None
        return self

    def fetchone(self):
        return self._last_fetch

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
        self._cursor = cursor
        self.mysql.connection.cursor = mock.Mock(return_value=cursor)

    def _get_current_object(self):
        return self


def _run_tool(fn, args, *, company_id=42, is_manager=True, fetch_results=None):
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
    """All recorded SQL statements whose first word is `verb` (e.g. INSERT)."""
    return [(s, p) for (s, p) in cursor.executed if s.strip().upper().startswith(verb)]


# ── set_skill_target ─────────────────────────────────────────────────────────

class SetSkillTargetTests(unittest.TestCase):
    def test_no_company_returns_error(self):
        out, _, _ = _run_tool(hr_tools._execute_set_skill_target,
                              {"skill_name": "Python", "target_level": 4}, company_id=None)
        self.assertIn("error", out)

    def test_non_manager_is_refused_and_writes_nothing(self):
        out, cursor, conn = _run_tool(
            hr_tools._execute_set_skill_target,
            {"skill_name": "Python", "target_level": 4, "confirm": True},
            is_manager=False)
        self.assertEqual(out.get("error"), "not_authorized")
        self.assertEqual(_writes(cursor, "INSERT"), [])
        self.assertFalse(conn.committed)

    def test_missing_skill_name_errors(self):
        out, _, _ = _run_tool(hr_tools._execute_set_skill_target,
                              {"skill_name": "  ", "target_level": 3})
        self.assertIn("error", out)

    def test_target_level_out_of_range_rejected(self):
        for bad in (0, 6, 99, -1):
            out, cursor, _ = _run_tool(
                hr_tools._execute_set_skill_target,
                {"skill_name": "Python", "target_level": bad, "confirm": True})
            self.assertIn("error", out, f"level {bad} should be rejected")
            self.assertEqual(_writes(cursor, "INSERT"), [])

    def test_non_numeric_target_level_rejected(self):
        out, cursor, _ = _run_tool(
            hr_tools._execute_set_skill_target,
            {"skill_name": "Python", "target_level": "høj", "confirm": True})
        self.assertIn("error", out)
        self.assertEqual(_writes(cursor, "INSERT"), [])

    def test_without_confirm_is_preview_only_no_write(self):
        out, cursor, conn = _run_tool(
            hr_tools._execute_set_skill_target,
            {"skill_name": "Cybersikkerhed", "target_level": 4, "department": "IT"},
            fetch_results=[None])  # no existing row -> create preview
        self.assertTrue(out.get("needs_confirmation"))
        self.assertFalse(out.get("is_update"))
        self.assertEqual(_writes(cursor, "INSERT"), [])
        self.assertFalse(conn.committed)

    def test_confirm_creates_and_commits(self):
        out, cursor, conn = _run_tool(
            hr_tools._execute_set_skill_target,
            {"skill_name": "Cybersikkerhed", "target_level": 4, "department": "IT",
             "priority": "high", "confirm": True},
            fetch_results=[None])  # no existing row
        self.assertTrue(out.get("success"))
        self.assertFalse(out.get("was_update"))
        inserts = _writes(cursor, "INSERT")
        target_inserts = [i for i in inserts if "company_skill_targets" in i[0]]
        self.assertEqual(len(target_inserts), 1)
        params = target_inserts[0][1]
        # company_id is bound first and pinned to the session company.
        self.assertEqual(params[0], 42)
        self.assertIn("IT", params)
        self.assertIn(4, params)
        self.assertTrue(conn.committed)

    def test_confirm_update_reports_previous_level(self):
        out, cursor, conn = _run_tool(
            hr_tools._execute_set_skill_target,
            {"skill_name": "Python", "target_level": 5, "confirm": True},
            fetch_results=[{"id": 9, "target_level": 3}])  # existing row
        self.assertTrue(out.get("success"))
        self.assertTrue(out.get("was_update"))
        self.assertTrue(conn.committed)

    def test_preview_update_shows_old_to_new(self):
        out, cursor, conn = _run_tool(
            hr_tools._execute_set_skill_target,
            {"skill_name": "Python", "target_level": 5},
            fetch_results=[{"id": 9, "target_level": 2}])
        self.assertTrue(out.get("needs_confirmation"))
        self.assertTrue(out.get("is_update"))
        self.assertEqual(out.get("previous_target_level"), 2)
        self.assertEqual(_writes(cursor, "INSERT"), [])

    def test_audit_row_written_on_success(self):
        out, cursor, _ = _run_tool(
            hr_tools._execute_set_skill_target,
            {"skill_name": "Python", "target_level": 4, "confirm": True},
            fetch_results=[None])
        audit = [i for i in _writes(cursor, "INSERT") if "audit_log" in i[0]]
        self.assertEqual(len(audit), 1)


# ── create_compliance_requirement ───────────────────────────────────────────

class CreateComplianceRequirementTests(unittest.TestCase):
    def test_no_company_returns_error(self):
        out, _, _ = _run_tool(hr_tools._execute_create_compliance_requirement,
                              {"title": "GDPR"}, company_id=None)
        self.assertIn("error", out)

    def test_non_manager_is_refused_and_writes_nothing(self):
        out, cursor, conn = _run_tool(
            hr_tools._execute_create_compliance_requirement,
            {"title": "Årlig GDPR", "confirm": True}, is_manager=False)
        self.assertEqual(out.get("error"), "not_authorized")
        self.assertEqual(_writes(cursor, "INSERT"), [])
        self.assertFalse(conn.committed)

    def test_missing_title_errors(self):
        out, _, _ = _run_tool(hr_tools._execute_create_compliance_requirement,
                              {"title": "   "})
        self.assertIn("error", out)

    def test_without_confirm_is_preview_only_no_write(self):
        out, cursor, conn = _run_tool(
            hr_tools._execute_create_compliance_requirement,
            {"title": "Årlig GDPR", "recurrence_months": 12, "is_statutory": True})
        self.assertTrue(out.get("needs_confirmation"))
        self.assertTrue(out.get("is_statutory"))
        self.assertEqual(out.get("recurrence_months"), 12)
        self.assertEqual(_writes(cursor, "INSERT"), [])
        self.assertFalse(conn.committed)

    def test_confirm_creates_and_commits(self):
        out, cursor, conn = _run_tool(
            hr_tools._execute_create_compliance_requirement,
            {"title": "Årlig GDPR-genopfriskning", "category": "GDPR",
             "applies_to_department": "Salg", "recurrence_months": 12,
             "is_statutory": True, "confirm": True},
            fetch_results=[None])  # no duplicate
        self.assertTrue(out.get("success"))
        self.assertEqual(out.get("requirement_id"), 4242)
        req_inserts = [i for i in _writes(cursor, "INSERT")
                       if "compliance_requirements" in i[0]]
        self.assertEqual(len(req_inserts), 1)
        params = req_inserts[0][1]
        self.assertEqual(params[0], 42)              # company_id pinned
        self.assertIn("Salg", params)
        self.assertIn(12, params)                    # recurrence
        self.assertIn(1, params)                     # is_statutory truthy -> 1
        self.assertTrue(conn.committed)

    def test_duplicate_is_blocked_no_second_insert(self):
        out, cursor, conn = _run_tool(
            hr_tools._execute_create_compliance_requirement,
            {"title": "Årlig GDPR", "confirm": True},
            fetch_results=[{"id": 11}])  # duplicate exists
        self.assertEqual(out.get("error"), "duplicate")
        self.assertEqual(out.get("existing_id"), 11)
        self.assertEqual([i for i in _writes(cursor, "INSERT")
                          if "compliance_requirements" in i[0]], [])
        self.assertFalse(conn.committed)

    def test_negative_recurrence_clamped_to_zero(self):
        out, cursor, _ = _run_tool(
            hr_tools._execute_create_compliance_requirement,
            {"title": "Engangskursus", "recurrence_months": -3, "confirm": True},
            fetch_results=[None])
        self.assertTrue(out.get("success"))
        self.assertEqual(out.get("recurrence_months"), 0)

    def test_is_statutory_string_truthiness(self):
        out, _, _ = _run_tool(
            hr_tools._execute_create_compliance_requirement,
            {"title": "ISO 27001", "is_statutory": "ja"})  # preview
        self.assertTrue(out.get("is_statutory"))

    def test_audit_row_written_on_success(self):
        out, cursor, _ = _run_tool(
            hr_tools._execute_create_compliance_requirement,
            {"title": "GDPR", "confirm": True}, fetch_results=[None])
        audit = [i for i in _writes(cursor, "INSERT") if "audit_log" in i[0]]
        self.assertEqual(len(audit), 1)


# ── Registration + keyword gating + parallel-safety ──────────────────────────

class RegistryTests(unittest.TestCase):
    def test_both_tools_in_schema_and_router(self):
        schema_names = {(_t.get("function") or {}).get("name") for _t in hr_tools.HR_TOOLS}
        self.assertIn("set_skill_target", schema_names)
        self.assertIn("create_compliance_requirement", schema_names)
        # Router wiring: an unknown-arg path still returns JSON (router reachable).
        out = hr_tools.execute_hr_tool(
            mock.Mock(function=mock.Mock(name="x", arguments="{bad")))
        self.assertIn("error", json.loads(out))

    def test_both_tools_are_side_effecting_not_parallel_safe(self):
        self.assertFalse(is_parallel_safe("set_skill_target"))
        self.assertFalse(is_parallel_safe("create_compliance_requirement"))

    def test_set_skill_target_keyword_gated(self):
        tools, _ = get_hr_tool_selection(
            company_id=1, user_query="Sæt målet for cybersikkerhed i IT til 4")
        self.assertIn("set_skill_target", {tool_name(t) for t in tools})

    def test_create_compliance_keyword_gated(self):
        tools, _ = get_hr_tool_selection(
            company_id=1, user_query="Gør GDPR til et årligt krav for hele virksomheden")
        self.assertIn("create_compliance_requirement", {tool_name(t) for t in tools})

    def test_write_tools_excluded_without_company(self):
        tools, _ = get_hr_tool_selection(
            company_id=None, user_query="Sæt kompetencemål og opret compliance-krav")
        names = {tool_name(t) for t in tools}
        self.assertNotIn("set_skill_target", names)
        self.assertNotIn("create_compliance_requirement", names)

    def test_write_tools_not_offered_off_topic(self):
        # A pure budget question must not pull in either write tool.
        tools, _ = get_hr_tool_selection(
            company_id=1, user_query="Hvor meget budget har vi tilbage?")
        names = {tool_name(t) for t in tools}
        self.assertNotIn("set_skill_target", names)
        self.assertNotIn("create_compliance_requirement", names)


if __name__ == "__main__":
    unittest.main()
