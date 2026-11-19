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


@unittest.skipUnless(os.getenv("MYSQL_DB") == "futurematch_sandbox" and _mysql_reachable(), "requires CI disposable MySQL sandbox")
class LegacyDataMigrationTests(unittest.TestCase):
    """The one-time data migrations (schema_registry.run_data_migrations and the
    plaintext-password rehash) against legacy-shaped rows on real MySQL. Before
    this test they had only ever run on the SQLite harness."""

    LEGACY = (  # status, payment_status, completion_status -> expected status, billing_status
        ("pending", None, None, "approved", "not_invoiced"),
        ("confirmed", None, None, "booked", "not_invoiced"),
        ("processing", None, None, "booked", "not_invoiced"),
        ("invoiced", None, None, "booked", "invoiced"),
        ("invoiced", None, "completed", "completed", "invoiced"),
        ("paid", None, None, "booked", "paid"),
        ("confirmed", "paid", None, "booked", "paid"),
        ("pending_approval", None, None, "pending_approval", "not_invoiced"),   # already canonical: untouched
        ("cancelled", None, None, "cancelled", "not_invoiced"),
    )

    def test_legacy_rows_are_migrated_and_a_second_run_changes_nothing(self):
        import run
        import schema_registry
        from auth import rehash_plaintext_passwords
        from enterprise_tables import ensure_enterprise_tables
        from werkzeug.security import check_password_hash

        app = run.create_app()
        ensure_enterprise_tables(app)
        token = uuid.uuid4().hex[:10]
        flags = ("order_lifecycle_v1", "variant_date_human_v1", "notifications_unified_v1")
        with app.app_context():
            conn = app.mysql.connection
            cur = conn.cursor()
            cur.execute("INSERT INTO companies(company_name,company_slug) VALUES(%s,%s)", ("Legacy " + token, "legacy-" + token))
            cid = cur.lastrowid
            name = "legacy_" + token
            cur.execute("INSERT INTO users(username,password,email) VALUES(%s,%s,%s)", (name, "Sommer2019!", name + "@example.invalid"))
            uid = cur.lastrowid
            cur.execute("INSERT INTO company_users(company_id,user_id,username,role,status) VALUES(%s,%s,%s,'hr_manager','active')", (cid, uid, name))
            ids = []
            for i, (status, payment, completion, _s, _b) in enumerate(self.LEGACY):
                oid = "%s-%d" % (token, i)
                ids.append(oid)
                cur.execute(
                    "INSERT INTO course_orders(order_id,company_id,user_id,username,product_handle,product_title,price,"
                    "status,payment_status,completion_status,billing_status,billing_notes,variant_date) "
                    "VALUES(%s,%s,%s,%s,'h','Legacy kursus',1000,%s,%s,%s,'not_invoiced',%s,%s)",
                    (oid, cid, uid, name, status, payment, completion, "Faktura 4711" if i == 3 else None,
                     "2026-12-03T09:00:00+01:00" if i == 1 else "3. december 2026"))
            cur.execute("INSERT INTO company_notifications(company_id,target_roles,title,message,is_urgent,is_read) "
                        "VALUES(%s,%s,%s,'Gammel besked',0,0)", (cid, '["hr_manager"]', "Legacy " + token))
            # A database that has never run the migrations has no flags.
            cur.execute("DELETE FROM schema_meta WHERE meta_key IN (%s,%s,%s)", flags)
            conn.commit()

            try:
                ran = schema_registry.run_data_migrations(conn)
                for flag in flags:
                    self.assertIn(flag, ran)
                cur.execute("SELECT order_id,status,billing_status,billing_note,variant_date FROM course_orders WHERE company_id=%s", (cid,))
                rows = {r["order_id"] if isinstance(r, dict) else r[0]: r for r in cur.fetchall()}

                def col(row, key, idx):
                    return row[key] if isinstance(row, dict) else row[idx]
                for oid, (_st, _p, _c, status, billing) in zip(ids, self.LEGACY):
                    self.assertEqual((col(rows[oid], "status", 1), col(rows[oid], "billing_status", 2)), (status, billing), oid)
                self.assertEqual(col(rows[ids[3]], "billing_note", 3), "Faktura 4711")
                self.assertEqual(col(rows[ids[1]], "variant_date", 4), "3. december 2026")
                cur.execute("SELECT COUNT(*) AS n FROM course_orders WHERE status NOT IN "
                            "('pending_approval','approved','booked','completed','rejected','cancelled')")
                left = cur.fetchone()
                self.assertEqual(left["n"] if isinstance(left, dict) else left[0], 0, "no legacy status may survive")
                cur.execute("SELECT title, `read` FROM notifications WHERE company_id=%s AND user_id=%s", (cid, name))
                cards = cur.fetchall()
                self.assertEqual(len(cards), 1, "the role broadcast becomes one card for the HR manager")

                self.assertGreaterEqual(rehash_plaintext_passwords(conn), 1)
                cur.execute("SELECT password FROM users WHERE id=%s", (uid,))
                stored = cur.fetchone()
                stored = stored["password"] if isinstance(stored, dict) else stored[0]
                self.assertNotEqual(stored, "Sommer2019!")
                self.assertTrue(check_password_hash(stored, "Sommer2019!"), "the person keeps their password")

                # Idempotent: nothing reruns, nothing is duplicated.
                self.assertEqual([k for k in schema_registry.run_data_migrations(conn) if k in flags], [])
                self.assertEqual(rehash_plaintext_passwords(conn), 0)
                cur.execute("SELECT COUNT(*) AS n FROM notifications WHERE company_id=%s", (cid,))
                again = cur.fetchone()
                self.assertEqual(again["n"] if isinstance(again, dict) else again[0], 1)
            finally:
                for table, column in (("course_orders", "company_id"), ("notifications", "company_id"),
                                      ("company_notifications", "company_id"), ("company_users", "company_id")):
                    cur.execute("DELETE FROM %s WHERE %s=%%s" % (table, column), (cid,))
                cur.execute("DELETE FROM users WHERE id=%s", (uid,))
                cur.execute("DELETE FROM companies WHERE id=%s", (cid,))
                for flag in flags:
                    cur.execute("INSERT IGNORE INTO schema_meta(meta_key,meta_value) VALUES(%s,'done')", (flag,))
                conn.commit()
                cur.close()
