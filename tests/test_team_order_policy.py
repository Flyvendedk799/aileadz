"""N-5.2: team orders from chat follow the company policy."""

import json
import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

from flask import Flask  # noqa: E402

import order_service as svc  # noqa: E402
import team_order_policy as tp  # noqa: E402
from app1 import tools  # noqa: E402
from tests.sqlite_mysql import SqliteMysql  # noqa: E402

EXTRA = """
CREATE TABLE company_team_order_policy (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER,
  vendor_id INTEGER, mode TEXT, updated_by INTEGER, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""

PRODUCT = {"handle": "prince2", "title": "PRINCE2", "vendor": "Kursus ApS", "vendor_slug": "kursus-aps",
           "product_type": "Kursus", "price_min": 4000.0,
           "variants": [{"price": 4000.0, "date": "1. juni 2099", "location": "København", "city": "København"}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.db = SqliteMysql()
        self.db.raw.executescript(EXTRA)
        self.app = Flask(__name__)
        self.app.secret_key = "x"
        self.app.mysql = self.db
        d = self.db
        d.execute("INSERT INTO companies (id, company_name) VALUES (7, 'A'), (8, 'B')")
        for uid, name in ((1, "ada"), (2, "bo"), (3, "cy"), (4, "eve"), (5, "hr")):
            d.execute("INSERT INTO users (id, username, email) VALUES (%s, %s, %s)", (uid, name, name + "@a.dk"))
        d.execute("INSERT INTO company_users (company_id, user_id, username, full_name, email, role, department, status) VALUES "
                  "(7,1,'ada','Ada Hansen','ada@a.dk','employee','Salg','active'),"
                  "(7,2,'bo','Bo Jensen','bo@a.dk','employee','Salg','active'),"
                  "(7,3,'cy','Bo Madsen','cy@a.dk','employee','Drift','active'),"
                  "(7,5,'hr','Hanne HR','hr@a.dk','hr_manager','HR','active'),"
                  "(8,4,'eve','Eve Fremmed','eve@a.dk','employee','Salg','active')")
        d.execute("INSERT INTO vendors (id, vendor_name, slug, contact_email) VALUES (11, 'Kursus ApS', 'kursus-aps', 'v@k.dk')")
        d.execute("INSERT INTO department_budgets (company_id, department, annual_budget, spent, fiscal_year) "
                  "VALUES (7,'Salg',100000,0,%s)", (svc.datetime.datetime.now().year,))
        self.emails = []
        self.patches = [
            mock.patch.object(svc, "_emit_event_safe"),
            mock.patch.object(svc, "_send_email_safe", side_effect=lambda *a, **k: self.emails.append(a)),
            mock.patch.object(svc, "_send_approval_needed_emails_safe"),
            mock.patch.object(svc, "_manager_recipient_emails", return_value=[]),
            mock.patch.object(svc, "_vendor_id_for_handle", return_value=11),
            mock.patch.object(tools.catalog, "get_product", return_value=PRODUCT),
            mock.patch.object(tools, "apply_discount", return_value=(None, None, None)),
            mock.patch.object(tools, "mark_order_flow_open"),
            mock.patch.object(tools, "clear_order_flow"),
            mock.patch.object(tools, "resolve_user_contact", return_value={
                "name": "Ada Hansen", "email": "ada@a.dk", "phone": "", "sources": {}, "missing_required": []}),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.ctx = self.app.test_request_context("/")
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        from flask import session
        self.session = session
        session.update({"user": "ada", "user_id": 1, "company_id": 7, "company_role": "employee",
                        "company_department": "Salg"})

    def policy(self, mode, vendor_id=None):
        tp.set_policy(self.db.connection, 7, mode, vendor_id)

    def order(self, **args):
        args.setdefault("product_handle", "prince2")
        return json.loads(tools._execute_create_order(args, "ada"))


class PolicyServiceTests(Base):
    def test_default_when_nothing_is_configured(self):
        cur = self.db.connection.cursor()
        self.assertEqual(tp.effective_mode(cur, 7, 11), tp.DEFAULT_MODE)

    def test_vendor_override_beats_company_default_and_can_be_cleared(self):
        self.policy(tp.LINKED)
        self.policy(tp.NOT_ALLOWED, vendor_id=11)
        cur = self.db.connection.cursor()
        self.assertEqual(tp.effective_mode(cur, 7, 11), tp.NOT_ALLOWED)
        self.assertEqual(tp.effective_mode(cur, 7, 99), tp.LINKED)
        tp.clear_vendor_override(self.db.connection, 7, 11)
        self.assertEqual(tp.effective_mode(self.db.connection.cursor(), 7, 11), tp.LINKED)

    def test_invalid_mode_is_refused(self):
        with self.assertRaises(ValueError):
            tp.set_policy(self.db.connection, 7, "anything")

    def test_policies_are_per_company(self):
        self.policy(tp.NOT_ALLOWED)
        self.assertEqual(tp.effective_mode(self.db.connection.cursor(), 8, None), tp.DEFAULT_MODE)

    def test_participants_resolve_within_the_company_only(self):
        cur = self.db.connection.cursor()
        res = tp.resolve_participants(cur, 7, ["Ada Hansen", "bo@a.dk", "Eve Fremmed", "an", "mig"], requester_username="ada")
        self.assertEqual({p["username"] for p in res["matched"]}, {"ada", "bo"})
        self.assertEqual(res["unmatched"], ["Eve Fremmed"])           # other tenant is invisible
        self.assertIn("an", res["ambiguous"])                           # Ada Hansen vs Hanne HR


class TeamOrderFlowTests(Base):
    def test_not_allowed_explains_instead_of_ordering(self):
        self.policy(tp.NOT_ALLOWED)
        out = self.order(participants=["Bo Jensen"], confirm=True)
        self.assertEqual(out["status"], "team_not_allowed")
        self.assertEqual(self.db.one("SELECT COUNT(*) AS c FROM course_orders")["c"], 0)

    def test_vendor_override_can_forbid_one_vendor_only(self):
        self.policy(tp.LINKED)
        self.policy(tp.NOT_ALLOWED, vendor_id=11)
        self.assertEqual(self.order(participants=["Bo Jensen"])["status"], "team_not_allowed")

    def test_linked_orders_preview_then_create_one_order_per_person_sharing_a_group(self):
        self.policy(tp.LINKED)
        preview = self.order(participants=["Bo Jensen", "Ada Hansen"])
        self.assertTrue(preview["needs_confirmation"])
        self.assertEqual(sorted(preview["details"]["participants"]), ["Ada Hansen", "Bo Jensen"])
        self.assertEqual(preview["details"]["total"], 8000.0)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS c FROM course_orders")["c"], 0)     # nothing booked yet
        out = self.order(participants=["Bo Jensen", "Ada Hansen"], confirm=True)
        self.assertEqual(out["status"], "team_orders_created")
        self.assertEqual(out["created"], 2)
        rows = self.db.query("SELECT username, status, group_order_id, vendor_id, request_notes FROM course_orders ORDER BY id")
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({r["group_order_id"] for r in rows}), 1)
        self.assertTrue(rows[0]["group_order_id"])
        self.assertEqual({r["username"] for r in rows}, {"ada", "bo"})
        self.assertTrue(all(r["status"] == "pending_approval" for r in rows))   # each goes through approval
        self.assertTrue(all("Bestilt af ada" in r["request_notes"] for r in rows))

    def test_an_employee_cannot_get_a_managers_order_auto_approved(self):
        self.policy(tp.LINKED)
        out = self.order(participants=["Hanne HR"], confirm=True)
        self.assertEqual(out["status"], "team_orders_created")
        self.assertEqual(self.db.one("SELECT status FROM course_orders")["status"], "pending_approval")

    def test_manager_team_assignment_uses_the_same_employee_policy_as_hr_screen(self):
        self.policy(tp.LINKED)
        self.session.update({"user": "hr", "user_id": 5, "company_role": "hr_manager", "company_department": "HR"})
        with mock.patch.object(tools, "resolve_user_contact", return_value={
                "name": "Hanne HR", "email": "hr@a.dk", "phone": "", "sources": {}, "missing_required": []}):
            out = json.loads(tools._execute_create_order(
                {"product_handle": "prince2", "participants": ["Bo Jensen"], "confirm": True}, "hr"))
        self.assertEqual(out["created"], 1)
        self.assertEqual(self.db.one("SELECT status FROM course_orders")["status"], "pending_approval")

    def test_hr_bulk_assign_hands_the_request_to_hr_with_a_prefilled_link(self):
        self.policy(tp.HR_BULK)
        out = self.order(participants=["Bo Jensen"], confirm=True)
        self.assertEqual(out["status"], "handed_off_to_hr")
        self.assertEqual(self.db.one("SELECT COUNT(*) AS c FROM course_orders")["c"], 0)
        n = self.db.one("SELECT user_id, action_url FROM notifications")
        self.assertEqual(n["user_id"], "hr")
        self.assertIn("/hr/assign-course?course=prince2&users=2", n["action_url"])

    def test_unknown_colleague_asks_for_clarification(self):
        self.policy(tp.LINKED)
        out = self.order(participants=["Nogen Ukendt"], confirm=True)
        self.assertEqual(out["status"], "needs_info")
        self.assertEqual(out["unmatched"], ["Nogen Ukendt"])

    def test_team_orders_need_a_company(self):
        self.session.pop("company_id")
        self.assertEqual(self.order(participants=["Bo"])["status"], "team_not_available")

    def test_self_order_is_unchanged_without_participants(self):
        out = self.order()
        self.assertTrue(out["needs_confirmation"])
        self.assertNotIn("participants", out.get("details", {}))


class SettingsRouteTests(Base):
    def _client(self, role):
        import run
        app = run.create_app()
        app.mysql = self.db
        c = app.test_client()
        with c.session_transaction() as s:
            s.update({"user": "x", "user_id": 5, "company_id": 7, "company_role": role})
        return c

    def test_hr_can_save_the_company_default_and_a_vendor_override(self):
        c = self._client("hr_manager")
        self.assertIn(c.post("/hr/team-order-policy/save", data={"mode": "hr_bulk_assign"}).status_code, (302, 303))
        self.assertIn(c.post("/hr/team-order-policy/save", data={"mode": "not_allowed", "vendor_id": "11"}).status_code, (302, 303))
        cur = self.db.connection.cursor()
        pol = tp.get_policies(cur, 7)
        self.assertEqual((pol["default"], pol["vendors"]), ("hr_bulk_assign", {11: "not_allowed"}))

    def test_employee_and_department_head_cannot_change_policy(self):
        for role in ("employee", "department_head"):
            c = self._client(role)
            r = c.post("/hr/team-order-policy/save", data={"mode": "not_allowed"})
            self.assertEqual(r.status_code, 403, role)
        self.assertIsNone(self.db.one("SELECT * FROM company_team_order_policy"))

    def test_partial_renders_nothing_for_employees(self):
        import capabilities
        self.assertFalse(tp.policy_state()["allowed"] if not capabilities.can("company.policies") else False)


if __name__ == "__main__":
    unittest.main()
