"""Tests for the skill-uplift loop (plan #19).

The one genuine net-new capability: measuring whether training actually moved
employee skill levels toward their targets, not just counting spend. Covers the
WRITE side (skill_history.record_snapshot / current_level_for — the append-only
trajectory) and the aggregate, k-anon-safe READ side
(insights_engine.get_skill_growth_trend / get_uplift_roi).

No live DB or Flask app — fakes mirror tests/test_company_analytics_rollup.py
and tests/test_workforce_risk_advisor.py. No SANDBOX/MySQL required.
"""

import contextlib
import unittest

import run  # noqa: F401  installs the pymysql->MySQLdb shim
import skill_history
import insights_engine
import kanon


K = kanon.K_DEFAULT


# ── Fakes ─────────────────────────────────────────────────────────────────--

class SeqCursor:
    """Records executed (sql, params) and returns staged fetchone/fetchall."""

    def __init__(self, fetchone_seq=None, fetchall_seq=None, raise_on_execute=False):
        self.executed = []
        self.closed = False
        self._fetchone_seq = list(fetchone_seq or [])
        self._fetchall_seq = list(fetchall_seq or [])
        self._raise = raise_on_execute

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self._raise:
            raise RuntimeError("execute blew up")

    def fetchone(self):
        return self._fetchone_seq.pop(0) if self._fetchone_seq else None

    def fetchall(self):
        return self._fetchall_seq.pop(0) if self._fetchall_seq else []

    def close(self):
        self.closed = True


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, *a, **k):
        return self._cursor


class FakeApp:
    """Minimal app: app_context() + mysql.connection.cursor() + _get_current_object."""

    def __init__(self, cursor):
        self.mysql = type("M", (), {"connection": FakeConn(cursor)})()

    def _get_current_object(self):
        return self

    @contextlib.contextmanager
    def app_context(self):
        yield


# ── record_snapshot (the append-only WRITE side) ─────────────────────────────

class RecordSnapshotTests(unittest.TestCase):
    def test_writes_well_shaped_insert_on_change(self):
        cur = SeqCursor()
        ok = skill_history.record_snapshot(
            cur, company_id=5, employee_id=42, skill_name="Python",
            level=4, previous_level=2, source="assign")
        self.assertTrue(ok)
        sql, params = cur.executed[-1]
        self.assertIn("INSERT INTO employee_skill_history", sql)
        # employee, company, skill, level, previous_level, source, order_id
        self.assertEqual(params, (42, 5, "Python", 4, 2, "assign", None))

    def test_no_row_when_level_unchanged(self):
        cur = SeqCursor()
        ok = skill_history.record_snapshot(
            cur, 5, 42, "Python", level=3, previous_level=3)
        self.assertFalse(ok)
        self.assertEqual(cur.executed, [])  # nothing written

    def test_first_observation_is_captured_when_prev_unknown(self):
        cur = SeqCursor()
        ok = skill_history.record_snapshot(
            cur, 5, 42, "Python", level=3, previous_level=None)
        self.assertTrue(ok)
        _, params = cur.executed[-1]
        self.assertEqual(params[4], None)  # previous_level NULL

    def test_post_course_source_and_order_link_flow_through(self):
        cur = SeqCursor()
        ok = skill_history.record_snapshot(
            cur, 5, 42, "ITIL", level=5, previous_level=3,
            source="post_course", order_id=999)
        self.assertTrue(ok)
        _, params = cur.executed[-1]
        self.assertEqual(params[5], "post_course")
        self.assertEqual(params[6], 999)

    def test_unknown_source_coerced_to_assign(self):
        cur = SeqCursor()
        skill_history.record_snapshot(cur, 5, 42, "X", 3, 1, source="hacker")
        _, params = cur.executed[-1]
        self.assertEqual(params[5], "assign")

    def test_level_clamped_to_0_5(self):
        cur = SeqCursor()
        skill_history.record_snapshot(cur, 5, 42, "X", level=9, previous_level=1)
        _, params = cur.executed[-1]
        self.assertEqual(params[3], 5)

    def test_missing_identifiers_and_blank_skill_are_noops(self):
        cur = SeqCursor()
        self.assertFalse(skill_history.record_snapshot(None, 5, 42, "X", 3))
        self.assertFalse(skill_history.record_snapshot(cur, None, 42, "X", 3))
        self.assertFalse(skill_history.record_snapshot(cur, 5, None, "X", 3))
        self.assertFalse(skill_history.record_snapshot(cur, 5, 42, "", 3))
        self.assertFalse(skill_history.record_snapshot(cur, 5, 42, "X", None))
        self.assertEqual(cur.executed, [])

    def test_db_error_never_raises_returns_false(self):
        cur = SeqCursor(raise_on_execute=True)
        ok = skill_history.record_snapshot(cur, 5, 42, "X", level=4, previous_level=1)
        self.assertFalse(ok)


class CurrentLevelForTests(unittest.TestCase):
    def test_returns_int_level(self):
        cur = SeqCursor(fetchone_seq=[{"current_level": 3}])
        self.assertEqual(skill_history.current_level_for(cur, 5, 42, "Python"), 3)

    def test_tuple_row_supported(self):
        cur = SeqCursor(fetchone_seq=[(4,)])
        self.assertEqual(skill_history.current_level_for(cur, 5, 42, "Python"), 4)

    def test_no_row_returns_none(self):
        cur = SeqCursor(fetchone_seq=[])
        self.assertIsNone(skill_history.current_level_for(cur, 5, 42, "Python"))

    def test_query_is_company_scoped(self):
        cur = SeqCursor(fetchone_seq=[{"current_level": 2}])
        skill_history.current_level_for(cur, 7, 42, "Python")
        sql, params = cur.executed[-1]
        self.assertIn("company_id = %s", sql)
        self.assertIn(7, params)


# ── get_skill_growth_trend (aggregate + k-anon-safe READ side) ───────────────

class SkillGrowthTrendTests(unittest.TestCase):
    def test_k_safe_months_pass_sub_k_months_suppressed(self):
        rows = [
            {"ym": "2026-01", "avg_level": 2.5, "employees": K},      # safe
            {"ym": "2026-02", "avg_level": 3.0, "employees": K - 1},  # sub-k -> dropped
            {"ym": "2026-03", "avg_level": 3.5, "employees": K + 4},  # safe
        ]
        app = FakeApp(SeqCursor(fetchall_seq=[rows]))
        out = insights_engine.get_skill_growth_trend(app, company_id=5, months=6)
        self.assertTrue(out["has_data"])
        self.assertEqual(out["labels"], ["2026-01", "2026-03"])
        self.assertEqual(out["avg_levels"], [2.5, 3.5])
        self.assertEqual(out["suppressed_months"], 1)
        self.assertIsNotNone(out["anon_note"])

    def test_all_months_safe_no_anon_note(self):
        rows = [{"ym": "2026-03", "avg_level": 4.0, "employees": K + 1}]
        app = FakeApp(SeqCursor(fetchall_seq=[rows]))
        out = insights_engine.get_skill_growth_trend(app, 5, months=3)
        self.assertEqual(out["suppressed_months"], 0)
        self.assertIsNone(out["anon_note"])

    def test_query_is_company_scoped_and_aggregate(self):
        app_cur = SeqCursor(fetchall_seq=[[]])
        app = FakeApp(app_cur)
        insights_engine.get_skill_growth_trend(app, 9, months=6)
        sql, params = app_cur.executed[-1]
        self.assertIn("company_id = %s", sql)
        self.assertIn("COUNT(DISTINCT employee_id)", sql)
        self.assertIn(9, params)

    def test_no_data_is_safe_empty(self):
        app = FakeApp(SeqCursor(fetchall_seq=[[]]))
        out = insights_engine.get_skill_growth_trend(app, 5)
        self.assertFalse(out["has_data"])
        self.assertEqual(out["labels"], [])

    def test_db_error_is_safe_empty_never_raises(self):
        app = FakeApp(SeqCursor(raise_on_execute=True))
        out = insights_engine.get_skill_growth_trend(app, 5)
        self.assertFalse(out["has_data"])


# ── get_uplift_roi (measured lift vs spend, k-anon-safe) ─────────────────────

class UpliftRoiTests(unittest.TestCase):
    def _run(self, lift_row, spend_row, company_id=5, year=2026):
        cur = SeqCursor(fetchone_seq=[lift_row, spend_row])
        app = FakeApp(cur)
        return insights_engine.get_uplift_roi(app, company_id, year), cur

    def test_measured_lift_per_kr_computed(self):
        # total_lift 12 points, spend 24.000 kr, 8 employees raised.
        out, _ = self._run(
            {"total_lift": 12, "levels_raised": 10, "employees": K + 3, "skills": 4},
            {"spend": 24000})
        self.assertTrue(out["has_data"])
        self.assertFalse(out["suppressed"])
        self.assertEqual(out["total_lift"], 12.0)
        # 12 / (24000/1000) = 0.5 points per 1.000 kr
        self.assertEqual(out["lift_per_1000_kr"], 0.5)
        self.assertEqual(out["employees"], K + 3)

    def test_sub_k_cohort_is_suppressed(self):
        out, _ = self._run(
            {"total_lift": 6, "levels_raised": 3, "employees": K - 1, "skills": 2},
            {"spend": 12000})
        self.assertTrue(out["suppressed"])
        self.assertEqual(out["total_lift"], 0.0)        # figure withheld
        self.assertEqual(out["lift_per_1000_kr"], 0.0)
        self.assertIsNotNone(out["anon_note"])

    def test_no_measured_lift_is_not_suppressed_just_empty(self):
        # Zero employees raised is "no data", not a privacy case.
        out, _ = self._run(
            {"total_lift": 0, "levels_raised": 0, "employees": 0, "skills": 0},
            {"spend": 5000})
        self.assertFalse(out["suppressed"])
        self.assertFalse(out["has_data"])

    def test_lift_query_excludes_null_previous_and_is_year_scoped(self):
        _, cur = self._run(
            {"total_lift": 4, "levels_raised": 2, "employees": K, "skills": 1},
            {"spend": 8000}, company_id=11, year=2025)
        lift_sql, lift_params = cur.executed[0]
        self.assertIn("previous_level IS NOT NULL", lift_sql)
        self.assertIn("GREATEST(level - previous_level, 0)", lift_sql)
        self.assertIn("YEAR(captured_at) = %s", lift_sql)
        self.assertIn(11, lift_params)
        self.assertIn(2025, lift_params)

    def test_db_error_is_safe_zero(self):
        cur = SeqCursor(raise_on_execute=True)
        app = FakeApp(cur)
        out = insights_engine.get_uplift_roi(app, 5, 2026)
        self.assertFalse(out["has_data"])
        self.assertEqual(out["total_lift"], 0.0)


# ── Route registration + capture wiring ──────────────────────────────────────

class RouteWiringTests(unittest.TestCase):
    def test_confirm_uplift_route_registered(self):
        from run import create_app
        app = create_app()
        rules = {r.rule: r for r in app.url_map.iter_rules()}
        match = [r for rule, r in rules.items()
                 if rule.endswith("/skills/confirm-uplift")]
        self.assertTrue(match, "confirm_skill_uplift route not registered")
        self.assertIn("POST", match[0].methods)

    def test_assign_route_records_history_source(self):
        # The assign path must capture a 'source=assign' snapshot — guard against
        # a future refactor silently dropping the trajectory write.
        import inspect
        import hr_dashboard
        src = inspect.getsource(hr_dashboard)
        self.assertIn("record_snapshot", src)
        self.assertIn("source='post_course'", src)
        self.assertIn("source='assign'", src)


if __name__ == "__main__":
    unittest.main()
