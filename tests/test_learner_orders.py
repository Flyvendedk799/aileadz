"""N-1.2 / N-1.3 / N-1.4 on the web layer: learner order detail, actions, permissions."""

import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")
os.environ.setdefault("AI_WARMUP_ON_IMPORT", "0")
os.environ.setdefault("SCHEDULER_OPPORTUNISTIC", "0")

import order_service as svc  # noqa: E402
import run  # noqa: E402
from tests.sqlite_mysql import SqliteMysql  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.db = SqliteMysql()
        for uid, name in ((1, "ada"), (2, "bo"), (3, "hr")):
            self.db.execute("INSERT INTO users (id, username, email) VALUES (%s, %s, %s)", (uid, name, name + "@f.dk"))
        self.db.execute("INSERT INTO companies (id, company_name) VALUES (7, 'Firma')")
        self.db.execute("INSERT INTO company_users (company_id, user_id, username, role, department, manager_user_id) VALUES "
                        "(7, 1, 'ada', 'employee', 'Salg', NULL), (7, 2, 'bo', 'employee', 'Salg', NULL), "
                        "(7, 3, 'hr', 'hr_manager', 'HR', NULL)")
        self.app = run.create_app()
        self.app.config["TESTING"] = True
        self.app.mysql = self.db
        for p in (
            mock.patch("white_label_global_integration.get_template_context", return_value={}),
            mock.patch.object(svc, "_emit_event_safe"),
            mock.patch.object(svc, "_send_email_safe"),
            mock.patch.object(svc, "_vendor_id_for_handle", return_value=None),
            mock.patch.object(svc, "_notify_vendor_safe"),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.db.execute(
            "INSERT INTO course_orders (order_id, company_id, user_id, username, product_handle, product_title, price, "
            "variant_date, variant_location, status, department, user_email) VALUES "
            "('ord-1', 7, 1, 'ada', 'prince2', 'PRINCE2', 2500, '2026-11-03', 'København', 'booked', 'Salg', 'ada@f.dk')")

    def client_as(self, user, uid, role="employee"):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s.update(user=user, user_id=uid, company_id=7, company_role=role)
        return c


class DetailPageTests(Base):
    def test_owner_sees_status_and_actions(self):
        html = self.client_as("ada", 1).get("/min-ordre/ord-1").get_data(as_text=True)
        self.assertIn("Booket", html)
        self.assertIn("Annullér", html)
        self.assertIn("Markér som gennemført", html)
        self.assertIn("Tilføj til kalender", html)
        self.assertIn("Faktureres eksternt", html)

    def test_colleague_gets_404_without_a_hint(self):
        self.assertEqual(self.client_as("bo", 2).get("/min-ordre/ord-1").status_code, 404)

    def test_manager_is_sent_to_the_hr_page(self):
        resp = self.client_as("hr", 3, "hr_manager").get("/min-ordre/ord-1")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/hr/order/ord-1/details", resp.headers["Location"])

    def test_anonymous_is_sent_to_login(self):
        resp = self.app.test_client().get("/min-ordre/ord-1")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_timeline_rows_link_to_the_detail_page(self):
        html = self.client_as("ada", 1).get("/min-tidslinje").get_data(as_text=True)
        self.assertIn('href="/min-ordre/ord-1"', html)
        self.assertIn("Booket", html)
        self.assertNotIn("Afventer betaling", html)


class ActionTests(Base):
    def test_cancel_by_owner_and_refund(self):
        self.db.execute("INSERT INTO department_budgets (company_id, department, annual_budget, spent, fiscal_year) "
                        "VALUES (7, 'Salg', 10000, 2500, %s)", (svc.datetime.datetime.now().year,))
        self.db.execute("UPDATE course_orders SET budget_charged = 1 WHERE order_id='ord-1'")
        resp = self.client_as("ada", 1).post("/min-ordre/ord-1/annuller", data={"reason": "Syg"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.db.one("SELECT status FROM course_orders")["status"], "cancelled")
        self.assertEqual(self.db.one("SELECT spent FROM department_budgets")["spent"], 0)

    def test_colleague_cannot_cancel(self):
        resp = self.client_as("bo", 2).post("/min-ordre/ord-1/annuller", headers={"Accept": "application/json"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.db.one("SELECT status FROM course_orders")["status"], "booked")

    def test_complete_then_completion_moment_and_skill_save(self):
        c = self.client_as("ada", 1)
        with mock.patch("completion_service.completion_moment",
                        return_value={"skill_proposals": [{"name": "Projektledelse", "level": "mellem", "why": "x"}],
                                      "next_steps": [], "review_url": "/products/prince2#reviews",
                                      "course_title": "PRINCE2"}):
            resp = c.post("/min-ordre/ord-1/gennemfoert")
            self.assertEqual(resp.status_code, 302)
            self.assertIn("completed=1", resp.headers["Location"])
            html = c.get("/min-ordre/ord-1?completed=1").get_data(as_text=True)
        self.assertIn("Gennemført", html)
        self.assertIn("Det tager du med dig", html)
        self.assertIn("Projektledelse", html)
        self.assertIn("Giv en vurdering", html)
        self.assertIn("Tal med AI", html)
        with mock.patch("app1.user_profile_db.add_skill", return_value=True) as add, \
                mock.patch("skill_history.record_user_snapshot", return_value=True) as snap:
            saved = c.post("/min-ordre/ord-1/kompetencer", json={"skills": [{"name": "Projektledelse", "level": "avanceret"}]})
        self.assertEqual(saved.get_json()["saved"], 1)
        add.assert_called_once()
        self.assertEqual(snap.call_args.kwargs["source"], "post_course")

    def test_cannot_save_skills_before_completion_or_for_someone_elses_order(self):
        self.assertEqual(self.client_as("ada", 1).post("/min-ordre/ord-1/kompetencer",
                                                       json={"skills": [{"name": "X"}]}).status_code, 400)
        self.assertEqual(self.client_as("bo", 2).post("/min-ordre/ord-1/kompetencer",
                                                      json={"skills": [{"name": "X"}]}).status_code, 404)


class CalendarTests(Base):
    def test_owner_gets_an_ics_for_the_order(self):
        resp = self.client_as("ada", 1).get("/min-ordre/ord-1/kalender.ics")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/calendar", resp.headers["Content-Type"])
        self.assertIn("PRINCE2", resp.get_data(as_text=True))

    def test_colleague_cannot_fetch_it(self):
        self.assertEqual(self.client_as("bo", 2).get("/min-ordre/ord-1/kalender.ics").status_code, 404)

    def test_personal_calendar_only_contains_my_orders(self):
        self.db.execute(
            "INSERT INTO course_orders (order_id, company_id, user_id, username, product_handle, product_title, variant_date, status) "
            "VALUES ('ord-2', 7, 2, 'bo', 'h', 'BOS KURSUS', '2026-12-01', 'booked')")
        body = self.client_as("ada", 1).get("/min-kalender.ics").get_data(as_text=True)
        self.assertIn("PRINCE2", body)
        self.assertNotIn("BOS KURSUS", body)


if __name__ == "__main__":
    unittest.main()
