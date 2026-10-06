"""Assigned by HR or a manager = approved: no self-approval queue, budget still charged,
approver recorded, one learner notification, honest copy and labels."""

import datetime
import unittest

import order_service as svc
from tests.sqlite_platform import PlatformDB, make_app, client_as, render_patches


class AssignmentApprovalTests(unittest.TestCase):
    def setUp(self):
        self.db = PlatformDB()
        self.app = make_app(self.db)
        p = render_patches()
        p.start()
        self.addCleanup(p.stop)
        d = self.db
        d.execute("INSERT INTO companies (id,company_name) VALUES (7,'Firma')")
        d.execute("INSERT INTO users (id,username,email) VALUES (1,'ada','a@f.dk'),(2,'hr','hr@f.dk'),(4,'bo','b@f.dk')")
        d.execute("INSERT INTO company_users (company_id,user_id,username,full_name,email,role,status,department) VALUES "
                  "(7,1,'ada','Ada Hansen','a@f.dk','employee','active','Salg'),"
                  "(7,4,'bo','Bo Jensen','b@f.dk','employee','active','Salg'),"
                  "(7,2,'hr','Hanne HR','hr@f.dk','hr_manager','active','HR')")
        d.execute("INSERT INTO company_courses(id,company_id,title,price,location) VALUES(5,7,'Introduktion',500,'Kontoret')")
        d.execute("INSERT INTO department_budgets (company_id,department,annual_budget,spent,fiscal_year) VALUES (7,'Salg',10000,0,%s)",
                  (datetime.datetime.now().year,))
        self.hr = client_as(self.app, user="hr", user_id=2, company_id=7, company_role="hr_manager")
        self.addCleanup(d.raw.close)

    def assign(self, ids=("1", "4")):
        return self.hr.post("/hr/assign-course", data={
            "handle": "internal:5", "employee_ids": list(ids), "confirm": "yes", "expected_price": "500"})

    def flashes(self, resp_client):
        with resp_client.session_transaction() as s:
            return [m for _, m in s.get("_flashes", [])]

    def test_assigning_two_people_leaves_nothing_to_approve(self):
        resp = self.assign()
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("/hr/approvals", resp.headers["Location"])
        orders = self.db.query("SELECT user_id, status, approved_by FROM course_orders ORDER BY user_id")
        self.assertEqual([(o["status"], o["approved_by"]) for o in orders], [("approved", 2), ("approved", 2)])
        self.assertEqual(self.db.one("SELECT COUNT(*) AS c FROM order_approvals WHERE status = 'pending'")["c"], 0)
        approval = self.db.one("SELECT approver_user_id, notes FROM order_approvals WHERE requester_user_id = 1")
        self.assertEqual((approval["approver_user_id"], approval["notes"]), (2, "Godkendt ved tildeling"))
        self.assertEqual(self.db.one("SELECT COUNT(*) AS c FROM notifications WHERE user_id = 'hr'")["c"], 0)
        self.assertEqual(self.flashes(self.hr), ["2 bestillinger oprettet og godkendt til 'Introduktion'."])

    def test_budget_is_still_charged_for_assigned_orders(self):
        self.assign()
        self.assertEqual(float(self.db.one("SELECT spent FROM department_budgets")["spent"]), 1000)
        self.assertEqual([o["budget_charged"] for o in self.db.query("SELECT budget_charged FROM course_orders")], [1, 1])

    def test_each_learner_gets_exactly_one_assignment_notification(self):
        self.assign()
        for name in ("ada", "bo"):
            rows = self.db.query("SELECT title, kind, action_url FROM notifications WHERE user_id = %s", (name,))
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["title"], rows[0]["kind"]), ("Du er tildelt Introduktion", "assignment"))
            self.assertTrue(rows[0]["action_url"].startswith("/min-ordre/"))

    def test_labels_name_the_assigner_and_say_approved(self):
        self.assign(("1",))
        oid = self.db.one("SELECT order_id FROM course_orders")["order_id"]
        learner = client_as(self.app, user="ada", user_id=1, company_id=7, company_role="employee")
        self.assertIn("Tildelt af Hanne HR · godkendt", learner.get("/min-ordre/%s" % oid).get_data(as_text=True))
        self.assertIn("Tildelt af Hanne HR · godkendt", learner.get("/min-tidslinje").get_data(as_text=True))
        self.assertIn("Godkendt ved tildeling", self.hr.get("/hr/order/%s/details" % oid).get_data(as_text=True))

    def test_confirm_page_says_assigned_courses_are_approved_at_once(self):
        html = self.hr.post("/hr/assign-course", data={"handle": "internal:5", "employee_ids": "1"}).get_data(as_text=True)
        self.assertIn("Tildelte kurser er godkendt med det samme og trækkes på afdelingens budget.", html)
        self.assertNotIn("følger virksomhedens godkendelses", html)

    def test_over_budget_assignment_still_goes_to_approval(self):
        self.db.execute("UPDATE department_budgets SET annual_budget = 700")
        self.assign()
        self.assertEqual(sorted(o["status"] for o in self.db.query("SELECT status FROM course_orders")),
                         ["approved", "pending_approval"])        # the second person would overspend

    def test_an_api_actor_without_a_user_id_never_pre_approves(self):
        ctx = svc.OrderContext(company_id=7, company_role="company_admin", source="api")
        with self.app.app_context():
            out = svc.create_order(ctx, product_handle="x", product_title="X", price=100,
                                   extra={"assign_to": {"user_id": 1, "username": "ada", "department": "Salg", "email": "a@f.dk"}})
        self.assertEqual(out["status"], "pending_approval")

    def test_a_learner_ordering_for_themselves_is_unchanged(self):
        ctx = svc.OrderContext(company_id=7, user_id=1, username="ada", company_role="employee", department="Salg")
        with self.app.app_context():
            out = svc.create_order(ctx, product_handle="x", product_title="X", price=100)
        self.assertEqual(out["status"], "pending_approval")
        self.assertFalse(out["pre_approved"])
