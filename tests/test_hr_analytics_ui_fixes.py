"""Regression tests for the HR analytics / overview UI audit.

Covers behaviour fixes only (offline, FakeMySQL, no network):
  * skill-gaps page survives the k-anon '_anon_note' key in the heatmap
  * skill-target save: "Alle afdelinger" (NULL department) is updated, not duplicated
  * skill assign + engagement nudge work with DictCursor rows (the app-wide cursor class)
  * learning-analytics KPI totals (Jinja loop scoping) and AI-usage block cursor
  * dashboard: role label, credits tile matches the header chip, KPI tiles are links
  * Danish number/date template filters
  * CSV export tolerates a company without a slug and returns to the referring page
"""
import datetime
import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

from tests.secapp import client_as, get_app, patch_mysql  # noqa: E402


COMPANY = {"id": 7, "name": "Demo", "company_name": "Demo", "company_slug": None,
           "user_role": "hr_manager", "department": None, "permissions": None,
           "status": "active", "plan": "pro"}


def _responder(extra=None):
    def r(sql, params):
        s = " ".join(str(sql).split()).lower()
        if "from companies c join company_users" in s:
            return dict(COMPANY)
        if extra:
            return extra(s, params)
        return None
    return r


class DanishFilterTests(unittest.TestCase):
    def setUp(self):
        self.env = get_app().jinja_env

    def test_dknum(self):
        f = self.env.filters["dknum"]
        self.assertEqual(f(1234567), "1.234.567")
        self.assertEqual(f(12.5), "12,5")
        self.assertEqual(f(100.0), "100")
        self.assertEqual(f(1234.567, 2), "1.234,57")
        self.assertEqual(f(None), None)
        self.assertEqual(f("n/a"), "n/a")

    def test_dkdate(self):
        f = self.env.filters["dkdate"]
        self.assertEqual(f("2026-10-01T08:30:00.123456"), "01.10.2026")
        self.assertEqual(f("2026-10-01T08:30:00", True), "01.10.2026 08:30")
        self.assertEqual(f(datetime.date(2026, 3, 4)), "04.03.2026")
        self.assertEqual(f(None), "")
        self.assertEqual(f("ikke en dato"), "ikke en dato")


class SkillGapsTests(unittest.TestCase):
    def test_page_renders_when_heatmap_carries_anon_note(self):
        app = get_app()
        heat = {"_anon_note": "Afdelinger med få medarbejdere er skjult.",
                "Salg": {"Python": {"target_level": 4, "priority": "high", "employees": 6,
                                    "avg_current": 2.0, "gap": 2.0, "status": "red"}}}
        with patch_mysql(app, _responder())[1], \
                mock.patch("insights_engine.get_skill_gap_analysis", return_value=heat):
            resp = client_as(app, "hr_manager").get("/hr/skill-gaps")
        self.assertEqual(resp.status_code, 200, resp.data[:300])
        html = resp.get_data(as_text=True)
        self.assertIn("Afdelinger med få medarbejdere er skjult.", html)
        self.assertIn("Kompetencegab under målniveau", html)

    def test_save_all_departments_target_updates_existing_row(self):
        app = get_app()
        fake, p = patch_mysql(app, _responder())
        with p:
            resp = client_as(app, "hr_manager").post(
                "/hr/skill-targets/save",
                json={"department": "", "skill_name": "Python", "target_level": 4, "priority": "high"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["success"])
        # FakeCursor.rowcount is 1 -> the NULL-department UPDATE hit, so no INSERT.
        self.assertTrue(fake.queries("update company_skill_targets"))
        self.assertFalse(fake.queries("insert into company_skill_targets"))

    def test_save_rejects_bad_level_with_danish_message(self):
        app = get_app()
        with patch_mysql(app, _responder())[1]:
            resp = client_as(app, "hr_manager").post(
                "/hr/skill-targets/save", json={"skill_name": "Python", "target_level": "abc"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Målniveau", resp.get_json()["message"])

    def test_assign_to_department_reads_dict_rows(self):
        app = get_app()

        def extra(s, params):
            if s.startswith("select user_id from company_users"):
                return [{"user_id": 21}, {"user_id": 22}]
            return None

        fake, p = patch_mysql(app, _responder(extra))
        with p:
            resp = client_as(app, "hr_manager").post(
                "/hr/skills/assign", json={"skill_name": "Excel", "level": 3, "department": "Salg"})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertIn("2 medarbejder", resp.get_json()["message"])
        self.assertEqual(len(fake.queries("insert into employee_skills_matrix")), 2)


class EngagementTests(unittest.TestCase):
    def test_nudge_reads_dict_rows(self):
        app = get_app()

        def extra(s, params):
            if s.startswith("select user_id from company_users"):
                return [{"user_id": 31}]
            return None

        with patch_mysql(app, _responder(extra))[1], \
                mock.patch("notification_service.insert_company_notification", return_value=True) as ins:
            resp = client_as(app, "hr_manager").post("/hr/engagement/nudge", json={"user_ids": [31, 32]})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["nudged"], 1)
        self.assertEqual(ins.call_count, 1)

    def test_page_formats_dates_and_hides_raw_errors(self):
        app = get_app()
        ns = {"non_starters": [{"user_id": 1, "username": "a", "full_name": "Anna", "department": "Salg",
                                "not_started_courses": [{"deadline": "2026-10-12T00:00:00"}]}],
              "total_employees": 1, "total_not_started_courses": 1}
        inactive = {"error": "boom: 'int' object has no attribute 'isoformat'"}
        with patch_mysql(app, _responder())[1], \
                mock.patch("hr_ext._tool", side_effect=lambda name, args=None: ns if "non_starters" in name else inactive):
            html = client_as(app, "hr_manager").get("/hr/engagement").get_data(as_text=True)
        self.assertIn("12.10.2026", html)
        self.assertNotIn("isoformat", html)
        self.assertIn("Prøv at genindlæse siden", html)


class LearningAnalyticsTests(unittest.TestCase):
    def _responder(self):
        today = datetime.date.today()

        def extra(s, params):
            if s.startswith("select date(co.created_at) as date"):
                return [{"date": today, "enrollments": 5, "completions": 2, "unique_learners": 4},
                        {"date": today - datetime.timedelta(days=1), "enrollments": 7, "completions": 3,
                         "unique_learners": 5}]
            if s.startswith("select count(*) as total_interactions"):
                return [{"total_interactions": 40, "unique_users": 9, "avg_response_time": 900,
                         "avg_quality": 0.8}]
            if s.startswith("select count(*) as total, count(case when status = 'active'"):
                return [{"total": 12, "active": 10, "new_hires": 1}]
            return None
        return extra

    def test_kpi_totals_sum_all_days_and_ai_block_renders(self):
        app = get_app()
        with patch_mysql(app, _responder(self._responder()))[1]:
            html = client_as(app, "hr_manager").get("/hr/learning-analytics").get_data(as_text=True)
        # 5 + 7 enrolments, 2 + 3 completions: the old loop-scoped {% set %} left both at 0.
        self.assertRegex(html, r'Tilmeldinger</div><div class="kv tnum">12</div>')
        self.assertRegex(html, r'Gennemført</div><div class="kv tnum">5</div>')
        self.assertIn("Medarbejdernes brug af AI-assistenten", html)
        self.assertIn("80%", html)

    def test_ai_block_uses_a_fresh_cursor(self):
        """A closed MySQLdb cursor raises on execute; the AI block must not reuse one."""
        from tests import secapp

        class StrictCursor(secapp.FakeCursor):
            closed = False

            def execute(self, sql, params=None):
                if self.closed:
                    raise RuntimeError("cursor closed")
                return super().execute(sql, params)

            def close(self):
                self.closed = True

        class StrictConn(secapp.FakeConnection):
            def cursor(self, *a, **k):
                return StrictCursor(self.log, self.responder)

        app = get_app()
        fake = secapp.FakeMySQL(_responder(self._responder()))
        fake.connection = StrictConn(fake.log, fake.connection.responder)
        with mock.patch.object(app, "mysql", fake):
            html = client_as(app, "hr_manager").get("/hr/learning-analytics").get_data(as_text=True)
        self.assertIn("Medarbejdernes brug af AI-assistenten", html)
        self.assertRegex(html, r'Samtaler</div><div class="kv tnum">40</div>')


class DashboardTests(unittest.TestCase):
    def _html(self, extra=None):
        app = get_app()
        with patch_mysql(app, _responder(extra))[1], \
                mock.patch.dict(app.jinja_env.globals,
                                {"credit_chip": lambda: {"scope": "company", "balance": 12345, "label": "12345"}}):
            return client_as(app, "hr_manager").get("/dashboard").get_data(as_text=True)

    def test_role_label_is_danish_and_not_the_raw_role_key(self):
        html = self._html()
        self.assertIn("HR-leder", html)
        self.assertNotIn("hr_manager", html)

    def test_credits_tile_matches_header_chip(self):
        html = self._html()
        self.assertGreaterEqual(html.count("12.345"), 2)  # status card + KPI tile

    def test_kpi_tiles_are_links(self):
        html = self._html()
        for href in ('href="/notifications"', 'href="/hr/approvals"', 'href="/analytics"'):
            self.assertIn(href, html)


class ExportTests(unittest.TestCase):
    def test_export_without_company_slug(self):
        app = get_app()

        def extra(s, params):
            return [{"afdeling": "Salg", "medarbejdere": 3}]

        with patch_mysql(app, _responder(extra))[1], \
                mock.patch("report_exports.build", return_value=(["a"], [[1]])):
            resp = client_as(app, "hr_manager").get("/hr/export/budget")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("budget_virksomhed_", resp.headers["Content-Disposition"])
        self.assertEqual(resp.headers["Content-Type"], "text/csv; charset=utf-8")

    def test_empty_export_returns_to_referring_page(self):
        app = get_app()
        with patch_mysql(app, _responder())[1], \
                mock.patch("report_exports.build", return_value=([], [])):
            resp = client_as(app, "hr_manager").get(
                "/hr/export/course_completions",
                headers={"Referer": "http://localhost/hr/learning-analytics"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/hr/learning-analytics"))


if __name__ == "__main__":
    unittest.main()
