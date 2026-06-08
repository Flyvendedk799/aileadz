"""hr_compare_cohorts advisor tool with NET-NEW per-cohort k-anon (plan #14).

Comparison ("Salg vs Marketing", "ledere vs medarbejdere", "dette kvartal vs
sidste") is the native shape of strategic HR questions, but every read tool takes
at most a single department filter, so the advisor had to serially call and
hand-diff — slow, error-prone and ungrounded. hr_compare_cohorts runs ONE
self-contained company-scoped aggregate per side and returns both sides + the
delta in one grounded turn.

The hard constraint this initiative had to BUILD (not reuse): per-cohort k-anon.
The get_* read tools have NO suppression today (HR_VALUE_PLAN risk register
#323/#344/#360), so net-new per-cohort suppression via kanon.suppress_small_groups
is required — a cohort below the floor must have its metrics REDACTED (never
silently shown), and the delta must be computed ONLY when BOTH cohorts are k-safe.

Tests run WITHOUT a live DB / OpenAI: the executor is exercised with a patched
session + a fake mysql cursor that answers the two cohort aggregates from a queue;
the registry checks are pure-selection.
"""

import json
import unittest
from unittest import mock

import db_compat  # noqa: F401  (installs pymysql->MySQLdb shim)
import hr_tools
import kanon
from ai_tool_registry import get_hr_tool_selection, tool_name, is_parallel_safe


# ── A fake DictCursor that answers each SELECT from a queue of result rows. The
# compare tool runs exactly two aggregate SELECTs (one per cohort), each read via
# fetchone(); we hand it one row per cohort. ─────────────────────────────────────

class _FakeCursor:
    def __init__(self, fetch_results=None):
        self._fetch = list(fetch_results or [])
        self.executed = []
        self._last_fetch = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if sql.strip().upper().startswith("SELECT"):
            self._last_fetch = self._fetch.pop(0) if self._fetch else None
        return self

    def fetchone(self):
        return self._last_fetch

    def close(self):
        pass


class _FakeApp:
    def __init__(self, cursor):
        self.mysql = mock.Mock()
        self.mysql.connection = mock.Mock()
        self.mysql.connection.cursor = mock.Mock(return_value=cursor)

    def _get_current_object(self):
        return self


def _agg_row(members, *, completed=0, in_progress=0, pending=0,
             total_spend=0, avg_completion_days=None, active_orders=0):
    """One cohort aggregate row as the SQL would return it."""
    return {
        "members": members,
        "completed": completed,
        "in_progress": in_progress,
        "pending": pending,
        "total_spend": total_spend,
        "avg_completion_days": avg_completion_days,
        "active_orders": active_orders,
    }


def _run(args, *, company_id=42, fetch_results=None):
    cursor = _FakeCursor(fetch_results=fetch_results)
    app = _FakeApp(cursor)
    sess = {"company_id": company_id} if company_id else {}
    with mock.patch.object(hr_tools, "session", sess), \
            mock.patch.object(hr_tools, "current_app", app):
        out = json.loads(hr_tools._execute_hr_compare_cohorts(args))
    return out, cursor


# A k-safe and a sub-k cohort (k defaults to 5).
_BIG_A = _agg_row(12, completed=8, active_orders=10, total_spend=24000, avg_completion_days=21)
_BIG_B = _agg_row(9, completed=3, active_orders=9, total_spend=9000, avg_completion_days=35)
_TINY = _agg_row(2, completed=1, active_orders=2, total_spend=4000, avg_completion_days=14)


# ── Executor ─────────────────────────────────────────────────────────────────

class CompareCohortsExecutorTests(unittest.TestCase):
    def test_no_company_returns_error(self):
        out, _ = _run({"cohort_a": {"department": "Salg"}, "cohort_b": {"department": "Marketing"}},
                      company_id=None)
        self.assertIn("error", out)

    def test_missing_selectors_errors(self):
        out, cursor = _run({"cohort_a": "Salg", "cohort_b": "Marketing"})
        self.assertIn("error", out)
        # Never even ran the aggregate when selectors are malformed.
        self.assertEqual(cursor.executed, [])

    def test_two_k_safe_cohorts_compare_with_delta(self):
        out, _ = _run(
            {"cohort_a": {"department": "Salg"}, "cohort_b": {"department": "Marketing"}},
            fetch_results=[_BIG_A, _BIG_B])
        self.assertTrue(out["comparable"])
        self.assertTrue(out["cohort_a"]["k_safe"])
        self.assertTrue(out["cohort_b"]["k_safe"])
        # Both sides carry real metrics...
        self.assertEqual(out["cohort_a"]["completed"], 8)
        self.assertEqual(out["cohort_b"]["completed"], 3)
        # ...and the delta is B minus A.
        self.assertIn("delta_b_minus_a", out)
        self.assertEqual(out["delta_b_minus_a"]["completed"], 3 - 8)
        # spend_per_member is a cohort-level scalar (denominator = member count).
        self.assertEqual(out["cohort_a"]["spend_per_member"], round(24000 / 12, 2))
        # completion_rate = completed / active_orders.
        self.assertEqual(out["cohort_a"]["completion_rate"], round(8 / 10 * 100, 1))

    def test_sub_k_cohort_metrics_are_redacted_and_no_delta(self):
        out, _ = _run(
            {"cohort_a": {"department": "Salg"}, "cohort_b": {"department": "Direktion"}},
            fetch_results=[_BIG_A, _TINY])
        self.assertFalse(out["comparable"])
        self.assertTrue(out["cohort_a"]["k_safe"])
        self.assertFalse(out["cohort_b"]["k_safe"])
        # The sub-k cohort is explicitly marked suppressed and carries NO metrics.
        self.assertTrue(out["cohort_b"].get("suppressed"))
        for key in ("completed", "total_spend", "completion_rate", "spend_per_member"):
            self.assertNotIn(key, out["cohort_b"],
                             f"sub-k cohort leaked metric {key}")
        # No delta is produced when either cohort is sub-k.
        self.assertNotIn("delta_b_minus_a", out)
        self.assertIn("anonymitet", out["summary_da"])

    def test_sub_k_cohort_member_count_is_never_a_metric_leak(self):
        # The member COUNT may show (it is the cohort size, not a per-person value)
        # but the count being < k must still gate the metrics. This guards the
        # "show the label so the model can route the user" behaviour.
        out, _ = _run(
            {"cohort_a": {"department": "Direktion"}, "cohort_b": {"department": "Salg"}},
            fetch_results=[_TINY, _BIG_A])
        self.assertEqual(out["cohort_a"]["members"], 2)
        self.assertTrue(out["cohort_a"]["suppressed"])
        self.assertFalse(out["comparable"])

    def test_both_sub_k_reports_both_too_small(self):
        out, _ = _run(
            {"cohort_a": {"department": "A"}, "cohort_b": {"department": "B"}},
            fetch_results=[_TINY, _agg_row(1)])
        self.assertFalse(out["comparable"])
        self.assertTrue(out["cohort_a"]["suppressed"])
        self.assertTrue(out["cohort_b"]["suppressed"])
        self.assertNotIn("delta_b_minus_a", out)

    def test_company_scoping_and_filters_in_sql(self):
        out, cursor = _run(
            {"cohort_a": {"department": "Salg", "role": "manager", "period_days": 30},
             "cohort_b": {"role": "employee"}},
            fetch_results=[_BIG_A, _BIG_B])
        selects = [(s, p) for (s, p) in cursor.executed if s.strip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 2)
        # Cohort A: company_id pinned first, dept + role filters bound, then period.
        a_params = selects[0][1]
        self.assertEqual(a_params[0], 42)            # company_id pinned
        self.assertIn("Salg", a_params)
        self.assertIn("manager", a_params)
        self.assertIn(30, a_params)                  # period_days
        self.assertIn("cu.company_id = %s", selects[0][0])
        self.assertIn("cu.status = 'active'", selects[0][0])
        # Cohort B: company-scoped too, role-only filter.
        b_params = selects[1][1]
        self.assertEqual(b_params[0], 42)
        self.assertIn("employee", b_params)

    def test_empty_selectors_mean_company_wide(self):
        out, cursor = _run(
            {"cohort_a": {}, "cohort_b": {}},
            fetch_results=[_BIG_A, _BIG_B])
        # No dept/role bound -> the two aggregates differ only by being company-wide;
        # both are k-safe here so a delta is produced.
        self.assertTrue(out["comparable"])
        self.assertEqual(out["cohort_a"]["label"], "Hele virksomheden")

    def test_aggregate_failure_is_guarded(self):
        class _BoomCursor(_FakeCursor):
            def execute(self, sql, params=None):
                raise RuntimeError("db down")
        cursor = _BoomCursor()
        app = _FakeApp(cursor)
        with mock.patch.object(hr_tools, "session", {"company_id": 42}), \
                mock.patch.object(hr_tools, "current_app", app):
            out = json.loads(hr_tools._execute_hr_compare_cohorts(
                {"cohort_a": {"department": "Salg"}, "cohort_b": {"department": "Marketing"}}))
        self.assertIn("error", out)

    def test_fails_closed_when_kanon_unavailable(self):
        # If kanon import is broken, the local floor (k=5) must still suppress the
        # sub-k cohort rather than leak it.
        real_import = __import__

        def _fake_import(name, *a, **kw):
            if name == "kanon":
                raise ImportError("no kanon")
            return real_import(name, *a, **kw)

        cursor = _FakeCursor(fetch_results=[_BIG_A, _TINY])
        app = _FakeApp(cursor)
        with mock.patch.object(hr_tools, "session", {"company_id": 42}), \
                mock.patch.object(hr_tools, "current_app", app), \
                mock.patch("builtins.__import__", side_effect=_fake_import):
            out = json.loads(hr_tools._execute_hr_compare_cohorts(
                {"cohort_a": {"department": "Salg"}, "cohort_b": {"department": "Direktion"}}))
        self.assertEqual(out["k"], 5)
        self.assertFalse(out["comparable"])
        self.assertTrue(out["cohort_b"]["suppressed"])
        for key in ("completed", "total_spend"):
            self.assertNotIn(key, out["cohort_b"])

    def test_uses_configured_k_default(self):
        # The floor follows kanon.K_DEFAULT, so a cohort that is k-safe at k=5 but
        # not at a raised k is suppressed when K_DEFAULT is raised.
        out, _ = _run(
            {"cohort_a": {"department": "Salg"}, "cohort_b": {"department": "Marketing"}},
            fetch_results=[_agg_row(6, completed=2, active_orders=4), _BIG_B])
        # At default k=5, a 6-member cohort is safe.
        self.assertTrue(out["cohort_a"]["k_safe"])
        with mock.patch.object(kanon, "K_DEFAULT", 8):
            out2, _ = _run(
                {"cohort_a": {"department": "Salg"}, "cohort_b": {"department": "Marketing"}},
                fetch_results=[_agg_row(6, completed=2, active_orders=4), _BIG_B])
            self.assertFalse(out2["cohort_a"]["k_safe"])
            self.assertEqual(out2["k"], 8)


# ── Registration + keyword gating ─────────────────────────────────────────────

class RegistryTests(unittest.TestCase):
    def test_tool_in_schema_and_router(self):
        schema_names = {(_t.get("function") or {}).get("name") for _t in hr_tools.HR_TOOLS}
        self.assertIn("hr_compare_cohorts", schema_names)
        # Router reachable: a bad-json path still returns a JSON error.
        out = hr_tools.execute_hr_tool(
            mock.Mock(function=mock.Mock(name="x", arguments="{bad")))
        self.assertIn("error", json.loads(out))

    def test_read_tool_is_parallel_safe(self):
        # Comparison is read-only — safe to run in parallel with other reads.
        self.assertTrue(is_parallel_safe("hr_compare_cohorts"))

    def test_keyword_gated_and_forced(self):
        tools, meta = get_hr_tool_selection(
            company_id=1, user_query="Sammenlign Salg vs Marketing på gennemførselsrate")
        names = {tool_name(t) for t in tools}
        self.assertIn("hr_compare_cohorts", names)
        self.assertEqual(meta["forced_tool"], "hr_compare_cohorts")

    def test_versus_phrasing_selects_tool(self):
        tools, _ = get_hr_tool_selection(
            company_id=1, user_query="Hvordan klarer ledere sig mod menige medarbejdere?")
        self.assertIn("hr_compare_cohorts", {tool_name(t) for t in tools})

    def test_excluded_without_company(self):
        tools, _ = get_hr_tool_selection(
            company_id=None, user_query="Sammenlign Salg vs Marketing")
        self.assertNotIn("hr_compare_cohorts", {tool_name(t) for t in tools})

    def test_not_offered_off_topic(self):
        tools, _ = get_hr_tool_selection(
            company_id=1, user_query="Hvor meget budget har vi tilbage?")
        self.assertNotIn("hr_compare_cohorts", {tool_name(t) for t in tools})


if __name__ == "__main__":
    unittest.main()
