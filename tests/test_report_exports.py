"""N-4.1 exports (BOM, Danish headers, filters) and N-4.2 scheduled reports."""

import datetime
import os
import unittest

os.environ.setdefault("SANDBOX", "1")

import report_exports as rx  # noqa: E402
import scheduled_reports as sr  # noqa: E402
from tests.sqlite_mysql import SqliteMysql  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.db = SqliteMysql()
        d = self.db
        d.raw.executescript("""
          CREATE TABLE compliance_requirements (id INTEGER PRIMARY KEY, company_id INTEGER, title TEXT, category TEXT,
            applies_to_department TEXT, recurrence_months INTEGER, is_statutory INTEGER);
          CREATE TABLE company_report_schedules (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, report_type TEXT,
            cadence TEXT, department TEXT, created_by INTEGER, enabled INTEGER DEFAULT 1, last_sent_at TEXT, last_status TEXT);
        """)
        d.execute("INSERT INTO users (id, username, email) VALUES (1,'ada','ada@f.dk'),(2,'hr','hr@f.dk')")
        d.execute("INSERT INTO company_users (company_id,user_id,username,email,role,department,status) VALUES "
                  "(7,1,'ada','ada@f.dk','employee','Salg','active'),(7,2,'hr','hr@f.dk','hr_manager','HR','active')")
        d.execute("INSERT INTO course_orders (order_id,company_id,user_id,username,product_title,price,status,department,completion_status) "
                  "VALUES ('o1',7,1,'ada','Ledelse æøå',1500,'completed','Salg','completed')")
        d.execute("INSERT INTO department_budgets (company_id,department,annual_budget,spent,fiscal_year) VALUES (7,'Salg',10000,1500,2026)")
        d.execute("INSERT INTO order_approvals (order_id,company_id,requester_user_id,status) VALUES ('o1',7,1,'approved')")
        self.cur = d.connection.cursor()


class ExportTests(Base):
    def test_csv_has_bom_danish_headers_and_keeps_aeoeaa(self):
        h, rows = rx.build(self.cur, 7, "course_completions")
        body = rx.to_csv(h, rows)
        self.assertTrue(body.startswith("﻿"))
        self.assertIn("Medarbejder;Afdeling;Kursus", body)
        self.assertIn("Ledelse æøå", body)
        self.assertIn("Gennemført", body)          # order status label in Danish

    def test_budget_and_approvals_reports(self):
        h, rows = rx.build(self.cur, 7, "budget")
        self.assertIn("Tilbage (kr)", h)
        self.assertEqual(rows[0][h.index("Tilbage (kr)")], 8500)
        h2, rows2 = rx.build(self.cur, 7, "approvals")
        self.assertEqual(rows2[0][h2.index("Beslutning")], "Godkendt")

    def test_filters_and_company_scope(self):
        self.assertEqual(rx.build(self.cur, 7, "course_completions", department="Drift"), ([], []))
        self.assertEqual(rx.build(self.cur, 8, "course_completions"), ([], []))     # other tenant
        self.assertEqual(rx.build(self.cur, 7, "course_completions", date_from="2999-01-01"), ([], []))

    def test_scheduler_alias_and_unknown_type(self):
        self.assertEqual(rx.resolve("training_status"), "employee_progress")
        self.assertIsNone(rx.resolve("drop table"))
        with self.assertRaises(ValueError):
            rx.build(self.cur, 7, "nope")


class ScheduledReportTests(Base):
    def test_due_logic(self):
        now = datetime.datetime(2026, 10, 10, 12, 0)
        self.assertTrue(sr.is_due("weekly", None, now))
        self.assertFalse(sr.is_due("weekly", now - datetime.timedelta(days=3), now))
        self.assertTrue(sr.is_due("weekly", now - datetime.timedelta(days=7), now))
        self.assertTrue(sr.is_due("daily", now - datetime.timedelta(hours=23, minutes=30), now))
        self.assertFalse(sr.is_due("yearly", None, now))

    def _schedule(self, enabled=1, rt="budget", cadence="weekly"):
        self.db.execute("INSERT INTO company_report_schedules (company_id, report_type, cadence, enabled) VALUES (7,%s,%s,%s)",
                        (rt, cadence, enabled))

    def test_due_schedule_is_emailed_to_hr_only_and_stamped(self):
        self._schedule()
        sent = []
        out = sr.run_due_schedules(self.cur, send=lambda to, title, s, body, n: sent.append((to, title, n, body)))
        self.assertEqual(out["sent"], 1)
        self.assertEqual([s[0] for s in sent], ["hr@f.dk"])           # not the employee
        self.assertTrue(sent[0][3].startswith("﻿"))
        row = self.db.one("SELECT last_sent_at, last_status FROM company_report_schedules")
        self.assertEqual(row["last_status"], "sent")
        again = sr.run_due_schedules(self.cur, send=lambda *a: sent.append(a))
        self.assertEqual(again["sent"], 0)                             # not due again yet

    def test_paused_schedule_sends_nothing(self):
        self._schedule(enabled=0)
        sent = []
        out = sr.run_due_schedules(self.cur, send=lambda *a: sent.append(a))
        self.assertEqual((out["checked"], sent), (0, []))

    def test_empty_report_is_not_mailed(self):
        self._schedule(rt="skill_gaps")
        sent = []
        out = sr.run_due_schedules(self.cur, send=lambda *a: sent.append(a))
        self.assertEqual((out["empty"], sent), (1, []))


class RoutePermissionTests(unittest.TestCase):
    def test_employee_cannot_export_or_schedule(self):
        import run
        client = run.create_app().test_client()
        with client.session_transaction() as s:
            s["user"] = "ada"; s["company_id"] = 7; s["company_role"] = "employee"
        self.assertEqual(client.get("/hr/export/budget").status_code, 302)
        self.assertEqual(client.post("/hr/reports/schedules/add", data={"report_type": "budget", "cadence": "weekly"}).status_code, 302)
        self.assertEqual(client.post("/hr/reports/schedules/1/cancel").status_code, 302)


if __name__ == "__main__":
    unittest.main()
