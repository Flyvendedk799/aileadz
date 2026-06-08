"""get_workforce_risk advisor tool with explicit aggregate / k-anon mode (plan #12).

The richest predictive logic the codebase owns — churn risk, at-risk-by-gap
employees, trending unmet learning demand in insights_engine.get_predictive_data
— ran only on the roi page route; no HR tool wrapped it. BUT get_predictive_data
returns NAMED INDIVIDUALS (username / user_id / job_title / department) with NO
suppression. On a page route HR views its own people, but as an AI tool the LLM
would verbalise identifiable at-risk/churn employees with no k-anon floor — a
GDPR / k-anonymity hard-constraint violation (HR_VALUE_PLAN risk register #314).

This initiative adds:
  * insights_engine.aggregate_workforce_risk — a PURE, NET-NEW k-anon floor that
    turns the named rows into department/skill-level COUNTS with sub-k cohorts
    suppressed; names only behind an explicit + manager-gated drill-down, and even
    then only for departments whose cohort is itself k-safe.
  * hr_tools.get_workforce_risk — the read-only advisor tool wrapping it, with
    the doubly-gated (flag + manager role) drill-down.
  * hr_tools.hr_explain_insights — a thin read-only wrapper over
    generate_company_insights so the advisor can explain the proactive cards.
  * registration + keyword gating in ai_tool_registry.

Tests run WITHOUT a live DB / OpenAI: the aggregator is pure; the executors are
exercised with a patched session + a patched engine; the registry checks are
pure-selection.
"""

import json
import unittest
from unittest import mock

import db_compat  # noqa: F401  (installs pymysql->MySQLdb shim)
import insights_engine
import hr_tools
from ai_tool_registry import get_hr_tool_selection, tool_name


# ── A representative raw get_predictive_data payload: two k-safe cohorts (Sales
# churn, Engineering skill gap) and several sub-k cohorts that MUST be suppressed
# (a solo Legal churn, a solo Finance gap, a single-person search term). ─────────
def _raw_predictions():
    return {
        "churn_risk": [
            {"user_id": i, "username": f"sales{i}", "department": "Sales",
             "job_title": "Rep", "days_inactive": 40 + i}
            for i in range(6)
        ] + [
            {"user_id": 99, "username": "lonewolf", "department": "Legal",
             "job_title": "Counsel", "days_inactive": 70},
        ],
        "at_risk_employees": [
            {"username": f"eng{i}", "department": "Engineering", "job_title": "Dev",
             "skill_name": "Python", "current_level": 2, "target_level": 5, "gap": 3}
            for i in range(7)
        ] + [
            {"username": "cfo", "department": "Finance", "job_title": "CFO",
             "skill_name": "IFRS", "current_level": 1, "target_level": 5, "gap": 4},
        ],
        "trending_courses": [
            {"query_text": "GDPR kursus", "cnt": 9},
            {"query_text": "enkeltperson søgning", "cnt": 1},  # sub-k -> suppress
        ],
    }


# ── 1) Pure aggregator: net-new k-anon suppression ───────────────────────────

class AggregateWorkforceRiskTests(unittest.TestCase):
    def test_default_is_aggregate_only_with_no_names(self):
        agg = insights_engine.aggregate_workforce_risk(_raw_predictions())
        flat = json.dumps(agg, default=str)
        # No individual names anywhere in the default (aggregate-first) payload.
        for name in ("sales0", "sales5", "lonewolf", "eng0", "cfo"):
            self.assertNotIn(name, flat, f"name {name} leaked into aggregate output")
        # No per-section "individuals" key without an explicit drill-down.
        self.assertNotIn("individuals", agg["churn_risk"])
        self.assertNotIn("individuals", agg["skill_gap_risk"])

    def test_sub_k_departments_and_skills_are_suppressed(self):
        agg = insights_engine.aggregate_workforce_risk(_raw_predictions())
        churn_depts = {r["department"] for r in agg["churn_risk"]["by_department"]}
        gap_depts = {r["department"] for r in agg["skill_gap_risk"]["by_department"]}
        gap_skills = {r["skill"] for r in agg["skill_gap_risk"]["by_skill"]}
        # k-safe cohorts survive...
        self.assertIn("Sales", churn_depts)
        self.assertIn("Engineering", gap_depts)
        self.assertIn("Python", gap_skills)
        # ...sub-k cohorts are dropped (naming the sole member would re-identify).
        self.assertNotIn("Legal", churn_depts)
        self.assertNotIn("Finance", gap_depts)
        self.assertNotIn("IFRS", gap_skills)
        self.assertGreaterEqual(agg["anon"]["suppressed_groups"], 1)
        self.assertIn("anonymitet", agg["anon"]["note_da"])

    def test_company_totals_are_full_aggregates_not_suppressed(self):
        # Totals are inherently aggregated (safe), so they reflect EVERY at-risk
        # person even when small departments are hidden from the breakdown.
        agg = insights_engine.aggregate_workforce_risk(_raw_predictions())
        self.assertEqual(agg["churn_risk"]["total"], 7)       # 6 Sales + 1 Legal
        self.assertEqual(agg["skill_gap_risk"]["total"], 8)   # 7 Eng + 1 Finance

    def test_sub_k_search_term_is_suppressed(self):
        agg = insights_engine.aggregate_workforce_risk(_raw_predictions())
        terms = {t["query"] for t in agg["trending_demand"]["terms"]}
        self.assertIn("GDPR kursus", terms)
        self.assertNotIn("enkeltperson søgning", terms)

    def test_drill_down_reveals_names_only_for_k_safe_departments(self):
        agg = insights_engine.aggregate_workforce_risk(_raw_predictions(), drill_down=True)
        flat = json.dumps(agg, default=str)
        # k-safe department members are named...
        self.assertIn("sales0", flat)
        self.assertIn("eng0", flat)
        # ...but the sole members of sub-k departments are STILL never named.
        self.assertNotIn("lonewolf", flat)
        self.assertNotIn("cfo", flat)
        self.assertIn("individuals", agg["churn_risk"])
        self.assertIn("individuals", agg["skill_gap_risk"])

    def test_drill_limit_caps_named_individuals(self):
        agg = insights_engine.aggregate_workforce_risk(
            _raw_predictions(), drill_down=True, drill_limit=2)
        self.assertLessEqual(len(agg["churn_risk"]["individuals"]), 2)
        self.assertLessEqual(len(agg["skill_gap_risk"]["individuals"]), 2)

    def test_fail_closed_when_kanon_module_unavailable(self):
        # If kanon is missing the engine must FAIL CLOSED — suppress sub-k cohorts
        # with a local floor rather than leak them.
        orig = insights_engine._kanon
        insights_engine._kanon = None
        try:
            agg = insights_engine.aggregate_workforce_risk(
                {"churn_risk": [{"username": "solo", "department": "Legal",
                                 "days_inactive": 10}],
                 "at_risk_employees": [],
                 "trending_courses": []},
                k=5)
            flat = json.dumps(agg, default=str)
            self.assertNotIn("Legal", flat)
            self.assertNotIn("solo", flat)
            self.assertEqual(agg["k"], 5)
            self.assertGreaterEqual(agg["anon"]["suppressed_groups"], 1)
        finally:
            insights_engine._kanon = orig

    def test_malformed_input_degrades_to_empty_never_raises(self):
        for bad in (None, [], "x", 5, {"churn_risk": None}):
            agg = insights_engine.aggregate_workforce_risk(bad)
            self.assertEqual(agg["churn_risk"]["total"], 0)
            self.assertEqual(agg["skill_gap_risk"]["total"], 0)
            self.assertEqual(agg["trending_demand"]["terms"], [])


# ── 2) get_workforce_risk executor: aggregate-first + drill-down gating ───────

class _FakeApp:
    def _get_current_object(self):
        return self


class GetWorkforceRiskExecutorTests(unittest.TestCase):
    def _run(self, args, *, company_id=42, is_manager=False,
             predictions=None, raise_predict=False):
        if predictions is None:
            predictions = _raw_predictions()

        def _fake_predict(app, cid):
            if raise_predict:
                raise RuntimeError("db down")
            return predictions

        sess = {"company_id": company_id} if company_id else {}
        ctx = mock.Mock(is_manager=is_manager)
        with mock.patch.object(hr_tools, "session", sess), \
                mock.patch.object(hr_tools, "current_app", _FakeApp()), \
                mock.patch("insights_engine.get_predictive_data",
                           side_effect=_fake_predict), \
                mock.patch("order_service.OrderContext.from_session",
                           return_value=ctx):
            return json.loads(hr_tools._execute_get_workforce_risk(args))

    def test_no_company_returns_error(self):
        out = self._run({}, company_id=None)
        self.assertIn("error", out)

    def test_default_returns_aggregates_without_names(self):
        out = self._run({})
        self.assertTrue(out["has_data"])
        self.assertFalse(out["drill_down_applied"])
        flat = json.dumps(out, default=str)
        for name in ("sales0", "lonewolf", "eng0", "cfo"):
            self.assertNotIn(name, flat)
        # k-safe department breakdown present; totals present.
        self.assertEqual(out["churn_risk"]["total"], 7)
        self.assertIn("anonymitet", out["anon_note"])

    def test_drill_down_denied_for_non_manager(self):
        out = self._run({"drill_down": True}, is_manager=False)
        self.assertFalse(out["drill_down_applied"])
        self.assertTrue(out.get("drill_down_denied"))
        flat = json.dumps(out, default=str)
        # No named individuals whatsoever for a non-manager, even with the flag.
        for name in ("sales0", "lonewolf", "eng0", "cfo"):
            self.assertNotIn(name, flat)

    def test_drill_down_granted_for_manager_only_k_safe_names(self):
        out = self._run({"drill_down": True}, is_manager=True)
        self.assertTrue(out["drill_down_applied"])
        self.assertNotIn("drill_down_denied", out)
        flat = json.dumps(out, default=str)
        # Manager sees k-safe-department names...
        self.assertIn("sales0", flat)
        self.assertIn("eng0", flat)
        # ...but solo sub-k members stay hidden even from a manager.
        self.assertNotIn("lonewolf", flat)
        self.assertNotIn("cfo", flat)

    def test_predictive_failure_is_guarded(self):
        out = self._run({}, raise_predict=True)
        self.assertIn("error", out)
        self.assertFalse(out["has_data"])

    def test_no_signals_reports_clean_state(self):
        empty = {"churn_risk": [], "at_risk_employees": [], "trending_courses": []}
        out = self._run({}, predictions=empty)
        self.assertFalse(out["has_data"])
        self.assertIn("Ingen", out["summary_da"])


# ── 3) hr_explain_insights executor: read-only, severity filter ──────────────

class ExplainInsightsExecutorTests(unittest.TestCase):
    def _run(self, args, *, company_id=42, insights=None, raise_gen=False):
        def _fake_gen(app, cid):
            if raise_gen:
                raise RuntimeError("insights down")
            return insights or []

        sess = {"company_id": company_id} if company_id else {}
        with mock.patch.object(hr_tools, "session", sess), \
                mock.patch.object(hr_tools, "current_app", _FakeApp()), \
                mock.patch("insights_engine.generate_company_insights",
                           side_effect=_fake_gen):
            return json.loads(hr_tools._execute_hr_explain_insights(args))

    def test_no_company_returns_error(self):
        self.assertIn("error", self._run({}, company_id=None))

    def test_returns_insights_and_counts_actionable(self):
        ins = [
            {"type": "trending_topic", "severity": "info", "title": "A", "body": "b", "data": {}},
            {"type": "low_satisfaction", "severity": "warning", "title": "C", "body": "d", "data": {}},
        ]
        out = self._run({}, insights=ins)
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["actionable"], 1)

    def test_severity_filter_narrows_results(self):
        ins = [
            {"type": "t", "severity": "info", "title": "A", "body": "b", "data": {}},
            {"type": "u", "severity": "warning", "title": "C", "body": "d", "data": {}},
        ]
        out = self._run({"severity": "warning"}, insights=ins)
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["insights"][0]["severity"], "warning")

    def test_generation_failure_is_guarded(self):
        out = self._run({}, raise_gen=True)
        self.assertIn("error", out)
        self.assertEqual(out["insights"], [])

    def test_empty_insights_is_clean_state(self):
        out = self._run({}, insights=[])
        self.assertEqual(out["total"], 0)
        self.assertIn("endnu ikke nok", out["summary_da"])


# ── 4) Registration + keyword gating ─────────────────────────────────────────

class RegistryTests(unittest.TestCase):
    def test_both_tools_registered_in_router(self):
        # The router maps names to executors; both new tools must be reachable.
        out = hr_tools.execute_hr_tool(
            mock.Mock(function=mock.Mock(name="x", arguments="{not json")))
        # bad json path still returns a JSON error (router is wired) — sanity only.
        self.assertIn("error", json.loads(out))
        # Schemas exist in HR_TOOLS.
        schema_names = {(_t.get("function") or {}).get("name") for _t in hr_tools.HR_TOOLS}
        self.assertIn("get_workforce_risk", schema_names)
        self.assertIn("hr_explain_insights", schema_names)

    def test_workforce_risk_is_keyword_gated_and_forced(self):
        tools, meta = get_hr_tool_selection(
            company_id=1, user_query="Hvad skal jeg handle på denne uge? Vis tidlig advarsel")
        names = {tool_name(t) for t in tools}
        self.assertIn("get_workforce_risk", names)
        self.assertEqual(meta["forced_tool"], "get_workforce_risk")

    def test_explain_insights_is_keyword_gated(self):
        tools, _ = get_hr_tool_selection(
            company_id=1, user_query="Giv mig overblik over indsigterne og advarslerne")
        self.assertIn("hr_explain_insights", {tool_name(t) for t in tools})

    def test_company_required_excludes_both_without_company(self):
        tools, _ = get_hr_tool_selection(
            company_id=None, user_query="tidlig advarsel og indsigter")
        names = {tool_name(t) for t in tools}
        self.assertNotIn("get_workforce_risk", names)
        self.assertNotIn("hr_explain_insights", names)

    def test_workforce_risk_not_offered_off_topic(self):
        # A pure budget question must not pull in the predictive risk tool.
        tools, _ = get_hr_tool_selection(
            company_id=1, user_query="Hvor meget budget har vi tilbage?")
        self.assertNotIn("get_workforce_risk", {tool_name(t) for t in tools})


if __name__ == "__main__":
    unittest.main()
