"""Regression tests for the HR analytics graph pass (offline, FakeMySQL).

Covers the behaviour behind the charts, not the pixels:
  * department breakdowns are k-floored before they reach a chart or list
  * time series are gap-free (a quiet day is a zero, not a missing point)
  * charts that had no honest data source were replaced, half-built sections
    render real data, and units/labels are Danish
"""
import datetime
import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

from tests.secapp import client_as, get_app, patch_mysql  # noqa: E402


COMPANY = {"id": 7, "name": "Demo", "company_name": "Demo", "company_slug": "demo",
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


def _get(path, extra=None, role="hr_manager", patches=()):
    app = get_app()
    fake, p = patch_mysql(app, _responder(extra))
    with p:
        ctxs = [mock.patch(*a, **k) for a, k in patches]
        for c in ctxs:
            c.start()
        try:
            resp = client_as(app, role).get(path)
        finally:
            for c in ctxs:
                c.stop()
    return resp, fake


class HelperTests(unittest.TestCase):
    def test_kanon_department_rows_drops_small_and_labels_null(self):
        import hr_dashboard
        rows = [{"department": "Salg", "employee_count": 9},
                {"department": None, "employee_count": 6},
                {"department": "Jura", "employee_count": 2}]
        kept, note = hr_dashboard._kanon_department_rows(rows, "employee_count")
        self.assertEqual([r["department_label"] for r in kept], ["Salg", "Uden afdeling"])
        self.assertIn("k=", note)

    def test_kanon_department_rows_fails_closed_without_kanon(self):
        import hr_dashboard
        with mock.patch.object(hr_dashboard, "_kanon", None):
            kept, note = hr_dashboard._kanon_department_rows(
                [{"department": "Salg", "employee_count": 50}], "employee_count")
        self.assertEqual(kept, [])
        self.assertTrue(note)

    def test_dense_daily_series_fills_gaps(self):
        import hr_dashboard
        end = datetime.date(2026, 10, 2)
        days, vals = hr_dashboard._dense_daily_series(
            {datetime.date(2026, 9, 30): 4, "2026-10-02": 1}, 4, end=end)
        self.assertEqual(days, ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"])
        self.assertEqual(vals, [0, 4, 0, 1])


def _render(template, **ctx):
    """Render a workspace template inside an HR-manager request context."""
    from flask import render_template, session
    from tests.secapp import ROLES
    app = get_app()
    with patch_mysql(app, _responder())[1], app.test_request_context("/hr/"):
        session.update(ROLES["hr_manager"])
        return render_template(template, company=COMPANY, **ctx)


class OverviewTests(unittest.TestCase):
    def test_department_charts_use_k_floored_rows(self):
        import hr_dashboard
        rows = [{"department": "Salg", "employee_count": 8, "total_enrollments": 10,
                 "completed_courses": 4, "completion_rate": 40.0},
                {"department": "Lille", "employee_count": 1, "total_enrollments": 3,
                 "completed_courses": 3, "completion_rate": 100.0}]
        kept, note = hr_dashboard._kanon_department_rows(rows, "employee_count")
        html = _render("fm/hr.html", department_performance=rows, department_chart=kept,
                       department_anon_note=note, completion_rate=50.0)
        self.assertIn('"department_label": "Salg"', html)
        self.assertNotIn("Lille", html)
        self.assertIn("Grupper under k=", html)
        self.assertIn("hrDeptRate", html)

    def test_no_department_charts_without_orders(self):
        html = _render("fm/hr.html", department_chart=[
            {"department": "Salg", "department_label": "Salg", "employee_count": 8,
             "total_enrollments": 0, "completed_courses": 0, "completion_rate": None}])
        self.assertNotIn('id="hrDeptRate"', html)
        self.assertIn("Ingen ordrer", html)


class RoiTests(unittest.TestCase):
    ROI = {"fiscal_year": 2026, "has_data": True, "total_training_spend": 24000.0,
           "employees_trained": 6, "courses_completed": 3, "courses_total": 5,
           "completion_rate": 60.0, "spend_per_employee": 4000, "cost_per_completion": 8000,
           "department_roi": [{"department": "Salg", "spend": 18000.0, "employees": 5, "completed": 2,
                               "total_orders": 3, "completion_rate": 66.7, "cost_per_completion": 9000}],
           "department_roi_anon_note": "Grupper under k=5 er skjult af hensyn til anonymitet",
           "budget_total": 0, "budget_overruns": [], "scenario": None}

    def test_monthly_and_department_spend_charts(self):
        def extra(s, params):
            if s.startswith("select month(created_at) as m"):
                return [{"m": 1, "spend": 12000}, {"m": 3, "spend": 12000}]
            return None
        resp, fake = _get("/hr/roi?year=2025", extra, patches=[
            (("insights_engine.get_roi_metrics",), {"return_value": dict(self.ROI)}),
            (("insights_engine.get_predictive_data",), {"return_value": {}}),
            (("insights_engine.get_uplift_roi",), {"return_value": None}),
        ])
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200, html[:300])
        # Full past year: 12 months, gaps are zeros.
        self.assertIn('"spend": [12000, 0, 12000, 0, 0, 0, 0, 0, 0, 0, 0, 0]', html)
        self.assertIn("roiDeptChart", html)
        self.assertIn("Grupper under k=5", html)
        self.assertIn("24.000", html)
        # Tenant scoping on the new query.
        q = fake.queries("select month(created_at) as m")
        self.assertTrue(q and q[0][1][0] == 7)

    def test_rare_search_terms_are_k_floored(self):
        preds = {"trending_courses": [{"query_text": "excel kursus", "cnt": 9},
                                      {"query_text": "min helt private søgning", "cnt": 1}]}
        resp, _ = _get("/hr/roi", None, patches=[
            (("insights_engine.get_roi_metrics",), {"return_value": {"has_data": False}}),
            (("insights_engine.get_predictive_data",), {"return_value": preds}),
            (("insights_engine.get_uplift_roi",), {"return_value": None}),
        ])
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200, html[:300])
        self.assertIn("excel kursus", html)
        self.assertNotIn("min helt private søgning", html)
        self.assertIn("Grupper under k=", html)


class FunnelRetentionTests(unittest.TestCase):
    def test_funnel_daily_series_is_gap_free(self):
        today = datetime.date.today()
        daily = {"labels": [(today - datetime.timedelta(days=2)).isoformat()], "data": [5]}
        funnel = {"sessions": 9, "searches": 5, "shown": 3, "ordered": 1, "rate_search": 55.6,
                  "rate_shown": 60.0, "rate_ordered": 33.3, "overall_rate": 11.1, "days": 7,
                  "suppressed": False, "anon_note": None}
        resp, _ = _get("/hr/funnel?days=7", None, patches=[
            (("report_query.conversion_funnel",), {"return_value": funnel}),
            (("report_query.daily_volume",), {"return_value": daily}),
        ])
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200, html[:300])
        self.assertIn("var data = [0, 0, 0, 0, 5, 0, 0];", html)

    def test_cohort_retention_marks_future_months(self):
        import report_query
        this_month = datetime.date.today().strftime("%Y-%m")

        def extra(s, params):
            if "month_offset" in s:
                return [{"cohort_month": this_month, "month_offset": 0, "learners": 3}]
            if s.startswith("select cohort_month, count(*) as size"):
                return [{"cohort_month": this_month, "size": 6}]
            return None

        app = get_app()
        with patch_mysql(app, _responder(extra))[1], app.app_context():
            out = report_query.cohort_retention(7, 2)
        cells = out["cohorts"][0]["retention"]
        self.assertEqual([c["future"] for c in cells], [False, True, True])
        self.assertEqual(cells[0]["pct"], 50.0)

    def test_retention_page_renders_future_cells_and_curve(self):
        ret = {"cohorts": [{"cohort": "2026-09", "size": 6, "_cohort": 6, "retention": [
            {"offset": 0, "count": 3, "pct": 50.0, "future": False},
            {"offset": 1, "count": 0, "pct": 0.0, "future": False},
            {"offset": 2, "count": 0, "pct": 0.0, "future": True}]}],
            "max_offset": 2, "months": 2, "company_id": 7, "anon_note": None}
        resp, _ = _get("/hr/retention?months=2", None, patches=[
            (("report_query.cohort_retention",), {"return_value": ret}),
        ])
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200, html[:300])
        self.assertIn("september 2026", html)
        self.assertIn('class="rt-cell future"', html)
        self.assertIn('class="rt-cell empty">0%', html)
        self.assertIn("rtCurveChart", html)


class LearningAnalyticsGraphTests(unittest.TestCase):
    def _extra(self, s, params):
        today = datetime.date.today()
        if s.startswith("select date(co.created_at) as date"):
            return [{"date": today - datetime.timedelta(days=2), "enrollments": 4, "completions": 1,
                     "unique_learners": 3}]
        if s.startswith("select cu.department, count(distinct cu.user_id) as total_employees"):
            return [{"department": "Salg", "total_employees": 9, "employees_with_training": 6,
                     "total_enrollments": 8, "completions": 4, "participation_rate": 66.7,
                     "completion_rate": 50.0},
                    {"department": "Solo", "total_employees": 1, "employees_with_training": 1,
                     "total_enrollments": 2, "completions": 2, "participation_rate": 100.0,
                     "completion_rate": 100.0}]
        if s.startswith("select case when co.product_title like"):
            return [{"skill_category": "Leadership", "demand": 7, "supply": 3,
                     "interested_employees": 6, "fulfillment_rate": 42.9}]
        return None

    def test_trend_gap_free_departments_floored_categories_danish(self):
        resp, _ = _get("/hr/learning-analytics?period=7d", self._extra)
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200, html[:300])
        self.assertIn('"enrollments": [0, 0, 0, 0, 4, 0, 0]', html)
        self.assertIn('"department_label": "Salg"', html)
        self.assertNotIn('"department_label": "Solo"', html)
        self.assertIn('"skill_category": "Ledelse"', html)
        self.assertNotIn("Leadership", html)

    def test_year_is_bucketed_by_month(self):
        resp, _ = _get("/hr/learning-analytics?period=1y", self._extra)
        html = resp.get_data(as_text=True)
        self.assertIn('"granularity": "month"', html)
        self.assertIn("Pr. måned", html)


class SkillEngagementBenchmarkTests(unittest.TestCase):
    def test_engagement_histogram_gets_real_days(self):
        # The histogram used a variable scoped to the content block, so it always
        # received [] and rendered its empty state.
        inactive = {"inactive_days_threshold": 30, "total": 2, "employees": [
            {"user_id": 1, "full_name": "Anna", "department": "Salg", "days_inactive": 45},
            {"user_id": 2, "full_name": "Bo", "department": "Salg", "days_inactive": None}]}
        resp, _ = _get("/hr/engagement", None, patches=[
            (("hr_ext._tool",), {"side_effect": lambda name, args=None: inactive if "inactive" in name else {}}),
        ])
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200, html[:300])
        self.assertIn("days: [45, null]", html)
        self.assertIn("Aldrig aktiv", html)

    def test_skill_gap_chart_and_top_skills_ranked(self):
        app = get_app()
        heat = {"Salg": {"Python": {"target_level": 4, "priority": "high", "employees": 6,
                                    "avg_current": 2.0, "gap": 2.0, "status": "red"}}}

        with patch_mysql(app, _responder())[1], \
                mock.patch("insights_engine.get_skill_gap_analysis", return_value=heat), \
                mock.patch("insights_engine.get_skill_growth_trend",
                           return_value={"labels": [], "avg_levels": [], "has_data": False}):
            html = client_as(app, "hr_manager").get("/hr/skill-gaps").get_data(as_text=True)
        self.assertIn('id="skillGapBars"', html)
        self.assertNotIn("FMChart.radar", html)
        # "Top" skills are ranked by holders, not alphabetically.
        html = _render("fm/skill_gaps.html", heatmap={}, skill_list=[
            {"skill_name": "Aaa", "avg_level": 2, "count": 1},
            {"skill_name": "Zeta", "avg_level": 3, "count": 9}])
        self.assertLess(html.index("Zeta"), html.index("Aaa"))

    def test_benchmark_chart_is_ranked_with_median_split(self):
        data = {"industry": "IT", "cohort_size": 8, "k": 5, "safe": True, "overall_note": "",
                "metrics": [{"key": "completion_rate", "label": "Gennemførelse", "unit": "%",
                             "your_value": 70, "cohort_avg": 60, "cohort_median": 58,
                             "your_percentile": 72, "safe": True},
                            {"key": "spend_per_employee", "label": "Forbrug", "unit": "kr",
                             "your_value": 900, "cohort_avg": 1200, "cohort_median": 1100,
                             "your_percentile": 30, "safe": True}]}
        resp, _ = _get("/hr/benchmarking", None, patches=[
            (("benchmarking.benchmark",), {"return_value": data}),
        ])
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200, html[:300])
        self.assertIn("Under branchens median", html)
        self.assertIn("50 er branchens median", html)
        # The chart gets the real percentiles (it used to read a block-scoped var).
        self.assertIn("var pct = [72, 30];", html)


class OperationsPagesTests(unittest.TestCase):
    def test_hr_reports_department_chart_is_k_floored(self):
        def extra(s, params):
            if s.startswith("select cu.department, count(distinct cu.user_id) as employees"):
                return [{"department": "Salg", "employees": 7, "orders": 4, "completed": 2, "spend": 12500},
                        {"department": "Solo", "employees": 1, "orders": 2, "completed": 2, "spend": 9000}]
            return None
        resp, _ = _get("/hr/reports", extra, patches=[
            (("report_exports.REPORTS",), {"new": {}}),
        ])
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200, html[:300])
        self.assertIn("deptCompletionChart", html)
        self.assertIn("12.500 kr", html)
        self.assertNotIn("Solo", html)
        self.assertIn("Grupper under k=", html)

    def test_compliance_status_is_a_status_coloured_stack(self):
        html = _render("fm/compliance.html", totals={"requirements": 2, "compliant": 6,
                                                     "expiring": 1, "overdue": 3},
                       matrix=[], at_risk_chart=[])
        self.assertIn("FMChart.stackedBar('complianceStatus'", html)
        self.assertNotIn("FMChart.doughnut", html)
        self.assertIn("60% <span", html)

    def test_budget_chart_splits_spent_left_and_overrun(self):
        html = _render("fm/budgets.html", fiscal_year=2026, total_budget=100000, total_spent=120000,
                       budgets=[{"department": "Salg", "annual_budget": 50000, "spent": 70000,
                                 "employee_count": 6}], unbudgeted_depts=[])
        self.assertIn("Over budget (kr)", html)
        self.assertIn("Overskredet", html)
        self.assertIn("20.000", html)

    def test_training_plan_gap_chart_on_fixed_scale(self):
        plan = {"priority_gaps": [{"skill": "Excel", "gap": 1, "critical": False},
                                  {"skill": "Python", "gap": 3, "critical": True}],
                "recommended_courses": [], "next_actions": []}
        resp, _ = _get("/hr/training-plan", None, patches=[
            (("hr_ext._tool",), {"return_value": plan}),
        ])
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200, html[:300])
        self.assertIn("max: 5", html)
        self.assertIn("største gab øverst", html)


class PeopleAndUsagePagesTests(unittest.TestCase):
    def test_team_cockpit_drops_fake_sparkline(self):
        html = _render("fm/team_cockpit.html", scope="reports", summary={}, pending_for_me=[],
                       reports=[{"user_id": 3, "name": "Anna", "courses_enrolled": 4,
                                 "courses_completed": 1, "avg_progress": 40}])
        self.assertNotIn("team-spark", html)
        self.assertIn("FMChart.stackedBar('teamCompletion'", html)
        self.assertIn("· 25%", html)

    def test_my_department_budget_is_a_meter_not_a_pie(self):
        html = _render("fm/my_department.html", department="Salg", fiscal_year=2026,
                       employees=[], orders=[], pending_count=0,
                       budget={"annual_budget": 10000, "spent": 12000, "remaining": -2000,
                               "utilization": 120})
        self.assertNotIn("FMChart.doughnut", html)
        self.assertIn("2.000 kr over budget", html)

    def test_employee_progress_histogram(self):
        html = _render("fm/employee_progress.html", departments=[], current_filters={},
                       summary_stats={}, employees=[
                           {"user_id": 1, "username": "a", "avg_progress": 80, "courses_enrolled": 2,
                            "learning_paths_enrolled": 0},
                           {"user_id": 2, "username": "b", "avg_progress": 0, "courses_enrolled": 0,
                            "learning_paths_enrolled": 0}])
        self.assertIn("'Ingen kurser'", html)
        self.assertNotIn("FMChart.doughnut", html)

    def test_personal_usage_last7_is_calendar_days(self):
        app = get_app()
        old = datetime.datetime.now() - datetime.timedelta(days=20)

        def extra(s, params):
            if s.startswith("select timestamp, credits_used"):
                return [{"timestamp": old, "credits_used": 5, "description": "x"}]
            return None
        with patch_mysql(app, _responder(extra))[1]:
            html = client_as(app, "employee").get("/reports/").get_data(as_text=True)
        today = datetime.date.today()
        self.assertIn('[0, 0, 0, 0, 0, 0, 0]', html)  # no usage in the last 7 calendar days
        self.assertIn(today.isoformat(), html)
        self.assertNotIn("Social exposure", html)

    def test_company_reports_kpis_are_danish(self):
        html = _render("fm/company_reports.html", company_stats={}, total_chatbot_queries=1200,
                       total_conversations=40, conversion_rate=2.5, completed_orders_count=3,
                       pending_orders_count=1, employee_engagement_rate=60.0, total_revenue=45000,
                       daily_chatbot_query_labels=["2026-10-01"], daily_chatbot_query_data=[4])
        self.assertIn("45.000", html)
        self.assertIn("3 gennemførte · 1 afventer", html)
        self.assertIn("FMChart.line('dailyChart'", html)


if __name__ == "__main__":
    unittest.main()
