"""N-4.3: compliance -> action (assign required course), edit/delete boundaries."""

import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

from flask import Flask  # noqa: E402

import compliance_assign as ca  # noqa: E402
import order_service as svc  # noqa: E402
from tests.sqlite_mysql import SqliteMysql  # noqa: E402


class AssignTests(unittest.TestCase):
    def setUp(self):
        self.db = SqliteMysql()
        self.app = Flask(__name__)
        self.app.mysql = self.db
        cm = self.app.app_context()
        cm.push()
        self.addCleanup(cm.pop)
        d = self.db
        d.raw.executescript("""CREATE TABLE compliance_requirements (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER,
            title TEXT, category TEXT, applies_to_department TEXT, applies_to_role TEXT, required_course_handle TEXT,
            recurrence_months INTEGER, is_statutory INTEGER);""")
        d.execute("INSERT INTO users (id, username, email) VALUES (1,'ada','a@f.dk'),(2,'bo','b@f.dk'),(3,'cy','c@f.dk'),(4,'hr','hr@f.dk')")
        d.execute("INSERT INTO company_users (company_id,user_id,username,email,role,department,status) VALUES "
                  "(7,1,'ada','a@f.dk','employee','Drift','active'),(7,2,'bo','b@f.dk','employee','Drift','active'),"
                  "(7,3,'cy','c@f.dk','employee','Salg','active'),(7,4,'hr','hr@f.dk','hr_manager','HR','active')")
        d.execute("INSERT INTO compliance_requirements (id,company_id,title,applies_to_department,required_course_handle,recurrence_months,is_statutory) "
                  "VALUES (1,7,'Brandøvelse','Drift','brand-101',12,1),(2,7,'Uden kursus','Drift',NULL,0,0)")
        d.execute("INSERT INTO course_orders (order_id,company_id,user_id,username,product_handle,status) VALUES ('x',7,2,'bo','brand-101','completed')")
        patches = [
            mock.patch.object(svc, "_emit_event_safe"), mock.patch.object(svc, "_send_email_safe"),
            mock.patch.object(svc, "_send_approval_needed_emails_safe"),
            mock.patch.object(svc, "_manager_recipient_emails", return_value=[]),
            mock.patch.object(svc, "_vendor_id_for_handle", return_value=None),
            mock.patch("catalog_service.get_product", return_value={"title": "Brand 101", "price_min": 900}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.cur = d.connection.cursor()
        self.hr = svc.OrderContext(company_id=7, user_id=4, username="hr", company_role="hr_manager", department="HR")

    def test_orders_only_for_applicable_employees_without_the_course(self):
        res = ca.assign_required_course(self.cur, self.hr, 7, 1)
        self.assertEqual((res["created"], res["skipped"]), (1, 1))     # ada gets it, bo already completed
        row = self.db.one("SELECT * FROM course_orders WHERE user_id = 1")
        self.assertEqual(row["status"], "pending_approval")            # normal approval + budget rules
        self.assertIn("Tildelt af HR", row["request_notes"])
        self.assertIsNone(self.db.one("SELECT 1 AS x FROM course_orders WHERE user_id = 3"))   # Salg not in scope

    def test_running_twice_does_not_duplicate(self):
        ca.assign_required_course(self.cur, self.hr, 7, 1)
        again = ca.assign_required_course(self.cur, self.hr, 7, 1)
        self.assertEqual(again["created"], 0)

    def test_requirement_without_course_explains_what_to_do(self):
        res = ca.assign_required_course(self.cur, self.hr, 7, 2)
        self.assertEqual(res["created"], 0)
        self.assertIn("handle", res["message"])

    def test_other_company_requirement_is_not_found(self):
        self.assertIn("ikke fundet", ca.assign_required_course(self.cur, self.hr, 8, 1)["message"])


class RoutePermissionTests(unittest.TestCase):
    def test_employee_cannot_edit_delete_or_assign(self):
        import run
        client = run.create_app().test_client()
        with client.session_transaction() as s:
            s["user"] = "ada"; s["company_id"] = 7; s["company_role"] = "employee"
        for url in ("/hr/compliance/1/edit", "/hr/compliance/1/delete", "/hr/compliance/1/assign"):
            self.assertEqual(client.post(url, data={"title": "x"}).status_code, 302, url)


if __name__ == "__main__":
    unittest.main()
