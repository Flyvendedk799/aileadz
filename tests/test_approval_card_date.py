"""The approval card tells HR WHEN the course is, in the shared Danish formats."""

import datetime
import json
import unittest
from unittest import mock

import order_timing
from tests.sqlite_platform import PlatformDB, make_app, client_as, render_patches


class ApprovalCardDateTests(unittest.TestCase):
    def setUp(self):
        self.db = PlatformDB()
        self.app = make_app(self.db)
        p = render_patches()
        p.start()
        self.addCleanup(p.stop)
        now = mock.patch.object(order_timing, "now", lambda: datetime.datetime(2026, 11, 20, 10, 0, tzinfo=order_timing.TZ))
        now.start()
        self.addCleanup(now.stop)
        d = self.db
        d.execute("INSERT INTO companies (id,company_name) VALUES (7,'Firma')")
        d.execute("INSERT INTO users (id,username,email) VALUES (1,'ada','a@f.dk'),(2,'hr','h@f.dk')")
        d.execute("INSERT INTO company_users (company_id,user_id,username,full_name,role,status,department) VALUES "
                  "(7,1,'ada','Ada Hansen','employee','active','Salg'),(7,2,'hr','Hanne HR','hr_manager','active','HR')")
        self.hr = client_as(self.app, user="hr", user_id=2, company_id=7, company_role="hr_manager")
        self.addCleanup(d.raw.close)

    def pending(self, oid, title, date, location="", price=12500):
        self.db.execute(
            "INSERT INTO course_orders (order_id,company_id,user_id,username,product_handle,product_title,price,variant_date,"
            "variant_location,status,department,user_name) VALUES (%s,7,1,'ada','h',%s,%s,%s,%s,'pending_approval','Salg','Ada Hansen')",
            (oid, title, price, date, location))
        self.db.execute("INSERT INTO order_approvals (order_id,company_id,requester_user_id,status) VALUES (%s,7,1,'pending')", (oid,))

    def html(self):
        resp = self.hr.get("/hr/approvals")
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_card_reads_date_location_and_price(self):
        self.pending("o1", "Excel", "2026-12-03", "Aarhus")
        self.assertIn("3. december 2026</span> · Aarhus · <span", self.html())
        self.assertIn("12.500 kr.", self.html())

    def test_a_booked_start_wins_over_the_ordered_session(self):
        self.pending("o1", "Excel", "2026-12-03", "Aarhus")
        self.db.execute("INSERT INTO course_order_details (order_id,company_id,user_id,booking_json) VALUES ('o1',7,1,%s)",
                        (json.dumps({"start_at": "2026-12-10T09:00:00+01:00"}),))
        self.assertIn("10. december 2026", self.html())

    def test_an_undated_order_says_dato_aftales(self):
        self.pending("o2", "Ledelse", "", "")
        html = self.html()
        self.assertIn("Dato aftales", html)
        self.assertNotIn("Starter om", html)

    def test_courses_starting_within_14_days_are_flagged(self):
        self.pending("o3", "Snart", "2026-11-25", "Aalborg")
        self.pending("o4", "Senere", "2026-12-24", "Aalborg")
        html = self.html()
        self.assertIn("Starter om 5 dage", html)
        self.assertEqual(html.count("Starter om"), 1)
