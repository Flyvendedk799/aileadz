"""N-3.5: one department budget, rename cascades, one Mit team page."""

import os
import unittest

os.environ.setdefault("SANDBOX", "1")

import department_service as ds  # noqa: E402
from tests.sqlite_mysql import SqliteMysql  # noqa: E402


class DepartmentServiceTests(unittest.TestCase):
    def setUp(self):
        self.db = SqliteMysql()
        d = self.db
        d.raw.executescript("""
            CREATE TABLE company_skill_targets (id INTEGER PRIMARY KEY, company_id INTEGER, department TEXT, skill_name TEXT);
            CREATE TABLE compliance_requirements (id INTEGER PRIMARY KEY, company_id INTEGER, applies_to_department TEXT, title TEXT);
        """)
        d.execute("INSERT INTO company_users (company_id, user_id, username, department, status) VALUES "
                  "(7,1,'a','Salg','active'),(7,2,'b','Salg','active'),(8,3,'c','Salg','active')")
        d.execute("INSERT INTO department_budgets (company_id, department, annual_budget, spent, fiscal_year) VALUES (7,'Salg',1000,400,2026),(8,'Salg',5,0,2026)")
        d.execute("INSERT INTO company_approval_policies (company_id, department, auto_approve_under) VALUES (7,'Salg',500)")
        d.execute("INSERT INTO company_skill_targets (company_id, department, skill_name) VALUES (7,'Salg','Forhandling')")
        d.execute("INSERT INTO compliance_requirements (company_id, applies_to_department, title) VALUES (7,'Salg','GDPR')")
        d.execute("INSERT INTO course_orders (order_id, company_id, department, status) VALUES ('o1',7,'Salg','approved')")
        self.cur = d.connection.cursor()

    def test_rename_cascades_everywhere_but_only_in_this_company(self):
        changed = ds.rename_department(self.cur, 7, "Salg", "Salg & Marketing")
        self.db.raw.commit()
        for table in ("company_users", "department_budgets", "company_approval_policies",
                      "company_skill_targets", "compliance_requirements", "course_orders"):
            self.assertGreaterEqual(changed[table], 1, table)
        self.assertEqual(self.db.one("SELECT department FROM department_budgets WHERE company_id=7")["department"], "Salg & Marketing")
        self.assertEqual(self.db.one("SELECT department FROM department_budgets WHERE company_id=8")["department"], "Salg")
        self.assertEqual(self.db.one("SELECT department FROM company_users WHERE company_id=8")["department"], "Salg")

    def test_per_employee_budget_feeds_department_budgets_keeping_spent(self):
        annual = ds.sync_budget_from_per_employee(self.cur, 7, "Salg", 2500, year=2026)
        self.assertEqual(annual, 5000)            # 2 active employees x 2.500
        row = self.db.one("SELECT annual_budget, spent FROM department_budgets WHERE company_id=7")
        self.assertEqual((row["annual_budget"], row["spent"]), (5000, 400))

    def test_zero_per_employee_budget_changes_nothing(self):
        self.assertIsNone(ds.sync_budget_from_per_employee(self.cur, 7, "Salg", 0, year=2026))
        self.assertEqual(self.db.one("SELECT annual_budget FROM department_budgets WHERE company_id=7")["annual_budget"], 1000)

    def test_new_department_gets_a_budget_row(self):
        ds.sync_budget_from_per_employee(self.cur, 7, "Drift", 1000, year=2026)
        self.assertEqual(self.db.one("SELECT annual_budget FROM department_budgets WHERE department='Drift'")["annual_budget"], 1000)


class OldTeamPagesRedirectTests(unittest.TestCase):
    def test_my_department_and_department_analytics_redirect_to_mit_team(self):
        import run
        client = run.create_app().test_client()
        with client.session_transaction() as s:
            s["user"] = "chef"; s["company_id"] = 7; s["company_role"] = "department_head"
        r = client.get("/hr/my-department")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/hr/team", r.headers["Location"])
        self.assertIn("scope=department", r.headers["Location"])
        r2 = client.get("/multitenant-reports/department/Salg")
        self.assertEqual(r2.status_code, 302)
        self.assertIn("/hr/team", r2.headers["Location"])


if __name__ == "__main__":
    unittest.main()
