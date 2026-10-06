"""The learner's learning-path page: real titles, true order status, dates, no internals."""

import json
import unittest
from unittest import mock

from tests.sqlite_platform import PlatformDB, make_app, client_as, render_patches

CATALOG = {
    "a": {"handle": "a", "title": "Databeskyttelse (GDPR)", "vendor": "V", "price_min": 1000.0, "variants": []},
    "b": {"handle": "b", "title": "Ledelse for nye ledere", "vendor": "V", "price_min": 2000.0, "variants": []},
    "c": {"handle": "c", "title": "Projektledelse", "vendor": "V", "price_min": 3000.0, "variants": []},
}


class AssignmentPageTests(unittest.TestCase):
    def setUp(self):
        self.db = PlatformDB()
        self.app = make_app(self.db)
        p = render_patches()
        p.start()
        self.addCleanup(p.stop)
        d = self.db
        d.execute("INSERT INTO companies (id,company_name) VALUES (7,'Firma')")
        d.execute("INSERT INTO users (id,username,email) VALUES (1,'ada','a@f.dk'),(2,'hr','h@f.dk'),(5,'eve','e@f.dk')")
        d.execute("INSERT INTO company_users (company_id,user_id,username,full_name,role,status,department) VALUES "
                  "(7,1,'ada','Ada Hansen','employee','active','Salg'),(7,2,'hr','Hanne HR','hr_manager','active','HR'),"
                  "(7,5,'eve','Eve','employee','active','Salg')")
        d.execute("INSERT INTO employee_learning_progress (id,user_id,company_id,learning_path_id,content_type,content_name,status,"
                  "progress_percentage,due_date,ordering_mode,assigned_by_user_id) VALUES "
                  "(11,1,7,1,'learning_path','Ledelse 1','in_progress',50,'2026-12-31','sequential',2)")
        for oid, status, handle in (("o1", "booked", "a"), ("o2", "approved", "b"), ("o3", "pending_approval", "c")):
            d.execute("INSERT INTO course_orders (order_id,company_id,user_id,username,product_handle,product_title,price,variant_date,"
                      "variant_location,status,department) VALUES (%s,7,1,'ada',%s,%s,1000,'3. december 2099','Aarhus',%s,'Salg')",
                      (oid, handle, handle, status))
        d.execute("INSERT INTO course_order_details (order_id,company_id,user_id,booking_json) VALUES ('o1',7,1,%s)",
                  (json.dumps({"start_at": "2099-12-10T09:00:00+01:00"}),))
        rows = [
            (1, "catalog", "a", "GDPR", "o1", "ordered"),
            (2, "catalog", "b", "Ledelse", "o2", "ordered"),
            (3, "catalog", "c", "Projekt", "o3", "ordered"),
            (4, "info", None, "Tal med din leder", None, "not_started"),
            (5, "catalog", "b", "Ledelse 2", None, "not_started"),
        ]
        for pos, kind, handle, title, oid, status in rows:
            d.execute("INSERT INTO learning_assignment_steps (progress_id,company_id,user_id,path_version,position,step_type,course_handle,"
                      "title,order_id,status) VALUES (11,7,1,2,%s,%s,%s,%s,%s,%s)", (pos, kind, handle, title, oid, status))
        patcher = mock.patch("catalog_service.get_product", side_effect=lambda h: CATALOG.get(h))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ada = client_as(self.app, user="ada", user_id=1, company_id=7, company_role="employee")
        self.addCleanup(d.raw.close)

    def html(self):
        resp = self.ada.get("/min-laering/forloeb/11")
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_steps_show_the_catalogue_title_not_hrs_typed_label(self):
        html = self.html()
        self.assertIn("Databeskyttelse (GDPR)", html)
        self.assertIn("Ledelse for nye ledere", html)
        self.assertIn('lpa-sub">GDPR<', html)               # HR's label is only a quiet subtitle

    def test_each_step_shows_its_orders_real_status_label_never_bestilt(self):
        html = self.html()
        for label in ("Booket", "Godkendt", "Afventer godkendelse"):
            self.assertIn(label, html)
        self.assertNotIn("Bestilt<", html)
        self.assertNotIn(">Bestilt", html)

    def test_no_internal_version_text_for_the_employee(self):
        self.assertNotIn("Version", self.html())
        self.assertNotIn("version 2", self.html().lower())

    def test_dates_order_links_and_session(self):
        html = self.html()
        self.assertIn('href="/min-ordre/o1"', html)
        self.assertIn("10. december 2099", html)                # the booked start wins
        self.assertIn("3. december 2099", html)                 # the ordered session
        self.assertIn("Aarhus", html)

    def test_header_shows_assigner_due_date_and_progress(self):
        html = self.html()
        self.assertIn("Tildelt af Hanne HR", html)
        self.assertIn("Frist 31. december 2026", html)
        self.assertIn('aria-valuenow="50"', html)
        self.assertIn("50%", html)

    def test_a_sequential_path_explains_when_the_next_course_is_ordered(self):
        html = self.html()
        self.assertIn("Kurserne bestilles ét ad gangen.", html)
        self.assertIn("Næste kursus, ‘Ledelse for nye ledere’, bestilles, når du har gennemført de forrige trin.", html)
        self.assertIn("Bestilles automatisk, når de forrige trin er gennemført.", html)
        self.assertNotIn("Anmod om plads", html)                 # nothing to press for a course that is not due yet

    def test_one_remaining_step_before_the_next_course_is_named(self):
        self.db.execute("UPDATE learning_assignment_steps SET status = 'completed' WHERE position < 4")
        self.assertIn("når du har gennemført ‘Tal med din leder’.", self.html())

    def test_guidance_can_be_acknowledged_or_skipped_with_a_reason(self):
        step = self.db.one("SELECT id FROM learning_assignment_steps WHERE step_type='info'")["id"]
        url = "/min-laering/forloeb/11/trin/%s" % step
        self.ada.post(url, data={"action": "skip", "note": ""})
        self.assertEqual(self.db.one("SELECT status FROM learning_assignment_steps WHERE id=%s", (step,))["status"], "not_started")
        self.ada.post(url, data={"action": "skip", "note": "Ikke relevant"})
        self.assertEqual(self.db.one("SELECT status FROM learning_assignment_steps WHERE id=%s", (step,))["status"], "skipped")
        self.assertIn("Sprunget over", self.html())

    def test_someone_elses_assignment_is_a_404(self):
        eve = client_as(self.app, user="eve", user_id=5, company_id=7, company_role="employee")
        self.assertEqual(eve.get("/min-laering/forloeb/11").status_code, 404)

    def test_an_all_at_once_path_has_no_sequential_note(self):
        self.db.execute("UPDATE employee_learning_progress SET ordering_mode = 'all_at_once'")
        html = self.html()
        self.assertNotIn("Kurserne bestilles ét ad gangen.", html)
