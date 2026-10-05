"""Concurrency proof against CI's disposable MySQL, never a production database."""

import datetime
import os
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
from tests.test_schema_baseline import _mysql_reachable


@unittest.skipUnless(os.getenv("MYSQL_DB") == "futurematch_sandbox" and _mysql_reachable(), "requires CI disposable MySQL sandbox")
class MySQLLaunchTests(unittest.TestCase):
    def test_concurrent_orders_do_not_lose_budget_updates_or_silently_overspend(self):
        import run
        import order_service as orders
        from enterprise_tables import ensure_enterprise_tables

        app = run.create_app()
        ensure_enterprise_tables(app)
        token = uuid.uuid4().hex[:12]
        with app.app_context():
            conn = app.mysql.connection
            cur = conn.cursor()
            cur.execute("INSERT INTO companies(company_name,company_slug) VALUES(%s,%s)", ("Launch regression", token))
            cid = cur.lastrowid
            users = []
            for i in range(2):
                name = "launch_" + token + "_" + str(i)
                cur.execute(
                    "INSERT INTO users(username,password,email) VALUES(%s,%s,%s)", (name, "test-only-unusable", name + "@example.invalid")
                )
                users.append((cur.lastrowid, name))
            cur.execute(
                "INSERT INTO department_budgets(company_id,department,annual_budget,spent,fiscal_year) VALUES(%s,%s,1000,0,%s)",
                (cid, "Test", datetime.date.today().year),
            )
            conn.commit()
            cur.close()
        barrier = threading.Barrier(2)

        def submit(person):
            with app.app_context():
                barrier.wait(timeout=10)
                uid, name = person
                ctx = orders.OrderContext(company_id=cid, user_id=uid, username=name, company_role="hr_manager", department="Test")
                return orders.create_order(ctx, product_handle="launch-" + token, product_title="Concurrency regression", price=600)

        try:
            with (
                mock.patch.object(orders, "_emit_event_safe"),
                mock.patch.object(orders, "_send_email_safe"),
                mock.patch.object(orders, "_send_approval_needed_emails_safe"),
                mock.patch.object(orders, "_notify_vendor_safe"),
                mock.patch.object(orders, "_vendor_id_for_handle", return_value=None),
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(submit, users))
            self.assertTrue(all(r["success"] for r in results), results)
            self.assertEqual(sorted(r["status"] for r in results), ["approved", "pending_approval"])
            with app.app_context():
                cur = app.mysql.connection.cursor()
                cur.execute("SELECT spent FROM department_budgets WHERE company_id=%s", (cid,))
                row = cur.fetchone()
                self.assertEqual(float(row["spent"] if isinstance(row, dict) else row[0]), 600)
                cur.close()
                approved = next(result for result in results if result["status"] == "approved")
                reviewer = orders.OrderContext(
                    company_id=cid, user_id=users[0][0], username=users[0][1], company_role="hr_manager", department="Test"
                )
                with mock.patch.object(orders, "_emit_event_safe"), mock.patch.object(orders, "_send_email_safe"):
                    cancellation = orders.set_status(reviewer, approved["order_id"], "cancelled")
                self.assertTrue(cancellation["success"], cancellation)
                cur = app.mysql.connection.cursor()
                cur.execute("SELECT spent FROM department_budgets WHERE company_id=%s", (cid,))
                row = cur.fetchone()
                self.assertEqual(float(row["spent"] if isinstance(row, dict) else row[0]), 0)
                cur.close()
        finally:
            with app.app_context():
                conn = app.mysql.connection
                cur = conn.cursor()
                for table in (
                    "order_approvals",
                    "order_status_history",
                    "audit_log",
                    "notifications",
                    "course_order_details",
                    "course_orders",
                    "department_budgets",
                ):
                    cur.execute("DELETE FROM " + table + " WHERE company_id=%s", (cid,))
                for uid, _ in users:
                    cur.execute("DELETE FROM users WHERE id=%s", (uid,))
                cur.execute("DELETE FROM companies WHERE id=%s", (cid,))
                conn.commit()
                cur.close()
