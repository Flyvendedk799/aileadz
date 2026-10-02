"""R-5: enterprise_analytics "Avanceret analyse" behind a flag, a capability and tenancy.

The ML engine itself is not exercised against SQL here (its queries are MySQL
dialect); ``get_company_data`` is stubbed with rows shaped like the real tables.
"""

import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

import run  # noqa: E402,F401  (installs pymysql as MySQLdb before the module import)
import enterprise_analytics as ea  # noqa: E402
from tests.sqlite_platform import PlatformDB, client_as, make_app, render_patches  # noqa: E402

URL = "/analytics/dashboard/7"


def _company_data(n_employees):
    now = datetime.now()
    employees, learning = [], []
    for i in range(n_employees):
        uid = 100 + i
        employees.append({
            "id": i + 1, "user_id": uid, "full_name": "Person Nummer%d" % i, "email": "p%d@x.dk" % i,
            "job_title": "Konsulent", "department": "Salg" if i % 2 else "IT", "role": "employee",
            "hire_date": None, "performance_rating": 3 + (i % 3) * 0.5, "total_learning_hours": i * 2,
            "courses_completed": i % 4, "last_active_at": now - timedelta(days=i * 3), "status": "active",
        })
        for j in range(i % 3 + 1):
            learning.append({
                "user_id": uid, "status": "completed" if j % 2 == 0 else "in_progress",
                "progress_percentage": 50 + j * 20, "time_spent_minutes": 30 * (j + 1),
                "attempts_count": 1, "final_score": 70, "content_name": "Kursus %d" % j,
            })
    return {"employees": employees, "learning": learning, "performance": [], "goals": [], "analytics": []}


class Base(unittest.TestCase):
    def setUp(self):
        self.db = PlatformDB()
        self.app = make_app(self.db)
        for p in (render_patches(), mock.patch.dict(os.environ, {"ADVANCED_ANALYTICS_ENABLED": "1"})):
            p.start()
            self.addCleanup(p.stop)
        self.db.execute("INSERT INTO companies (id, company_name, company_slug) VALUES (7, 'Firma', 'firma'), "
                        "(8, 'Andet', 'andet')")
        self.db.execute("INSERT INTO users (id, username, email, role) VALUES (1,'root','r@x.dk','admin'), "
                        "(3,'hr','hr@x.dk','user'), (5,'emp','e@x.dk','user'), (6,'dh','dh@x.dk','user')")
        self.db.execute("INSERT INTO company_users (company_id, user_id, username, role, status) VALUES "
                        "(7, 3, 'hr', 'hr_manager', 'active'), (7, 5, 'emp', 'employee', 'active'), "
                        "(7, 6, 'dh', 'department_head', 'active')")
        self.hr = client_as(self.app, user="hr", user_id=3, company_id=7, company_role="hr_manager")
        self.emp = client_as(self.app, user="emp", user_id=5, company_id=7, company_role="employee")
        self.dh = client_as(self.app, user="dh", user_id=6, company_id=7, company_role="department_head")
        self.admin = client_as(self.app, user="root", user_id=1, role="admin")

    def stub_data(self, n):
        p = mock.patch.object(ea.analytics_engine, "get_company_data", return_value=_company_data(n))
        p.start()
        self.addCleanup(p.stop)


class GuardTests(Base):
    def test_flag_off_hides_the_page_and_the_link(self):
        self.stub_data(8)
        with mock.patch.dict(os.environ, {"ADVANCED_ANALYTICS_ENABLED": ""}):
            self.assertEqual(self.hr.get(URL).status_code, 302)
            r = self.hr.get("/analytics/api/engagement-trends/7")
            self.assertEqual(r.status_code, 404)
            self.assertEqual(r.get_json()["error"], "Avanceret analyse er ikke slået til.")
            with self.app.test_request_context("/"):
                from flask import session
                session.update({"user": "hr", "user_id": 3, "company_id": 7, "company_role": "hr_manager"})
                self.assertIsNone(ea.advanced_analytics_url())

    def test_hr_manager_sees_the_dashboard_and_gets_the_link(self):
        self.stub_data(8)
        r = self.hr.get(URL)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Avanceret analyse", r.get_data(as_text=True))
        with self.app.test_request_context("/"):
            from flask import session
            session.update({"user": "hr", "user_id": 3, "company_id": 7, "company_role": "hr_manager"})
            self.assertEqual(ea.advanced_analytics_url(), URL)

    def test_employee_and_department_head_are_refused(self):
        self.stub_data(8)
        for c in (self.emp, self.dh):
            self.assertEqual(c.get(URL).status_code, 302)
            self.assertEqual(c.get("/analytics/api/learning-roi/7").status_code, 403)
            self.assertEqual(c.get("/analytics/employee/1/recommendations").status_code, 403)

    def test_another_companys_id_is_refused(self):
        self.stub_data(8)
        self.assertEqual(self.hr.get("/analytics/dashboard/8").status_code, 302)
        r = self.hr.get("/analytics/export/8")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["error"], "Du har ikke adgang til denne virksomheds data.")
        ea.analytics_engine.get_company_data.assert_not_called()

    def test_platform_admin_may_open_any_company(self):
        self.stub_data(8)
        self.assertEqual(self.admin.get("/analytics/dashboard/8").status_code, 200)

    def test_anonymous_goes_to_login(self):
        r = self.app.test_client().get(URL)
        self.assertIn("/login", r.headers["Location"])


class PrivacyTests(Base):
    def test_dashboard_shows_aggregates_not_named_employees(self):
        self.stub_data(9)
        html = self.hr.get(URL).get_data(as_text=True)
        self.assertIn("Engagement pr. niveau", html)
        self.assertIn('class="an-eng ', html)        # the aggregate table rendered
        self.assertIn("engagement_pie", html)         # chart payload reached the page
        self.assertNotIn("Person Nummer", html)
        self.assertNotIn("Employee Engagement", html)

    def test_engagement_is_suppressed_below_k(self):
        import kanon
        self.stub_data(max(kanon.K_DEFAULT - 1, 1))
        html = self.hr.get(URL).get_data(as_text=True)
        self.assertIn(kanon.anon_note(), html)
        self.assertNotIn("an-eng High", html)

    def test_small_departments_are_left_out_of_the_hours_chart(self):
        import kanon
        k = kanon.K_DEFAULT
        data = _company_data(k + 1)
        for e in data["employees"]:
            e["department"] = "Stor"
        data["employees"][0]["department"] = "Lille"
        charts = ea.create_analytics_charts(data, [], None)
        self.assertNotIn("Lille", charts["dept_learning_hours"])
        self.assertIn("Stor", charts["dept_learning_hours"])
        self.assertTrue(charts["_dept_anon"])


class LearningAnalyticsLinkTests(unittest.TestCase):
    def test_link_renders_only_when_the_global_returns_a_url(self):
        from tests.test_learning_analytics_render import _render
        self.assertNotIn("advanced-analytics-link", _render())
        html = _render(advanced_analytics_url=lambda: "/analytics/dashboard/7")
        self.assertIn('href="/analytics/dashboard/7"', html)
        self.assertIn("Avanceret", html)


if __name__ == "__main__":
    unittest.main()
