"""learner_context: the learner's own HR data as a per-turn AI context layer.

Offline: a fake cursor returns canned rows keyed by SQL substring, Flask's
current_app is patched with a MagicMock (new=... is mandatory — patching the
LocalProxy without it raises "Working outside of application context"), and
competency is swapped in sys.modules so no real DB is ever touched.
"""
import datetime
import os
import re
import sys
import types
import unittest
from unittest import mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO_ROOT)

_SAFE_ENV = {
    "SANDBOX": "1",
    "AI_WARMUP_ON_IMPORT": "0",
    "SCHEDULER_OPPORTUNISTIC": "0",
    "MYSQL_HOST": "127.0.0.1", "MYSQL_PORT": "3306",
    "MYSQL_USER": "none", "MYSQL_PASSWORD": "none", "MYSQL_DB": "none",
    "OPENAI_API_KEY": "sk-test",
}
for k, v in _SAFE_ENV.items():
    os.environ.setdefault(k, v)

import learner_context  # noqa: E402

CU_ID = 55501        # company_users.id
USER_ID = 90417      # users.id == company_users.user_id
COMPANY_ID = 3311
USERNAME = "anna.l"

ROUTES_OK = {
    "FROM company_users": [{"id": CU_ID, "company_id": COMPANY_ID,
                            "user_id": USER_ID, "department": "Salg"}],
    "FROM employee_learning_progress": [
        {"course_handle": "excel-videregaaende", "content_name": "Excel videregående",
         "status": "in_progress", "progress_percentage": "40.00",
         "due_date": datetime.date(2026, 9, 1), "path_name": "Data i salg",
         "path_category": "IT", "difficulty_level": "mellem"},
    ],
    "FROM employee_skills_matrix": [
        {"skill_name": "Excel", "current_level": 2, "target_level": 4},
        {"skill_name": "Forhandling", "current_level": 3, "target_level": 0},
    ],
    "FROM employee_goals": [
        {"goal_title": "Blive teamleder", "goal_description": "Inden udgangen af 2027",
         "target_date": datetime.date(2027, 12, 31), "status": "active", "progress": 10},
    ],
}

DEPT_TARGETS = [
    {"skill_name": "Excel", "target_level": 4, "priority": "high"},        # dup of matrix gap
    {"skill_name": "Forhandling", "target_level": 3, "priority": "high"},  # already met
    {"skill_name": "CRM", "target_level": 3, "priority": "critical"},
]


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        for needle, result in self.conn.routes.items():
            if needle in sql:
                if isinstance(result, Exception):
                    raise result
                self._rows = list(result)
                return
        self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class FakeConn:
    def __init__(self, routes):
        self.routes = routes
        self.executed = []
        self.rollbacks = 0

    def cursor(self, *args, **kwargs):
        return FakeCursor(self)

    def rollback(self):
        self.rollbacks += 1

    def ping(self, *args, **kwargs):
        return None


class LearnerContextTestBase(unittest.TestCase):
    def setUp(self):
        learner_context.clear_cache()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("AI_LEARNER_HR_CONTEXT", None)
        os.environ.pop("AI_LEARNER_HR_GOALS", None)
        self.fake_competency = types.ModuleType("competency")
        self.fake_competency._company_targets = mock.MagicMock(return_value=list(DEPT_TARGETS))
        self.mods = mock.patch.dict(sys.modules, {"competency": self.fake_competency})
        self.mods.start()

    def tearDown(self):
        self.mods.stop()
        self.env.stop()
        learner_context.clear_cache()

    def run_with(self, routes, **kwargs):
        conn = FakeConn(routes)
        app = mock.MagicMock()
        app.mysql.connection = conn
        with mock.patch.object(learner_context, "current_app", new=app):
            ctx = learner_context.build_learner_hr_context(USERNAME, **kwargs)
        return ctx, conn


class FlagTests(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_LEARNER_HR_CONTEXT", None)
            os.environ.pop("AI_LEARNER_HR_GOALS", None)
            self.assertTrue(learner_context.hr_context_enabled())
            self.assertFalse(learner_context.hr_goals_enabled())

    def test_overrides(self):
        with mock.patch.dict(os.environ, {"AI_LEARNER_HR_CONTEXT": "0", "AI_LEARNER_HR_GOALS": "1"}):
            self.assertFalse(learner_context.hr_context_enabled())
            self.assertTrue(learner_context.hr_goals_enabled())


class ResolveTests(LearnerContextTestBase):
    def test_resolve_by_username(self):
        conn = FakeConn(dict(ROUTES_OK))
        app = mock.MagicMock()
        app.mysql.connection = conn
        with mock.patch.object(learner_context, "current_app", new=app):
            emp = learner_context.resolve_company_user(USERNAME, company_id=COMPANY_ID)
        self.assertEqual(emp, {"employee_id": CU_ID, "user_id": USER_ID,
                               "company_id": COMPANY_ID, "department": "Salg"})
        sql, params = conn.executed[0]
        self.assertIn("username = %s", sql)
        self.assertIn("status = 'active'", sql)
        self.assertEqual(params, (USERNAME, COMPANY_ID))

    def test_resolve_falls_back_to_users_join(self):
        """SSO-created rows have no company_users.username."""
        routes = {"JOIN users u": ROUTES_OK["FROM company_users"], "FROM company_users": []}
        conn = FakeConn(routes)
        app = mock.MagicMock()
        app.mysql.connection = conn
        with mock.patch.object(learner_context, "current_app", new=app):
            emp = learner_context.resolve_company_user(USERNAME)
        self.assertEqual(emp["employee_id"], CU_ID)
        self.assertEqual(len(conn.executed), 2)

    def test_resolve_db_error_returns_none(self):
        conn = FakeConn({"FROM company_users": RuntimeError("boom")})
        app = mock.MagicMock()
        app.mysql.connection = conn
        with mock.patch.object(learner_context, "current_app", new=app):
            self.assertIsNone(learner_context.resolve_company_user(USERNAME))
        self.assertGreaterEqual(conn.rollbacks, 1)


class BuildTests(LearnerContextTestBase):
    def test_full_context(self):
        ctx, conn = self.run_with(dict(ROUTES_OK))
        self.assertEqual(ctx["failed_sources"], [])
        self.assertEqual(ctx["employee"]["user_id"], USER_ID)
        self.assertEqual(ctx["assignments"][0]["title"], "Excel videregående")
        self.assertEqual(ctx["skill_gaps"], [{"skill": "Excel", "current_level": 2,
                                              "target_level": 4, "gap": 2}])
        # Excel is already a matrix gap, Forhandling is met → only CRM remains.
        self.assertEqual([t["skill"] for t in ctx["dept_targets"]], ["CRM"])
        self.fake_competency._company_targets.assert_called_once_with(USERNAME)

    def test_each_source_degrades_independently(self):
        for needle, name in (("FROM employee_learning_progress", "assignments"),
                             ("FROM employee_skills_matrix", "skill_gaps"),
                             ("FROM employee_goals", "hr_goals")):
            with self.subTest(source=name):
                learner_context.clear_cache()
                routes = dict(ROUTES_OK)
                routes[needle] = RuntimeError("table missing")
                with mock.patch.dict(os.environ, {"AI_LEARNER_HR_GOALS": "1"}):
                    ctx, conn = self.run_with(routes)
                self.assertEqual(ctx["failed_sources"], [name])
                self.assertGreaterEqual(conn.rollbacks, 1)
                self.assertIsNotNone(ctx["employee"])
                if name != "assignments":
                    self.assertTrue(ctx["assignments"])
                if name != "hr_goals":
                    self.assertTrue(ctx["hr_goals"])
                if name != "skill_gaps":
                    self.assertTrue(ctx["skill_gaps"])
                self.assertTrue(ctx["dept_targets"])

    def test_dept_targets_failure_is_isolated(self):
        self.fake_competency._company_targets.side_effect = RuntimeError("nope")
        ctx, conn = self.run_with(dict(ROUTES_OK))
        self.assertEqual(ctx["failed_sources"], ["dept_targets"])
        self.assertTrue(ctx["assignments"])
        self.assertTrue(ctx["skill_gaps"])
        self.assertGreaterEqual(conn.rollbacks, 1)

    def test_employee_lookup_failure(self):
        routes = dict(ROUTES_OK)
        routes["FROM company_users"] = RuntimeError("down")
        ctx, conn = self.run_with(routes)
        self.assertEqual(ctx["failed_sources"], ["employee"])
        self.assertIsNone(ctx["employee"])
        self.assertGreaterEqual(conn.rollbacks, 1)

    def test_hr_goals_off_by_default(self):
        ctx, conn = self.run_with(dict(ROUTES_OK))
        self.assertEqual(ctx["hr_goals"], [])
        self.assertFalse(any("employee_goals" in sql for sql, _ in conn.executed))

    def test_hr_goals_on_with_flag(self):
        with mock.patch.dict(os.environ, {"AI_LEARNER_HR_GOALS": "1"}):
            ctx, conn = self.run_with(dict(ROUTES_OK))
        self.assertEqual(ctx["hr_goals"][0]["title"], "Blive teamleder")
        goal_sql = [(s, p) for s, p in conn.executed if "employee_goals" in s]
        self.assertEqual(goal_sql[0][1][:2], (USER_ID, COMPANY_ID))

    def test_master_flag_off_queries_nothing(self):
        with mock.patch.dict(os.environ, {"AI_LEARNER_HR_CONTEXT": "0"}):
            ctx, conn = self.run_with(dict(ROUTES_OK))
        self.assertEqual(conn.executed, [])
        self.assertIsNone(ctx["employee"])

    def test_all_sql_parameterised_with_learner_ids(self):
        with mock.patch.dict(os.environ, {"AI_LEARNER_HR_GOALS": "1"}):
            ctx, conn = self.run_with(dict(ROUTES_OK), company_id=COMPANY_ID, user_id=USER_ID)
        self.assertTrue(conn.executed)
        for sql, params in conn.executed:
            self.assertIsInstance(params, tuple, sql)
            for ident in (str(USER_ID), str(COMPANY_ID), str(CU_ID), USERNAME):
                self.assertNotIn(ident, sql)
            self.assertEqual(sql.count("%s"), len(params), sql)
            if "company_users" in sql:
                self.assertIn(USERNAME, params)
            else:
                self.assertEqual(params[:2], (USER_ID, COMPANY_ID), sql)
                self.assertTrue(re.search(r"(user_id|employee_id) = %s AND (elp\.)?company_id = %s", sql), sql)

    def test_row_user_id_wins_over_caller_user_id(self):
        ctx, conn = self.run_with(dict(ROUTES_OK), user_id=1)
        for sql, params in conn.executed:
            if "company_users" not in sql:
                self.assertEqual(params[0], USER_ID)

    def test_cache_hit_avoids_second_query(self):
        ctx1, conn1 = self.run_with(dict(ROUTES_OK))
        self.assertTrue(conn1.executed)
        ctx2, conn2 = self.run_with(dict(ROUTES_OK))
        self.assertEqual(conn2.executed, [])
        self.assertEqual(ctx1, ctx2)
        # Returned copies: mutating one must not poison the cache.
        ctx2["assignments"].clear()
        ctx3, _ = self.run_with(dict(ROUTES_OK))
        self.assertTrue(ctx3["assignments"])

    def test_degraded_result_not_cached(self):
        routes = dict(ROUTES_OK)
        routes["FROM employee_skills_matrix"] = RuntimeError("blip")
        self.run_with(routes)
        _, conn2 = self.run_with(dict(ROUTES_OK))
        self.assertTrue(conn2.executed)

    def test_no_company_user_row(self):
        routes = {"FROM company_users": []}
        ctx, conn = self.run_with(routes)
        self.assertEqual(ctx, learner_context._empty_context())
        self.assertEqual(conn.rollbacks, 0)
        self.fake_competency._company_targets.assert_not_called()
        self.assertEqual(learner_context.format_learner_hr_context(ctx), "")

    def test_never_raises_without_app_context(self):
        # Real current_app proxy outside an app context → treated as a DB failure.
        ctx = learner_context.build_learner_hr_context(USERNAME)
        self.assertEqual(ctx["failed_sources"], ["employee"])


class FormatTests(unittest.TestCase):
    TODAY = datetime.date(2026, 9, 14)

    def test_empty(self):
        self.assertEqual(learner_context.format_learner_hr_context(learner_context._empty_context()), "")
        self.assertEqual(learner_context.format_learner_hr_context({}), "")
        self.assertEqual(learner_context.format_learner_hr_context(None), "")

    def test_sections(self):
        ctx = learner_context._empty_context()
        ctx["assignments"] = [{"title": "Excel videregående", "path_name": "Data i salg",
                               "status": "in_progress", "progress": "40.00",
                               "due_date": datetime.date(2026, 9, 1)}]
        ctx["skill_gaps"] = [{"skill": "Excel", "current_level": 2, "target_level": 4, "gap": 2}]
        ctx["dept_targets"] = [{"skill": "CRM", "target_level": 3, "current_level": None,
                                "priority": "critical"}]
        ctx["hr_goals"] = [{"title": "Blive teamleder", "description": "", "status": "active",
                            "progress": 0, "target_date": "2027-12-31"}]
        text = learner_context.format_learner_hr_context(ctx, today=self.TODAY)
        self.assertIn("Tildelt af HR:", text)
        self.assertIn("- Excel videregående (forløb: Data i salg) — i gang, 40 %, frist 2026-09-01 (overskredet)", text)
        self.assertIn("Kompetencemål fra HR (nu → mål, skala 1-5):\n- Excel: 2 → 4", text)
        self.assertIn("Afdelingens kompetencemål (skala 1-5):\n- CRM: mål 3, prioritet kritisk", text)
        self.assertIn("Mål sat af HR:\n- Blive teamleder — måldato 2027-12-31", text)

    def test_length_cap_and_newline_sanitising(self):
        ctx = learner_context._empty_context()
        ctx["assignments"] = [{"title": "Kursus\nnummer %d " % i + "x" * 200,
                               "status": "not_started", "progress": 0, "due_date": None}
                              for i in range(40)]
        ctx["dept_targets"] = [{"skill": "Skill %d" % i, "target_level": 4,
                                "current_level": 1, "priority": "high"} for i in range(40)]
        text = learner_context.format_learner_hr_context(ctx, today=self.TODAY)
        self.assertLessEqual(len(text), 1800)
        self.assertTrue(text.startswith("Tildelt af HR:"))
        self.assertFalse(text.rstrip().endswith(":"))
        for line in text.split("\n"):
            self.assertLessEqual(len(line), 120)


if __name__ == "__main__":
    unittest.main()
