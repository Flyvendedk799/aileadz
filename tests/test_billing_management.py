"""N-6.3: billing management (off-platform): filters, overdue, CSV, notifications."""

import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

from flask import Flask  # noqa: E402

import billing_service  # noqa: E402
import order_service as svc  # noqa: E402
from tests.sqlite_mysql import SqliteMysql  # noqa: E402


class BillingServiceTests(unittest.TestCase):
    def setUp(self):
        self.db = SqliteMysql()
        self.app = Flask(__name__)
        self.app.mysql = self.db
        self.cm = self.app.app_context()
        self.cm.push()
        self.addCleanup(self.cm.pop)
        d = self.db
        d.execute("INSERT INTO companies (id, company_name) VALUES (7, 'Firma'), (8, 'Andet')")
        d.execute("INSERT INTO users (id, username) VALUES (1, 'ada'), (2, 'hr')")
        d.execute("INSERT INTO company_users (company_id, user_id, username, role, status) VALUES (7, 2, 'hr', 'hr_manager', 'active')")

        def order(oid, cid, status, billing, price, due=None, inv=None):
            d.execute("INSERT INTO course_orders (order_id, company_id, username, product_title, price, status, "
                      "billing_status, invoice_due_date, invoice_number, department) VALUES (%s,%s,'ada','Kursus',%s,%s,%s,%s,%s,'Salg')",
                      (oid, cid, price, status, billing, due, inv))
        order("o1", 7, "approved", "not_invoiced", 1000)
        order("o2", 7, "booked", "invoiced", 2000, due="2020-01-01", inv="F-2")      # overdue
        order("o3", 7, "completed", "invoiced", 3000, due="2999-01-01", inv="F-3")   # not overdue
        order("o4", 7, "completed", "paid", 4000)
        order("o5", 7, "cancelled", "not_invoiced", 500)                             # excluded
        order("o6", 7, "pending_approval", "not_invoiced", 600)                      # excluded (not billable yet)
        order("o7", 8, "approved", "not_invoiced", 900)                              # other company
        order("o8", None, "approved", "not_invoiced", 700)                           # solo user

    def cur(self):
        return self.db.connection.cursor()

    def test_company_scope_and_exclusions(self):
        rows = billing_service.fetch_orders(self.cur(), company_id=7)
        self.assertEqual({r["order_id"] for r in rows}, {"o1", "o2", "o3", "o4"})

    def test_summary_splits_statuses_and_overdue(self):
        s = billing_service.summary(self.cur(), company_id=7)
        self.assertEqual((s["not_invoiced"], s["invoiced"], s["paid"]), (1, 2, 1))
        self.assertEqual(s["overdue"], 1)
        self.assertEqual(s["overdue_value"], 2000)
        self.assertEqual(s["total_value"], 10000)

    def test_filters(self):
        ids = lambda f: {r["order_id"] for r in billing_service.fetch_orders(self.cur(), company_id=7, billing_filter=f)}
        self.assertEqual(ids("overdue"), {"o2"})
        self.assertEqual(ids("unpaid"), {"o1", "o2", "o3"})
        self.assertEqual(ids("paid"), {"o4"})

    def test_solo_queue_for_admin(self):
        rows = billing_service.fetch_orders(self.cur(), solo_only=True)
        self.assertEqual([r["order_id"] for r in rows], ["o8"])

    def test_csv_has_bom_and_excel_friendly_decimals(self):
        rows = billing_service.fetch_orders(self.cur(), company_id=7)
        body = billing_service.to_csv(rows, {7: "Firma"})
        self.assertTrue(body.startswith("﻿"))
        self.assertIn("Fakturanr.", body.splitlines()[0])
        self.assertIn("2000,00", body)
        self.assertIn("Firma", body)

    def test_overdue_notification_is_deduped_per_order(self):
        cur = self.cur()
        self.assertEqual(billing_service.notify_overdue(cur), 1)
        self.assertEqual(billing_service.notify_overdue(cur), 0)
        n = self.db.one("SELECT user_id, dedupe_key, action_url FROM notifications")
        self.assertEqual((n["user_id"], n["dedupe_key"]), ("hr", "billing-overdue:o2"))
        self.assertIn("overdue", n["action_url"])

    def test_learner_cannot_change_billing_but_hr_can(self):
        learner = svc.OrderContext(company_id=7, user_id=1, username="ada", company_role="employee")
        hr = svc.OrderContext(company_id=7, user_id=2, username="hr", company_role="hr_manager")
        with mock.patch.object(svc, "_emit_event_safe"):
            self.assertEqual(svc.set_billing_status(learner, "o1", "invoiced", invoice_number="X")["error"], "not_found")
            self.assertTrue(svc.set_billing_status(hr, "o1", "invoiced", invoice_number="F-1", due_date="2026-12-01")["success"])
        self.assertEqual(self.db.one("SELECT billing_status FROM course_orders WHERE order_id='o1'")["billing_status"], "invoiced")


class BillingRoutePermissionTests(unittest.TestCase):
    def test_admin_billing_requires_platform_admin(self):
        import run
        client = run.create_app().test_client()
        with client.session_transaction() as s:
            s["user"] = "hr"; s["role"] = "user"; s["company_role"] = "hr_manager"
        r = client.get("/admin/billing")
        self.assertIn(r.status_code, (302, 401, 403))
        self.assertNotIn(b"Alle virksomheder", r.data)

    def test_hr_csv_requires_hr_access(self):
        import run
        client = run.create_app().test_client()
        with client.session_transaction() as s:
            s["user"] = "emp"; s["company_id"] = 7; s["company_role"] = "employee"
        r = client.get("/hr/billing/export.csv")
        self.assertEqual(r.status_code, 302)


if __name__ == "__main__":
    unittest.main()
