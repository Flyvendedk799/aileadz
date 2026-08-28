"""AI empowerment pass — new tool calls across all three chat surfaces.

What this guards:

  EMPLOYEE
    * ``get_my_agenda`` — one cross-silo read of everything waiting on the
      learner (deadlines, pending approvals, expiring certifications, goal
      dates), sorted worst-first, degrading per source.
    * ``get_my_compliance`` — the learner's OWN mandatory-training status,
      derived by the same primitives HR's compliance matrix uses.
    * ``open_in_app`` reaching the three employee surfaces it previously could
      not (learning home, goals, timeline).

  HR
    * ``hr_open_in_app`` — read-only navigation to any of the 22 HR pages, with
      the URL resolved server-side. Always on the menu (like the employee
      ``open_in_app``), so an answer can end in a destination, not a description.
    * The embedded panel's ``page`` finally reaching the selector as an additive
      hint instead of being dropped at the route.

  VENDOR
    * ``vendor_catalog_health`` — listing-quality audit of the vendor's OWN
      courses only, from public catalog data.

  DRIFT
    * every new tool is reachable through the selector (the three-step rule in
      docs/ai-framework.md §4), every new SSE event is in KNOWN_EVENT_TYPES and
      has a chat.js branch, and every navigation enum matches its shared list.

Fully offline: no MySQL, no OpenAI, no network.
"""
import datetime
import json
import os
import re
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO_ROOT)

_SAFE_ENV = {
    "SANDBOX": "1",
    "AI_WARMUP_ON_IMPORT": "0",
    "SCHEDULER_OPPORTUNISTIC": "0",
    "MYSQL_HOST": "127.0.0.1",
    "MYSQL_PORT": "3306",
    "MYSQL_USER": "none",
    "MYSQL_PASSWORD": "none",
    "MYSQL_DB": "none",
    "OPENAI_API_KEY": "sk-test",
}
for _k, _v in _SAFE_ENV.items():
    os.environ.setdefault(_k, _v)

import app1.tools as tools  # noqa: E402
from app1 import sse_events  # noqa: E402
import hr_tools  # noqa: E402
import vendor_tools  # noqa: E402
from ai_tool_registry import (  # noqa: E402
    get_employee_tool_selection,
    get_hr_tool_selection,
    get_tool_meta,
    tool_display_metadata,
    tool_name,
)

CHAT_JS = os.path.join(_REPO_ROOT, "static", "futurematch", "assets", "chat.js")
AI_PANEL = os.path.join(_REPO_ROOT, "templates", "fm", "_ai_panel.html")


def _schema(name, pool):
    for tool in pool:
        fn = tool.get("function") or tool
        if fn.get("name") == name:
            return fn
    return None


# ── Employee: get_my_agenda ───────────────────────────────────────────────

class GetMyAgendaTests(unittest.TestCase):
    """The agenda executor runs OUTSIDE a request context here, so the orders
    block is skipped by design and the certification/goal sources are exercised
    on their own — which is exactly the per-source degradation contract."""

    def _profile(self, certs=(), goals=()):
        return {"certifications": list(certs), "learning_goals": list(goals)}

    def _run(self, profile, **args):
        with mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.get_full_profile", return_value=profile):
            return json.loads(tools._execute_get_my_agenda(dict(args), username="eva"))

    def test_requires_login(self):
        out = json.loads(tools._execute_get_my_agenda({}, username=None))
        self.assertEqual(out.get("status"), "error")

    def test_empty_agenda_is_an_honest_empty_state(self):
        out = self._run(self._profile())
        self.assertEqual(out["status"], "agenda")
        self.assertEqual(out["items"], [])
        self.assertEqual(out["urgent_count"], 0)
        self.assertIn("ikke noget der haster", out["message"])

    def test_expiring_certification_becomes_an_item(self):
        soon = (datetime.date.today() + datetime.timedelta(days=10)).isoformat()
        out = self._run(self._profile(certs=[{"name": "PRINCE2", "expiry_date": soon}]))
        self.assertEqual(len(out["items"]), 1)
        item = out["items"][0]
        self.assertEqual(item["kind"], "certification")
        self.assertEqual(item["title"], "PRINCE2")
        self.assertFalse(item["overdue"])
        self.assertEqual(item["days_left"], 10)
        self.assertEqual(out["urgent_count"], 1)

    def test_lapsed_certification_is_overdue(self):
        past = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
        out = self._run(self._profile(certs=[{"name": "Førstehjælp", "expiry_date": past}]))
        self.assertTrue(out["items"][0]["overdue"])
        self.assertLess(out["items"][0]["days_left"], 0)

    def test_certification_beyond_horizon_is_excluded(self):
        far = (datetime.date.today() + datetime.timedelta(days=200)).isoformat()
        out = self._run(self._profile(certs=[{"name": "ITIL", "expiry_date": far}]), horizon_days=30)
        self.assertEqual(out["items"], [])
        self.assertEqual(out["horizon_days"], 30)

    def test_undated_and_completed_goals_are_ignored(self):
        soon = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
        out = self._run(self._profile(goals=[
            {"title": "Uden dato", "status": "aktiv", "target_date": ""},
            {"title": "Færdig", "status": "fuldfoert", "target_date": soon},
        ]))
        self.assertEqual(out["items"], [])

    def test_worst_first_ordering(self):
        today = datetime.date.today()
        out = self._run(self._profile(
            certs=[{"name": "Snart", "expiry_date": (today + datetime.timedelta(days=20)).isoformat()},
                   {"name": "Udløbet", "expiry_date": (today - datetime.timedelta(days=3)).isoformat()}],
            goals=[{"title": "Mål", "status": "aktiv",
                    "target_date": (today + datetime.timedelta(days=2)).isoformat()}],
        ))
        self.assertEqual([i["title"] for i in out["items"]], ["Udløbet", "Mål", "Snart"])

    def test_horizon_is_clamped(self):
        out = self._run(self._profile(), horizon_days=9999)
        self.assertEqual(out["horizon_days"], 365)
        out = self._run(self._profile(), horizon_days=1)
        self.assertEqual(out["horizon_days"], 7)

    def test_profile_failure_still_returns_an_agenda(self):
        with mock.patch("app1.user_profile_db.ensure_tables", side_effect=RuntimeError("no db")):
            out = json.loads(tools._execute_get_my_agenda({}, username="eva"))
        self.assertEqual(out["status"], "agenda")
        self.assertEqual(out["items"], [])


class AgendaOrdersTests(unittest.TestCase):
    """The orders half of the agenda: deadlines + pending approvals, read with a
    LEFT JOIN onto order_approvals that must degrade to a plain course_orders
    read on tenants where that table is absent."""

    class _Cursor:
        def __init__(self, rows, fail_join=False):
            self.rows, self.fail_join = rows, fail_join
            self.statements = []

        def execute(self, sql, params=None):
            self.statements.append(sql)
            if self.fail_join and "order_approvals" in sql:
                raise RuntimeError("Table 'order_approvals' doesn't exist")

        def fetchall(self):
            return self.rows

        def close(self):
            pass

    def _run(self, rows, fail_join=False, profile=None):
        cursor = self._Cursor(rows, fail_join=fail_join)
        app = mock.Mock()
        app.mysql.connection.cursor.return_value = cursor
        with mock.patch("flask.has_request_context", return_value=True), \
                mock.patch("flask.session", {"user_id": 7}), \
                mock.patch("flask.current_app", app), \
                mock.patch("db_compat.refresh_flask_mysql_connection"), \
                mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.get_full_profile",
                           return_value=profile or {"certifications": [], "learning_goals": []}):
            out = json.loads(tools._execute_get_my_agenda({}, username="eva"))
        return out, cursor

    def _order(self, **over):
        row = {
            "order_id": "o1", "product_handle": "python", "product_title": "Python Basis",
            "status": "approved", "completion_status": "in_progress",
            "completion_deadline": datetime.date.today() + datetime.timedelta(days=6),
            "approval_status": None,
        }
        row.update(over)
        return row

    def test_upcoming_deadline_becomes_an_urgent_item(self):
        out, _cur = self._run([self._order()])
        self.assertEqual(len(out["items"]), 1)
        item = out["items"][0]
        self.assertEqual(item["kind"], "deadline")
        self.assertEqual(item["order_id"], "o1")
        self.assertEqual(item["days_left"], 6)
        self.assertEqual(out["urgent_count"], 1)

    def test_overdue_deadline_sorts_first_and_is_flagged(self):
        out, _cur = self._run([
            self._order(order_id="soon"),
            self._order(order_id="late", product_title="GDPR",
                        completion_deadline=datetime.date.today() - datetime.timedelta(days=2)),
        ])
        self.assertEqual(out["items"][0]["order_id"], "late")
        self.assertTrue(out["items"][0]["overdue"])

    def test_completed_and_cancelled_orders_are_skipped(self):
        out, _cur = self._run([
            self._order(order_id="done", completion_status="completed"),
            self._order(order_id="gone", status="cancelled"),
        ])
        self.assertEqual(out["items"], [])

    def test_pending_approval_becomes_its_own_item(self):
        out, _cur = self._run([self._order(completion_deadline=None, status="pending",
                                           approval_status="pending")])
        kinds = [i["kind"] for i in out["items"]]
        self.assertEqual(kinds, ["approval"])
        self.assertIsNone(out["items"][0]["days_left"])

    def test_a_row_can_carry_both_a_deadline_and_an_approval(self):
        out, _cur = self._run([self._order(approval_status="pending")])
        self.assertEqual({i["kind"] for i in out["items"]}, {"deadline", "approval"})

    def test_missing_order_approvals_table_falls_back_to_orders_alone(self):
        out, cursor = self._run([self._order(approval_status=None)], fail_join=True)
        self.assertEqual(len(cursor.statements), 2)
        self.assertIn("order_approvals", cursor.statements[0])
        self.assertNotIn("order_approvals", cursor.statements[1])
        # The deadline survives the fallback — that is the whole point.
        self.assertEqual(out["items"][0]["kind"], "deadline")

    def test_query_is_scoped_to_the_session_user(self):
        _out, cursor = self._run([self._order()])
        self.assertIn("co.username = %s", cursor.statements[0])
        self.assertIn("co.user_id = %s", cursor.statements[0])

    def test_orders_and_profile_sources_merge_into_one_list(self):
        soon = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        out, _cur = self._run(
            [self._order()],
            profile={"certifications": [{"name": "ITIL", "expiry_date": soon}],
                     "learning_goals": []})
        self.assertEqual([i["kind"] for i in out["items"]], ["certification", "deadline"])


# ── Employee: get_my_compliance ───────────────────────────────────────────

class GetMyComplianceTests(unittest.TestCase):
    def test_requires_login(self):
        out = json.loads(tools._execute_get_my_compliance({}, username=None))
        self.assertEqual(out.get("status"), "error")

    def test_outside_request_context_is_a_clean_error(self):
        out = json.loads(tools._execute_get_my_compliance({}, username="eva"))
        self.assertEqual(out.get("status"), "error")

    def test_delegates_to_the_shared_hr_derivation(self):
        """The employee answer MUST come from hr_tools.derive_employee_compliance,
        not a second local implementation — otherwise the learner and their
        manager can be told different things about the same person."""
        payload = {"has_requirements": True, "applicable": 2, "action_needed": 1,
                   "is_compliant": False, "requirements": [], "message": "x"}
        flask_session = {"company_id": 42, "user_id": 7, "company_department": "Salg",
                         "company_role": "employee"}
        with mock.patch("flask.has_request_context", return_value=True), \
                mock.patch("flask.session", flask_session), \
                mock.patch("flask.current_app", mock.Mock()), \
                mock.patch("db_compat.refresh_flask_mysql_connection"), \
                mock.patch("hr_tools.derive_employee_compliance", return_value=dict(payload)) as derive:
            out = json.loads(tools._execute_get_my_compliance({}, username="eva"))
        self.assertEqual(out["status"], "compliance")
        self.assertEqual(out["action_needed"], 1)
        self.assertEqual(derive.call_args.kwargs["username"], "eva")
        self.assertEqual(derive.call_args.kwargs["user_id"], 7)
        self.assertEqual(derive.call_args.args[1], 42)

    def test_without_a_company_it_says_so_instead_of_erroring(self):
        with mock.patch("flask.has_request_context", return_value=True), \
                mock.patch("flask.session", {}):
            out = json.loads(tools._execute_get_my_compliance({}, username="eva"))
        self.assertEqual(out["status"], "compliance")
        self.assertFalse(out["has_requirements"])


# ── Shared compliance primitives (pure) ───────────────────────────────────

class CompliancePrimitiveTests(unittest.TestCase):
    REQ_RECURRING = {"title": "Arbejdsmiljø", "category": "hse",
                     "required_course_handle": "arbejdsmiljo", "recurrence_months": 12}
    REQ_ONE_TIME = {"title": "Intro", "category": "", "required_course_handle": "intro",
                    "recurrence_months": 0}

    def _entry(self, handle, days_ago=None, title=""):
        dt = None if days_ago is None else datetime.datetime.now() - datetime.timedelta(days=days_ago)
        return (handle, title, dt)

    def test_missing_when_nothing_matches(self):
        state, _e, _d = hr_tools.compliance_state_for_entries([self._entry("andet", 10)], self.REQ_RECURRING)
        self.assertEqual(state, "missing")

    def test_compliant_inside_the_recurrence_window(self):
        state, expiry, days = hr_tools.compliance_state_for_entries(
            [self._entry("arbejdsmiljo", 30)], self.REQ_RECURRING)
        self.assertEqual(state, "compliant")
        self.assertIsNotNone(expiry)
        self.assertGreater(days, hr_tools.COMPLIANCE_EXPIRING_DAYS)

    def test_expiring_inside_the_renewal_window(self):
        state, _e, days = hr_tools.compliance_state_for_entries(
            [self._entry("arbejdsmiljo", 340)], self.REQ_RECURRING)
        self.assertEqual(state, "expiring")
        self.assertLessEqual(days, hr_tools.COMPLIANCE_EXPIRING_DAYS)

    def test_overdue_past_the_recurrence_window(self):
        state, _e, days = hr_tools.compliance_state_for_entries(
            [self._entry("arbejdsmiljo", 500)], self.REQ_RECURRING)
        self.assertEqual(state, "overdue")
        self.assertLess(days, 0)

    def test_one_time_requirement_never_expires(self):
        state, _e, _d = hr_tools.compliance_state_for_entries(
            [self._entry("intro", 4000)], self.REQ_ONE_TIME)
        self.assertEqual(state, "compliant")

    def test_undatable_completion_is_not_manufactured_into_an_overdue(self):
        state, _e, _d = hr_tools.compliance_state_for_entries(
            [self._entry("arbejdsmiljo", None)], self.REQ_RECURRING)
        self.assertEqual(state, "compliant")

    def test_title_match_when_no_handle_is_required(self):
        req = {"title": "gdpr", "category": "", "required_course_handle": "", "recurrence_months": 0}
        self.assertTrue(hr_tools.compliance_completion_matches(
            ("", "gdpr for begyndere", None), "", "gdpr", ""))
        state, _e, _d = hr_tools.compliance_state_for_entries(
            [("h", "gdpr for begyndere", None)], req)
        self.assertEqual(state, "compliant")

    def test_applicability_by_department_and_role(self):
        emp = {"department": "Salg", "role": "employee"}
        self.assertTrue(hr_tools.compliance_requirement_applies(
            emp, {"applies_to_department": None, "applies_to_role": None}))
        self.assertTrue(hr_tools.compliance_requirement_applies(
            emp, {"applies_to_department": "Salg", "applies_to_role": None}))
        self.assertFalse(hr_tools.compliance_requirement_applies(
            emp, {"applies_to_department": "IT", "applies_to_role": None}))
        self.assertFalse(hr_tools.compliance_requirement_applies(
            emp, {"applies_to_department": None, "applies_to_role": "manager"}))

    def test_company_matrix_still_uses_the_shared_state_function(self):
        """The refactor's whole point: one implementation, two views."""
        self.assertIn("compliance_state_for_entries",
                      open(os.path.join(_REPO_ROOT, "hr_tools.py"), encoding="utf-8").read())


# ── Employee: open_in_app reaches the learner's own surfaces ──────────────

class OpenInAppSurfaceTests(unittest.TestCase):
    def test_new_actions_resolve_to_real_routes(self):
        for action, target in (("open_my_learning", "/min-laering"),
                               ("open_goals", "/mine-maal"),
                               ("open_timeline", "/min-tidslinje")):
            out = json.loads(tools._execute_open_in_app({"action": action}))
            self.assertEqual(out.get("status"), "success", action)
            self.assertEqual(out.get("target"), target)
            self.assertTrue(out.get("label"))

    def test_unknown_action_is_rejected(self):
        out = json.loads(tools._execute_open_in_app({"action": "drop_database"}))
        self.assertEqual(out.get("status"), "error")

    def test_schema_enum_matches_the_shared_ui_actions_list(self):
        fn = _schema("open_in_app", tools.OPENAI_TOOLS)
        enum = set(fn["parameters"]["properties"]["action"]["enum"])
        self.assertEqual(enum, set(sse_events.UI_ACTIONS))


# ── HR: hr_open_in_app ────────────────────────────────────────────────────

class HrOpenInAppTests(unittest.TestCase):
    def test_every_destination_resolves_to_a_path(self):
        for destination, (_ep, fallback, _label) in sse_events.HR_DESTINATIONS.items():
            out = json.loads(hr_tools._execute_hr_open_in_app({"destination": destination}))
            self.assertEqual(out.get("status"), "success", destination)
            # No request context here → the literal fallback path is used.
            self.assertEqual(out.get("target"), fallback, destination)
            self.assertTrue(out.get("label"), destination)

    def test_view_product_requires_a_handle(self):
        out = json.loads(hr_tools._execute_hr_open_in_app({"destination": "view_product"}))
        self.assertIn("error", out)
        out = json.loads(hr_tools._execute_hr_open_in_app(
            {"destination": "view_product", "handle": "python-basis"}))
        self.assertEqual(out["target"], "/products/python-basis")

    def test_open_catalog_carries_the_query(self):
        out = json.loads(hr_tools._execute_hr_open_in_app(
            {"destination": "open_catalog", "query": "gdpr"}))
        self.assertEqual(out["target"], "/catalog?q=gdpr")

    def test_unknown_destination_is_rejected(self):
        out = json.loads(hr_tools._execute_hr_open_in_app({"destination": "payroll"}))
        self.assertIn("error", out)

    def test_schema_enum_matches_the_shared_hr_action_list(self):
        fn = _schema("hr_open_in_app", hr_tools.HR_TOOLS)
        self.assertIsNotNone(fn)
        enum = set(fn["parameters"]["properties"]["destination"]["enum"])
        self.assertEqual(enum, set(sse_events.HR_UI_ACTIONS))

    def test_it_is_wired_into_the_dispatcher(self):
        call = mock.Mock()
        call.function.name = "hr_open_in_app"
        call.function.arguments = json.dumps({"destination": "compliance"})
        out = json.loads(hr_tools.execute_hr_tool(call))
        self.assertEqual(out.get("destination"), "compliance")

    def test_it_mutates_nothing(self):
        meta = get_tool_meta("hr_open_in_app", "hr")
        self.assertFalse(meta.side_effect)
        self.assertFalse(meta.confirm_required)
        self.assertFalse(meta.company_required)


# ── HR: selection, page context, panel rendering ──────────────────────────

class HrSelectionTests(unittest.TestCase):
    def test_navigation_is_always_on_the_menu(self):
        _tools, meta = get_hr_tool_selection(company_id=42, user_query="hvordan ser budgettet ud?")
        self.assertIn("hr_open_in_app", meta["tool_names"])

    def test_unambiguous_navigation_request_forces_the_tool(self):
        _tools, meta = get_hr_tool_selection(company_id=42, user_query="tag mig til den side")
        self.assertEqual(meta["forced_tool"], "hr_open_in_app")

    def test_ambiguous_navigation_request_only_offers_it(self):
        """TR-01: two branches pointing at different tools must demote the force
        rather than let the last match win — 'tag mig til compliance-siden' is
        both a navigation and a compliance question, so the model decides."""
        _tools, meta = get_hr_tool_selection(company_id=42, user_query="tag mig til compliance-siden")
        self.assertIsNone(meta["forced_tool"])
        self.assertIn("hr_open_in_app", meta["tool_names"])
        self.assertIn("get_compliance_status", meta["tool_names"])

    def test_page_context_surfaces_that_page_s_tools(self):
        _tools, meta = get_hr_tool_selection(
            company_id=42, user_query="hvem mangler her?", page="compliance")
        self.assertIn("get_compliance_status", meta["tool_names"])
        _tools, meta = get_hr_tool_selection(
            company_id=42, user_query="hvem mangler her?", page="suppliers")
        self.assertIn("hr_get_supplier_coverage", meta["tool_names"])

    def test_page_context_is_additive_not_a_replacement(self):
        _tools, meta = get_hr_tool_selection(
            company_id=42, user_query="hvad er vores budget?", page="compliance")
        self.assertIn("get_budget_overview", meta["tool_names"])
        self.assertIn("get_compliance_status", meta["tool_names"])

    def test_page_hint_covers_every_hr_destination_page(self):
        """A page the panel can be embedded on but that maps to no tools would
        silently make the page context a no-op there."""
        from ai_tool_registry import _HR_PAGE_TOOLS
        self.assertEqual(set(_HR_PAGE_TOOLS), set(sse_events.HR_DESTINATIONS))

    def test_page_context_never_surfaces_a_mutation(self):
        """Standing on a page says what the manager is LOOKING at, not what they
        intend — writes must stay behind their explicit keyword gate."""
        from ai_tool_registry import _HR_PAGE_TOOLS, _hr_page_tool_names
        for page in _HR_PAGE_TOOLS:
            for name in _hr_page_tool_names(page):
                self.assertFalse(get_tool_meta(name, "hr").side_effect, f"{page} -> {name}")

    def test_approvals_page_does_not_arm_the_approve_tool(self):
        _tools, meta = get_hr_tool_selection(
            company_id=42, user_query="hvad ligger her?", page="approvals")
        self.assertIn("get_pending_actions", meta["tool_names"])
        self.assertNotIn("approve_order_from_chat", meta["tool_names"])

    def test_page_hint_names_only_real_hr_tools(self):
        from ai_tool_registry import _HR_PAGE_TOOLS
        known = {tool_name(t) for t in hr_tools.HR_TOOLS}
        for page, names in _HR_PAGE_TOOLS.items():
            for name in names:
                self.assertIn(name, known, f"{page} -> {name}")

    def test_route_whitelists_the_posted_page(self):
        src = open(os.path.join(_REPO_ROOT, "hr_dashboard", "__init__.py"), encoding="utf-8").read()
        self.assertIn("handle_hr_ask(user_query, session, page=page)", src)
        self.assertIn("if page not in HR_DESTINATIONS", src)

    def test_panel_renders_the_navigation_directive(self):
        panel = open(AI_PANEL, encoding="utf-8").read()
        self.assertIn("ui_action", panel)
        self.assertIn("fm-aip-action", panel)
        # Only same-origin absolute paths may be navigated to.
        self.assertIn(r"/^\/[^\/]/.test(target)", panel)


# ── Vendor: vendor_catalog_health ─────────────────────────────────────────

class VendorCatalogHealthTests(unittest.TestCase):
    def _product(self, handle, vendor="Nordic Academy", **over):
        base = {
            "handle": handle, "title": handle.title(), "vendor": vendor,
            "price_min": 4500, "price_max": 4500,
            "dates": [(datetime.date.today() + datetime.timedelta(days=30)).strftime("%d-%m-%Y")],
            "description_text": "x" * 400,
            "categories": ["Ledelse"],
            "image_url": "https://cdn/x.jpg",
            "metadata": {"difficulty": "beginner", "language": "dansk", "duration_days": 2},
            "product_type": "Kursus",
        }
        base.update(over)
        return base

    def _run(self, products, **args):
        with mock.patch.object(vendor_tools, "_catalog_products", return_value=products):
            return vendor_tools.vendor_catalog_health(dict(args), "Nordic Academy")

    def test_requires_a_vendor_session(self):
        self.assertIn("error", vendor_tools.vendor_catalog_health({}, ""))

    def test_healthy_catalog_scores_100(self):
        out = self._run([self._product("a"), self._product("b")])
        self.assertEqual(out["health_score"], 100)
        self.assertEqual(out["courses_with_issues"], 0)
        self.assertEqual(out["courses"], [])

    def test_empty_catalog_is_an_honest_empty_state(self):
        out = self._run([self._product("a", vendor="Someone Else")])
        self.assertEqual(out["total_courses"], 0)
        self.assertIn("ingen kurser", out["summary_da"].lower())

    def test_only_this_vendor_s_own_courses_are_audited(self):
        out = self._run([
            self._product("mine"),
            self._product("theirs", vendor="Rival ApS", price_min=None, price_max=None),
        ])
        self.assertEqual(out["total_courses"], 1)
        self.assertNotIn("missing_price", out["issue_counts"])

    def test_missing_price_is_flagged(self):
        out = self._run([self._product("a", price_min=None, price_max=None)])
        self.assertIn("missing_price", out["issue_counts"])
        self.assertEqual(out["courses"][0]["handle"], "a")

    def test_only_past_dates_means_unbookable(self):
        past = (datetime.date.today() - datetime.timedelta(days=40)).strftime("%d-%m-%Y")
        out = self._run([self._product("a", dates=[past])])
        self.assertIn("no_upcoming_dates", out["issue_counts"])

    def test_one_upcoming_date_is_enough(self):
        past = (datetime.date.today() - datetime.timedelta(days=40)).strftime("%d-%m-%Y")
        future = (datetime.date.today() + datetime.timedelta(days=40)).strftime("%d-%m-%Y")
        out = self._run([self._product("a", dates=[past, future])])
        self.assertNotIn("no_upcoming_dates", out["issue_counts"])

    def test_an_unparseable_date_is_not_treated_as_expired(self):
        out = self._run([self._product("a", dates=["efter aftale"])])
        self.assertNotIn("no_upcoming_dates", out["issue_counts"])

    def test_thin_metadata_is_flagged(self):
        out = self._run([self._product("ok"),  # enrichment has run for this catalog
                         self._product("a", metadata={}, categories=[],
                                       description_text="kort", image_url="")])
        for key in ("missing_description", "missing_category", "missing_difficulty",
                    "missing_language", "missing_duration", "missing_image"):
            self.assertIn(key, out["issue_counts"], key)

    def test_worst_course_is_listed_first(self):
        out = self._run([
            self._product("mild", image_url=""),  # one cosmetic issue
            self._product("broken", price_min=None, price_max=None, metadata={},
                          categories=[], description_text="", image_url=""),
        ])
        self.assertEqual(out["courses"][0]["handle"], "broken")
        self.assertGreater(out["courses"][0]["severity"], out["courses"][1]["severity"])

    def test_issue_filter_narrows_the_fix_list(self):
        out = self._run([
            self._product("nopris", price_min=None, price_max=None),
            self._product("nobillede", image_url=""),
        ], issue="missing_price")
        self.assertEqual([c["handle"] for c in out["courses"]], ["nopris"])

    def test_unknown_issue_filter_is_rejected(self):
        self.assertIn("error", self._run([self._product("a")], issue="missing_soul"))

    def test_limit_is_clamped_and_applied(self):
        broken = [self._product(f"c{i}", price_min=None, price_max=None) for i in range(30)]
        out = self._run(broken, limit=3)
        self.assertEqual(out["shown"], 3)
        out = self._run(broken, limit=999)
        self.assertEqual(out["shown"], 25)

    def test_enrichment_gap_is_not_blamed_on_the_vendor(self):
        """structured_metadata is an LLM pass over the description, not a field the
        vendor fills in. If NO course carries it, the pass has not run — reporting
        'niveau mangler' on all of them would be blaming them for our build."""
        out = self._run([self._product("a", metadata={}), self._product("b", metadata={})])
        self.assertTrue(out["enrichment_missing"])
        for key in vendor_tools._DERIVED_CHECKS:
            self.assertNotIn(key, out["issue_counts"], key)
        self.assertEqual(out["health_score"], 100)
        self.assertIn("berigelse", out["summary_da"])

    def test_partial_enrichment_still_flags_the_stragglers(self):
        out = self._run([self._product("rich"), self._product("poor", metadata={})])
        self.assertFalse(out["enrichment_missing"])
        self.assertEqual(out["issue_counts"]["missing_difficulty"]["count"], 1)

    def test_derived_issue_filter_is_honest_when_enrichment_never_ran(self):
        out = self._run([self._product("a", metadata={})], issue="missing_difficulty")
        self.assertTrue(out["enrichment_missing"])
        self.assertEqual(out["courses"], [])

    def test_score_denominator_follows_the_checks_actually_applied(self):
        out = self._run([self._product("a")])
        self.assertEqual(out["checks_applied"], len(vendor_tools._HEALTH_CHECKS))
        out = self._run([self._product("a", metadata={})])
        self.assertEqual(out["checks_applied"],
                         len(vendor_tools._HEALTH_CHECKS) - len(vendor_tools._DERIVED_CHECKS))

    def test_it_is_wired_into_the_vendor_dispatcher(self):
        with mock.patch.object(vendor_tools, "_catalog_products", return_value=[self._product("a")]):
            out = vendor_tools.execute_vendor_tool("vendor_catalog_health", {}, "Nordic Academy")
        self.assertEqual(out.get("health_score"), 100)

    def test_schema_and_triggers_are_registered(self):
        self.assertIsNotNone(_schema("vendor_catalog_health", vendor_tools.VENDOR_TOOLS))
        self.assertIn("vendor_catalog_health", vendor_tools.VENDOR_TOOL_TRIGGER_KEYWORDS)
        self.assertIn("vendor_catalog_health", vendor_tools._VENDOR_TOOL_ROUTER)


# ── Reachability: the three-step rule (schema + executor + menu) ───────────

class ReachabilityTests(unittest.TestCase):
    def _names(self, query, **over):
        kwargs = dict(logged_in=True, company_id=42, intent="discovery",
                      user_query=query, shown_count=0)
        kwargs.update(over)
        _tools, meta = get_employee_tool_selection(**kwargs)
        return meta["tool_names"]

    def test_agenda_reachable_on_danish_phrasing(self):
        self.assertIn("get_my_agenda", self._names("hvad har jeg på tavlen lige nu?"))

    def test_agenda_reachable_on_a_deadline_question(self):
        self.assertIn("get_my_agenda", self._names("er jeg forsinket med mit kursus?"))

    def test_agenda_reachable_on_english_paraphrase(self):
        # Exact-keyword gates are Danish; the semantic fallback covers the rest.
        self.assertIn("get_my_agenda", self._names("what is on my plate right now"))

    def test_compliance_reachable_on_danish_phrasing(self):
        self.assertIn("get_my_compliance", self._names("hvilke kurser er obligatoriske for mig?"))

    def test_compliance_reachable_on_english_paraphrase(self):
        self.assertIn("get_my_compliance", self._names("am i compliant with mandatory training"))

    def test_compliance_offers_the_catalog_so_a_gap_can_be_closed(self):
        self.assertIn("catalog_search", self._names("mangler jeg noget lovpligtigt?"))

    def test_compliance_is_company_scoped(self):
        self.assertNotIn("get_my_compliance",
                         self._names("er jeg compliant?", company_id=None))

    def test_both_tools_require_login(self):
        names = self._names("hvad har jeg på tavlen? er jeg compliant?", logged_in=False)
        self.assertNotIn("get_my_agenda", names)
        self.assertNotIn("get_my_compliance", names)

    def test_navigation_stays_always_on(self):
        self.assertIn("open_in_app", self._names("hvad har jeg på tavlen?"))

    def test_new_tools_have_schemas_and_dispatch(self):
        source = open(os.path.join(_REPO_ROOT, "app1", "tools.py"), encoding="utf-8").read()
        for name in ("get_my_agenda", "get_my_compliance"):
            self.assertIsNotNone(_schema(name, tools.OPENAI_TOOLS), name)
            self.assertIn(f'function_name == "{name}"', source, name)

    def test_new_tools_carry_display_metadata(self):
        for name in ("get_my_agenda", "get_my_compliance"):
            meta = tool_display_metadata(name, "employee")
            self.assertNotEqual(meta["label"], name.replace("_", " ").capitalize(), name)
            self.assertFalse(meta["side_effect"], name)
        for name, scope in (("hr_open_in_app", "hr"), ("vendor_catalog_health", "vendor")):
            meta = tool_display_metadata(name, scope)
            self.assertTrue(meta["label"], name)
            self.assertFalse(meta["side_effect"], name)


# ── SSE vocabulary drift ──────────────────────────────────────────────────

class SseDriftTests(unittest.TestCase):
    def test_new_events_are_in_the_canonical_set(self):
        for event in (sse_events.AGENDA_CARD, sse_events.COMPLIANCE_CARD):
            self.assertIn(event, sse_events.KNOWN_EVENT_TYPES)

    def test_new_events_have_a_chat_js_branch(self):
        js = open(CHAT_JS, encoding="utf-8").read()
        for event in (sse_events.AGENDA_CARD, sse_events.COMPLIANCE_CARD):
            self.assertIn(f'data.type === "{event}"', js, event)

    def test_agent_emits_both_cards(self):
        src = open(os.path.join(_REPO_ROOT, "app1", "agent.py"), encoding="utf-8").read()
        self.assertIn('fn == "get_my_agenda"', src)
        self.assertIn('fn == "get_my_compliance"', src)

    def test_hr_agent_emits_the_navigation_directive(self):
        src = open(os.path.join(_REPO_ROOT, "hr_agent.py"), encoding="utf-8").read()
        self.assertIn('tool_result.name == "hr_open_in_app"', src)
        self.assertIn('"type": "ui_action"', src)

    def test_hr_destination_labels_match_the_subnav_vocabulary(self):
        subnav = open(os.path.join(_REPO_ROOT, "templates", "fm", "_hr_subnav.html"),
                      encoding="utf-8").read()
        for destination, (endpoint, _path, _label) in sse_events.HR_DESTINATIONS.items():
            self.assertIn(f"url_for('{endpoint}')", subnav, destination)
            self.assertIn(f"_hp == '{destination}'", subnav, destination)


if __name__ == "__main__":
    unittest.main()
