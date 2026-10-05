import unittest
from tests.sqlite_platform import PlatformDB, make_app, client_as, render_patches
import customer_success


class CustomerLaunchTests(unittest.TestCase):
    def setUp(self):
        self.db = PlatformDB()
        self.app = make_app(self.db)
        p = render_patches()
        p.start()
        self.addCleanup(p.stop)
        self.db.execute("INSERT INTO companies (id,company_name) VALUES (7,'Firma'),(8,'Anden')")
        self.db.execute(
            "INSERT INTO users (id,username,email) VALUES (1,'learner','l@example.invalid'),(2,'hr','hr@example.invalid'),(3,'admin','a@example.invalid')"
        )
        self.db.execute("UPDATE users SET role='admin' WHERE id=3")
        self.db.execute(
            "INSERT INTO company_users (company_id,user_id,username,role,status,department,manager_user_id) VALUES (7,1,'learner','employee','active','Salg',2),(7,2,'hr','hr_manager','active','HR',NULL)"
        )
        self.hr = client_as(self.app, user="hr", user_id=2, company_id=7, company_role="hr_manager")
        self.admin = client_as(self.app, user="admin", user_id=3, role="admin")
        self.addCleanup(self.db.raw.close)

    def test_new_customer_is_not_ready_just_because_default_departments_exist(self):
        self.db.execute("INSERT INTO company_departments(company_id,department_name) VALUES (7,'General')")
        with self.app.app_context():
            r = customer_success.readiness(self.db.connection.cursor(), 7)
        self.assertFalse(r["checks"][0]["done"])
        self.assertEqual(r["booked"], 0)
        self.assertEqual(self.hr.get("/hr/kom-i-gang").status_code, 200)

    def test_customer_request_reaches_admin_and_response_returns_to_customer(self):
        self.assertEqual(
            self.hr.post(
                "/virksomhed/kundeforloeb", data={"kind": "seats", "note": "Vi har brug for ti pladser", "quantity": "10"}
            ).status_code,
            302,
        )
        row = self.db.one("SELECT * FROM customer_requests")
        self.assertEqual(row["company_id"], 7)
        self.assertIn("Vi har brug", self.admin.get("/admin/kundeforloeb/7").get_data(as_text=True))
        response = self.admin.post(
            "/admin/kundeforloeb/7",
            data={"action": "resolve", "request_id": row["id"], "status": "resolved", "resolution": "Vi aftaler omfang på fredag."},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("Vi aftaler omfang", self.hr.get("/virksomhed/kundeforloeb").get_data(as_text=True))
        other = client_as(self.app, user="other", user_id=99, company_id=8, company_role="hr_manager")
        self.assertNotIn("Vi aftaler omfang", other.get("/virksomhed/kundeforloeb").get_data(as_text=True))

    def test_account_handover_is_saved_and_visible_to_the_company(self):
        response = self.admin.post(
            "/admin/kundeforloeb/7",
            data={
                "stage": "pilot",
                "account_owner": "Kundeteamet",
                "account_email": "team@example.invalid",
                "offer_name": "Læringspilot",
                "success_criteria": "Tre bekræftede kursusforløb",
                "pilot_end": "2099-01-01",
            },
        )
        self.assertEqual(response.status_code, 302)
        page = self.hr.get("/virksomhed/kundeforloeb").get_data(as_text=True)
        self.assertIn("Læringspilot", page)
        self.assertIn("Tre bekræftede", page)

    def test_demo_enquiry_is_durable_and_requires_contact_consent(self):
        client = self.app.test_client()
        fields = {"name": "Kontakt", "email": "contact@example.invalid", "company_name": "Ny firma", "message": "En demo"}
        client.post("/for-virksomheder", data=fields)
        self.assertEqual(self.db.query("SELECT * FROM sales_enquiries"), [])
        response = client.post("/for-virksomheder", data={**fields, "contact_consent": "yes"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(self.db.query("SELECT * FROM sales_enquiries")), 1)
        self.assertIn("Ny firma", self.admin.get("/admin/kundeforloeb").get_data(as_text=True))

    def test_employee_cannot_change_company_readiness_or_account_requests(self):
        employee = client_as(self.app, user="learner", user_id=1, company_id=7, company_role="employee")
        self.assertEqual(employee.post("/hr/kom-i-gang", data={"check_key": "organisation", "note": "Ready"}).status_code, 403)
        self.assertEqual(employee.get("/admin/kundeforloeb").status_code, 302)

    def test_customer_request_and_response_have_actionable_notifications(self):
        self.hr.post("/virksomhed/kundeforloeb", data={"kind": "support", "note": "Hjælp til pilot"})
        request = self.db.one("SELECT * FROM customer_requests")
        self.assertTrue(self.db.one("SELECT id FROM notifications WHERE user_id='admin'"))
        self.assertTrue(self.db.one("SELECT id FROM mail_outbox WHERE to_email='a@example.invalid'"))
        self.admin.post(
            "/admin/kundeforloeb/7",
            data={"action": "resolve", "request_id": request["id"], "status": "resolved", "resolution": "Her er næste skridt"},
        )
        self.assertTrue(self.db.one("SELECT id FROM notifications WHERE user_id='hr' AND kind='customer_response'"))

    def test_internal_course_screens_and_enrolment_are_connected(self):
        self.db.execute("INSERT INTO company_courses(id,company_id,title,price,location) VALUES(5,7,'Introduktion',0,'Kontoret')")
        learner = client_as(self.app, user="learner", user_id=1, company_id=7, company_role="employee")
        self.assertIn("Introduktion", learner.get("/interne-kurser").get_data(as_text=True))
        self.assertEqual(learner.get("/interne-kurser/5").status_code, 200)
        result = learner.post("/interne-kurser/5", data={"email": "l@example.invalid"})
        self.assertEqual(result.status_code, 302)
        order = self.db.one("SELECT * FROM course_orders")
        self.assertEqual(order["internal_course_id"], 5)
        self.assertEqual(learner.get("/ordre/%s/booking" % order["order_id"]).status_code, 200)
        other = client_as(self.app, user="other", user_id=99, company_id=8, company_role="hr_manager")
        self.assertEqual(other.get("/interne-kurser/5").status_code, 404)

    def test_hr_assignment_requires_a_real_price_preview(self):
        self.db.execute("INSERT INTO company_courses(id,company_id,title,price,location) VALUES(5,7,'Introduktion',250,'Kontoret')")
        response = self.hr.post("/hr/assign-course", data={"handle": "internal:5", "employee_ids": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("250", response.get_data(as_text=True))
        self.assertEqual(self.db.query("SELECT * FROM course_orders"), [])
        self.hr.post("/hr/assign-course", data={"handle": "internal:5", "employee_ids": "1", "confirm": "yes", "expected_price": "250"})
        self.assertEqual(float(self.db.one("SELECT price FROM course_orders")["price"]), 250)

    def test_daily_followup_flags_stalled_bookings_and_upcoming_pilot(self):
        import launch_followup
        import datetime

        self.db.execute("INSERT INTO customer_accounts(company_id,pilot_end,stage) VALUES(7,'2026-01-01','pilot')")
        with self.app.app_context():
            first = launch_followup.run(now=datetime.datetime(2026, 1, 1))
            second = launch_followup.run(now=datetime.datetime(2026, 1, 1))
        self.assertEqual(first["accounts"], 1)
        self.assertEqual(second["accounts"], 1)
        self.assertEqual(len(self.db.query("SELECT * FROM mail_outbox")), 1)

    def test_api_order_uses_member_identity_and_canonical_price(self):
        import inspect
        from flask import g
        import enterprise_api

        self.db.execute("INSERT INTO company_courses(id,company_id,title,price,location) VALUES(5,7,'Introduktion',250,'Kontoret')")
        with self.app.test_request_context(
            "/api/v1/orders",
            method="POST",
            json={
                "product_handle": "internal:5",
                "product_title": "Untrusted title",
                "price": 1,
                "user_email": "l@example.invalid",
                "user_name": "Learner",
            },
        ):
            g.company_id = 7
            response, status = inspect.unwrap(enterprise_api.create_order_api)()
        self.assertEqual(status, 201, response.get_json())
        row = self.db.one("SELECT * FROM course_orders")
        self.assertEqual((row["user_id"], row["username"], row["status"]), (1, "learner", "pending_approval"))
        self.assertEqual(float(row["price"]), 250)
        self.assertEqual(row["product_title"], "Introduktion")

    def test_api_bulk_failure_is_not_reported_as_success(self):
        import inspect
        from flask import g
        import enterprise_api

        with self.app.test_request_context(
            "/api/v1/bulk/enroll", method="POST", json={"product_handle": "internal:5", "employee_ids": [99999]}
        ):
            g.company_id = 7
            response, status = inspect.unwrap(enterprise_api.bulk_enroll)()
        self.assertEqual(status, 400)
        self.assertFalse(response.get_json()["success"])

    def test_hr_ai_course_assignment_creates_the_same_real_employee_order(self):
        import json
        import hr_tools
        from flask import session

        self.db.execute("INSERT INTO company_courses(id,company_id,title,price,location) VALUES(5,7,'Introduktion',250,'Kontoret')")
        with self.app.test_request_context("/hr/"):
            session.update(user="hr", user_id=2, company_id=7, company_role="hr_manager")
            preview = json.loads(hr_tools._execute_assign_learning_path_to_team({"course_handle": "internal:5", "employee_ids": [1]}))
            self.assertEqual(preview["confirmation_args"]["expected_price"], 250)
            self.assertEqual(self.db.query("SELECT * FROM course_orders"), [])
            result = json.loads(
                hr_tools._execute_assign_learning_path_to_team(
                    {"course_handle": "internal:5", "employee_ids": [1], "confirm": True, **preview["confirmation_args"]}
                )
            )
        self.assertTrue(result["success"], result)
        row = self.db.one("SELECT * FROM course_orders")
        self.assertEqual((row["user_id"], row["status"]), (1, "pending_approval"))
