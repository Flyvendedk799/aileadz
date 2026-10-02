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


if __name__ == "__main__":
    unittest.main()
