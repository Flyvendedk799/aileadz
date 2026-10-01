"""N-2.2: first run per role, /dashboard routing, empty states on the learner home."""

import datetime
import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")
os.environ.setdefault("AI_WARMUP_ON_IMPORT", "0")
os.environ.setdefault("SCHEDULER_OPPORTUNISTIC", "0")

import run  # noqa: E402
from tests.sqlite_mysql import SqliteMysql  # noqa: E402

SCHEMA_EXTRA = "ALTER TABLE users ADD COLUMN first_login_completed INTEGER DEFAULT 0"


def _client(db, **session_values):
    app = run.create_app()
    app.config["TESTING"] = True
    app.mysql = db
    client = app.test_client()
    with client.session_transaction() as s:
        s.update(session_values)
    return client


class DashboardRoutingTests(unittest.TestCase):
    def test_anonymous_goes_to_login(self):
        client = _client(SqliteMysql())
        resp = client.get("/dashboard")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_employee_lands_on_min_laering(self):
        client = _client(SqliteMysql(), user="ada", user_id=1, company_id=7, company_role="employee")
        resp = client.get("/dashboard")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/min-laering"))

    def test_solo_user_lands_on_min_laering(self):
        client = _client(SqliteMysql(), user="solo", user_id=2)
        self.assertTrue(client.get("/dashboard").headers["Location"].endswith("/min-laering"))

    def test_manager_gets_the_workspace_overview(self):
        client = _client(SqliteMysql(), user="hr", user_id=3, company_id=7, company_role="hr_manager")
        with mock.patch("white_label_global_integration.get_template_context", return_value={}):
            resp = client.get("/dashboard")
        self.assertEqual(resp.status_code, 200)


class WelcomeCardTests(unittest.TestCase):
    def setUp(self):
        self.db = SqliteMysql()
        self.db.execute("INSERT INTO users (id, username, email) VALUES (1, 'ada', 'ada@x.dk')")
        self.client = _client(self.db, user="ada", user_id=1, company_id=7, company_role="employee")
        self.patches = [
            mock.patch("white_label_global_integration.get_template_context", return_value={}),
            mock.patch("futurematch_ui._home_recommendations", return_value=[]),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _home(self):
        return self.client.get("/min-laering").get_data(as_text=True)

    def test_new_learner_sees_the_welcome_entry_points_not_a_checklist(self):
        html = self._home()
        self.assertIn("Vælg, hvor du vil starte", html)
        self.assertIn("Fortæl AI'en om dig", html)
        self.assertIn("Upload dit CV", html)
        self.assertIn("Udforsk kataloget", html)
        self.assertNotIn("felter", html.lower().split("vælg, hvor du vil starte")[1][:600])

    def test_dismissing_records_first_login_completed(self):
        resp = self.client.post("/min-laering/velkommen/luk")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.db.one("SELECT first_login_completed AS f FROM users WHERE id=1")["f"], 1)
        self.assertNotIn("Vælg, hvor du vil starte", self._home())

    def test_recommendations_empty_state_is_honest_with_a_next_step(self):
        html = self._home()
        self.assertIn("Vi har ingen anbefalinger til dig endnu", html)
        self.assertNotIn("fm-skel", html)        # no permanent skeleton

    def test_goals_and_deadlines_cards_have_empty_states(self):
        html = self._home()
        self.assertIn("Mål og frister", html)
        self.assertIn("Sæt dit første mål", html)
        self.assertIn("Ingen frister lige nu", html)

    def test_upcoming_deadline_shows_with_link_to_the_order(self):
        soon = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
        self.db.execute(
            "INSERT INTO course_orders (order_id, user_id, username, product_handle, product_title, status, "
            "completion_deadline) VALUES ('o-1', 1, 'ada', 'h', 'PRINCE2', 'booked', %s)", (soon,))
        html = self._home()
        self.assertIn("/min-ordre/o-1", html)
        self.assertIn("PRINCE2", html)

    def test_dismiss_requires_login(self):
        anon = _client(SqliteMysql())
        resp = anon.post("/min-laering/velkommen/luk")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])


class HrOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.db = SqliteMysql()
        self.db.execute("INSERT INTO companies (id, company_name) VALUES (7, 'Firma')")
        self.db.execute("INSERT INTO users (id, username, email) VALUES (2, 'hr', 'hr@f.dk')")
        self.db.execute("INSERT INTO company_users (company_id, user_id, username, role, status) VALUES (7, 2, 'hr', 'hr_manager', 'active')")

    def test_hr_can_dismiss_the_checklist_per_company(self):
        c = _client(self.db, user="hr", user_id=2, company_id=7, company_role="hr_manager")
        resp = c.post("/hr/onboarding/dismiss")
        self.assertEqual(resp.status_code, 302)
        row = self.db.one("SELECT meta_value FROM schema_meta WHERE meta_key = 'hr_onboarding_dismissed:7'")
        self.assertEqual(row["meta_value"], "dismissed")

    def test_employee_cannot_dismiss_it(self):
        c = _client(self.db, user="ada", user_id=1, company_id=7, company_role="employee")
        c.post("/hr/onboarding/dismiss")
        self.assertIsNone(self.db.one("SELECT 1 FROM schema_meta WHERE meta_key LIKE 'hr_onboarding%'"))

    def test_template_renders_the_checklist_until_dismissed(self):
        import jinja2, os as _os
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(_os.path.join(_os.path.dirname(__file__), "..", "templates")))
        src = env.loader.get_source(env, "fm/hr.html")[0]
        self.assertIn("Kom godt i gang", src)
        self.assertIn("not onboarding_dismissed", src)


if __name__ == "__main__":
    unittest.main()
