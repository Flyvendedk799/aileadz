"""Tests for the AI co-pilot upgrade.

Covers the cross-surface tool-calling, guidance, and combined-experience work:
  * depth-aware profile completeness (weighted_pct / weakest / strength /
    target_role) — keeping the 8-section pct/total/missing contract intact
  * budget min-variant filtering fix + language/difficulty facets (both filter
    paths)
  * analytical compare_courses (per-axis winners + verdict)
  * verifiable per-card match_reason
  * get_learning_context contract (profile/budget/agreements/completed keys)
  * open_in_app cross-surface action tool
  * save/get learning-path executors (DB mocked)
  * registry: open_in_app always-on + paraphrase/English reachability fallback
  * grounding price token-boundary + cents-aware fix
  * sse_events vocabulary + formatter
  * confirm_store in-process fallback (store/pop, session binding, double-pop)
  * _fallback_suggestions guidance guarantee

Offline: no OpenAI, no MySQL. Run with the safe env prefix (see
reference_aileadz_local_verify): SANDBOX=1 MYSQL_HOST=127.0.0.1 ... OPENAI_API_KEY=sk-test.
"""
import json
import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import app1.tools as tools  # noqa: E402
import app1.user_profile_db as DB  # noqa: E402
import grounding as g  # noqa: E402
from app1 import sse_events  # noqa: E402
from app1 import confirm_store  # noqa: E402
import app1.agent as agent  # noqa: E402
from ai_tool_registry import (  # noqa: E402
    get_employee_tool_selection, _semantic_tool_fallback,
)


def _tc(name, args):
    """Minimal tool_call object for execute_tool()."""
    return tools._SimpleToolCall(name, json.dumps(args)) if hasattr(tools, "_SimpleToolCall") else _Shim(name, args)


class _Shim:
    def __init__(self, name, args):
        self.id = ""
        self.function = type("_F", (), {"name": name, "arguments": json.dumps(args)})()


def _prod(handle, title, prices, language=None, difficulty=None, duration=None, cert=None, tags=None):
    variants = [{"price": str(p), "option1": "København", "option2": "1. maj 2026"} for p in prices]
    meta = {}
    if language:
        meta["language"] = language
    if difficulty:
        meta["difficulty"] = difficulty
    if duration:
        meta["duration_days"] = duration
    if cert:
        meta["certification"] = cert
    return {
        "handle": handle, "title": title, "vendor": "V", "product_type": "Kursus",
        "ai_summary": f"Et kursus: {title}", "tags": tags or [],
        "variants": variants, "structured_metadata": meta,
    }


# ── Depth-aware completeness ──────────────────────────────────────────────

class CompletenessDepthTests(unittest.TestCase):
    def _full(self):
        return {"headline": "Erfaren projektleder", "skills": [{"name": "Python", "level": "ekspert"}],
                "experience": [{"id": 1}, {"id": 2}], "education": [{"id": 1}],
                "certifications": [{"id": 1}], "languages": [{"id": 1}],
                "goals": "blive bedre", "learning_goals": [{"id": 1}], "preferred_format": "online",
                "preferred_location": "København", "target_role": "IT-chef"}

    def test_binary_contract_unchanged(self):
        # The 8-section pct/total/missing contract the profiler relies on.
        c = DB.profile_completeness("u", profile=self._full())
        self.assertEqual(c["total"], 8)
        self.assertEqual(c["pct"], 100)
        self.assertEqual(c["missing"], [])

    def test_depth_fields_present(self):
        c = DB.profile_completeness("u", profile=self._full())
        self.assertIn("weighted_pct", c)
        self.assertIn("weakest", c)
        self.assertEqual(c["target_role"], "IT-chef")
        for s in c["sections"]:
            self.assertIn("strength", s)
            self.assertTrue(0.0 <= s["strength"] <= 1.0)

    def test_weakest_is_lowest_strength_section(self):
        p = {"headline": "x", "skills": [{"name": "a"}], "experience": [], "education": [],
             "certifications": [], "languages": [], "goals": "", "learning_goals": [], "preferred_format": ""}
        c = DB.profile_completeness("u", profile=p)
        # Empty sections have strength 0 → weakest is one of them.
        weak_section = next(s for s in c["sections"] if s["key"] == c["weakest"])
        self.assertEqual(weak_section["strength"], 0.0)

    def test_one_shallow_item_is_weaker_than_three(self):
        shallow = DB.profile_completeness("u", profile={"experience": [{"id": 1}]})
        deep = DB.profile_completeness("u", profile={"experience": [{"id": 1}, {"id": 2}, {"id": 3}]})
        s1 = next(s["strength"] for s in shallow["sections"] if s["key"] == "experience")
        s3 = next(s["strength"] for s in deep["sections"] if s["key"] == "experience")
        self.assertLess(s1, s3)


# ── Budget min-variant + language/difficulty facets ───────────────────────

class FilterFacetTests(unittest.TestCase):
    def setUp(self):
        tools.set_search_context()

    def tearDown(self):
        tools.set_search_context()

    def test_budget_uses_cheapest_variant_not_first(self):
        # First variant 12000 (over ceiling) but a 4000 variant is bookable → kept.
        prods = [_prod("multi", "Multi", [12000, 4000])]
        kept = tools._filter_products_by_constraints(prods, price_max=5000)
        self.assertEqual([p["handle"] for p in kept], ["multi"])

    def test_budget_excludes_when_no_variant_in_window(self):
        prods = [_prod("dyr", "Dyr", [12000, 9000])]
        self.assertEqual(tools._filter_products_by_constraints(prods, price_max=5000), [])

    def test_unknown_price_not_excluded(self):
        prods = [{"handle": "ask", "title": "Ask", "variants": [{"price": "Pris på forespørgsel"}]}]
        self.assertEqual(len(tools._filter_products_by_constraints(prods, price_max=5000)), 1)

    def test_language_facet(self):
        prods = [_prod("en", "EN", [1000], language="engelsk"), _prod("da", "DA", [1000], language="dansk")]
        self.assertEqual([p["handle"] for p in tools._filter_products_by_constraints(prods, language="dansk")], ["da"])
        # 'begge' matches any request; unknown language is not excluded.
        self.assertEqual(len(tools._filter_products_by_constraints(
            [_prod("b", "B", [1000], language="begge")], language="engelsk")), 1)

    def test_difficulty_facet(self):
        prods = [_prod("beg", "Beg", [1000], difficulty="beginner"), _prod("adv", "Adv", [1000], difficulty="advanced")]
        self.assertEqual([p["handle"] for p in tools._filter_products_by_constraints(prods, difficulty="advanced")], ["adv"])
        # Danish alias maps to canonical difficulty.
        self.assertEqual([p["handle"] for p in tools._filter_products_by_constraints(prods, difficulty="begynder")], ["beg"])

    def test_hard_filter_path_also_uses_min_variant(self):
        p = _prod("multi", "Multi", [12000, 4000])
        self.assertTrue(tools._product_passes_hard_filters(p, price_max=5000))
        filtered, active = tools._apply_hard_filters([p], price_max=5000, language="dansk")
        self.assertIn("language", active)


# ── Analytical comparison ─────────────────────────────────────────────────

class ComparisonAnalysisTests(unittest.TestCase):
    def test_winners_and_verdict(self):
        comps = [
            {"handle": "a", "title": "A", "price_num": 4000, "duration_days": 3, "certification": "PRINCE2", "soonest_date": "1. maj"},
            {"handle": "b", "title": "B", "price_num": 9000, "duration_days": 1, "certification": "", "soonest_date": "3. juni"},
        ]
        a = tools._comparison_analysis(comps)
        self.assertEqual(a["winners"]["cheapest"]["handle"], "a")
        self.assertEqual(a["winners"]["shortest"]["handle"], "b")
        self.assertEqual(a["winners"]["certification"]["handle"], "a")
        self.assertTrue(a["verdict"])

    def test_handles_missing_axes(self):
        a = tools._comparison_analysis([{"handle": "a", "title": "A"}, {"handle": "b", "title": "B"}])
        self.assertEqual(a["winners"], {})


# ── match_reason ──────────────────────────────────────────────────────────

class MatchReasonTests(unittest.TestCase):
    def test_query_term_and_attribute(self):
        r = tools._course_match_reason(
            {"title": "PRINCE2 Projektledelse", "tags": ["projektledelse"], "certification": "PRINCE2"},
            query_terms=["projektledelse"])
        self.assertIn("projektledelse", r)
        self.assertIn("certificering", r)

    def test_no_match_is_empty(self):
        r = tools._course_match_reason({"title": "Excel", "tags": []}, query_terms=["jura"])
        self.assertEqual(r, "")

    def test_skips_generic_stopwords(self):
        r = tools._course_match_reason({"title": "Kursus i ledelse", "tags": []}, query_terms=["kursus"])
        self.assertNotIn("kursus", r)


# ── get_learning_context contract ─────────────────────────────────────────

class LearningContextContractTests(unittest.TestCase):
    def test_returns_all_advertised_sections(self):
        out = json.loads(tools._execute_get_learning_context({}, None))
        for key in ("profile", "completed_courses", "company_budget", "supplier_agreements",
                    "shown_product_handles", "open_orders"):
            self.assertIn(key, out)


# ── open_in_app ───────────────────────────────────────────────────────────

class OpenInAppTests(unittest.TestCase):
    def test_open_compare(self):
        out = json.loads(tools._execute_open_in_app({"action": "open_compare", "handles": ["a", "b"]}))
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["target"], "/catalog?compare=a,b")

    def test_open_profile_section(self):
        out = json.loads(tools._execute_open_in_app({"action": "open_profile", "section": "skills"}))
        self.assertEqual(out["target"], "/profile#skills")

    def test_open_catalog_query(self):
        out = json.loads(tools._execute_open_in_app({"action": "open_catalog", "query": "ledelse"}))
        self.assertEqual(out["target"], "/catalog?q=ledelse")

    def test_unknown_action_errors(self):
        out = json.loads(tools._execute_open_in_app({"action": "nuke_everything"}))
        self.assertEqual(out["status"], "error")

    def test_compare_needs_two(self):
        out = json.loads(tools._execute_open_in_app({"action": "open_compare", "handles": ["a"]}))
        self.assertEqual(out["status"], "error")

    def test_view_product_missing_handle(self):
        out = json.loads(tools._execute_open_in_app({"action": "view_product"}))
        self.assertEqual(out["status"], "error")


# ── Learning-path executors (DB mocked) ───────────────────────────────────

class LearningPathExecutorTests(unittest.TestCase):
    def test_save_requires_login(self):
        out = json.loads(tools._execute_save_learning_path({"title": "X", "steps": [{}]}, None))
        self.assertEqual(out["status"], "error")

    def test_save_persists(self):
        with mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.save_learning_path", return_value=42) as sv:
            out = json.loads(tools._execute_save_learning_path(
                {"title": "Min sti", "steps": [{"order": 1, "topic": "ledelse"}]}, "eva"))
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["path_id"], 42)
        sv.assert_called_once()

    def test_get_lists_paths(self):
        with mock.patch("app1.user_profile_db.ensure_tables"), \
                mock.patch("app1.user_profile_db.get_learning_paths", return_value=[{"id": 1, "title": "A", "steps": []}]):
            out = json.loads(tools._execute_get_learning_path({}, "eva"))
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["count"], 1)


# ── Registry reachability ─────────────────────────────────────────────────

class RegistryReachabilityTests(unittest.TestCase):
    def test_open_in_app_always_on(self):
        for li in (True, False):
            _, meta = get_employee_tool_selection(
                logged_in=li, company_id=None, intent="discovery", user_query="hej", shown_count=0)
            self.assertIn("open_in_app", meta["tool_names"])

    def test_english_discount_paraphrase_reaches_tool(self):
        self.assertIn("get_negotiated_discount",
                      _semantic_tool_fallback("what is the discount with the agreement", already=set()))

    def test_english_prerequisites_paraphrase(self):
        self.assertIn("check_course_prerequisites",
                      _semantic_tool_fallback("show me the prerequisites for this", already=set()))

    def test_already_selected_not_re_added(self):
        self.assertNotIn("get_negotiated_discount",
                         _semantic_tool_fallback("discount please", already={"get_negotiated_discount"}))

    def test_learning_path_tools_on_english_query(self):
        _, meta = get_employee_tool_selection(
            logged_in=True, company_id=None, intent="discovery",
            user_query="make me a learning path", shown_count=0)
        self.assertIn("suggest_learning_path", meta["tool_names"])
        self.assertIn("save_learning_path", meta["tool_names"])

    def test_cv_summary_reachable_on_profile_query(self):
        # Guards the regression where show_cv_summary was defined + dispatched
        # but never added to the per-turn menu (so the model could never call it).
        _, meta = get_employee_tool_selection(
            logged_in=True, company_id=None, intent="profile_update",
            user_query="hvad har jeg på mit cv?", shown_count=0)
        self.assertIn("show_cv_summary", meta["tool_names"])

    def test_cv_summary_requires_login(self):
        _, meta = get_employee_tool_selection(
            logged_in=False, company_id=None, intent="profile_update",
            user_query="vis mit cv", shown_count=0)
        self.assertNotIn("show_cv_summary", meta["tool_names"])

    def test_skill_gaps_reachable_on_gap_query(self):
        # The gap card is the grounded bridge to recommendations — it must reach
        # the menu on "what am I missing" type queries (the 3-step reachability
        # rule: schema + executor + menu).
        _, meta = get_employee_tool_selection(
            logged_in=True, company_id=True, intent="profile_update",
            user_query="hvad mangler jeg for at blive data analyst?", shown_count=0)
        self.assertIn("show_skill_gaps", meta["tool_names"])

    def test_skill_gaps_english_paraphrase(self):
        self.assertIn("show_skill_gaps",
                      _semantic_tool_fallback("what skills do i need", already=set()))

    def test_skill_gaps_requires_login(self):
        _, meta = get_employee_tool_selection(
            logged_in=False, company_id=None, intent="profile_update",
            user_query="hvad mangler jeg", shown_count=0)
        self.assertNotIn("show_skill_gaps", meta["tool_names"])

    def test_mindmap_preview_reachable_on_memory_query(self):
        _, meta = get_employee_tool_selection(
            logged_in=True, company_id=None, intent="discovery",
            user_query="hvad husker du om mig?", shown_count=0)
        self.assertIn("show_mindmap_preview", meta["tool_names"])

    def test_mindmap_preview_english_paraphrase(self):
        self.assertIn("show_mindmap_preview",
                      _semantic_tool_fallback("what do you know about me", already=set()))

    def test_fallback_can_be_disabled(self):
        with mock.patch.dict(os.environ, {"AI_TOOL_SEMANTIC_FALLBACK": "0"}):
            _, meta = get_employee_tool_selection(
                logged_in=True, company_id=True, intent="discovery",
                user_query="what is the discount", shown_count=0)
            self.assertNotIn("get_negotiated_discount", meta["tool_names"])


# ── Grounding price token-boundary fix ────────────────────────────────────

class GroundingPriceTests(unittest.TestCase):
    def _ev(self, price):
        return [{"name": "catalog_search", "output": json.dumps({"results": [{"title": "K", "price": price}]})}]

    def test_substring_price_not_supported(self):
        rep = g.claims_supported("Kurset koster kr 5.000.", self._ev("kr 15.000"))
        self.assertTrue(any(u["type"] == "price" for u in rep["unsupported"]))

    def test_cents_aware_supported(self):
        rep = g.claims_supported("Prisen er kr 15.000.", self._ev("15.000,00"))
        self.assertTrue(any(s["type"] == "price" for s in rep["supported"]))
        self.assertFalse(rep["unsupported"])

    def test_canon_amount(self):
        self.assertEqual(g._canon_amount("kr 5.000"), "5000")
        self.assertEqual(g._canon_amount("15.000,00"), "15000")
        self.assertEqual(g._canon_amount("0,00"), "0")
        self.assertEqual(g._canon_amount("abc"), "")


# ── SSE vocabulary ────────────────────────────────────────────────────────

class SseEventsTests(unittest.TestCase):
    def test_new_events_known(self):
        for e in (sse_events.UI_ACTION, sse_events.COMPARISON_CARD, sse_events.LEARNING_PATH_CARD):
            self.assertIn(e, sse_events.KNOWN_EVENT_TYPES)

    def test_formatter_shape_and_utf8(self):
        frame = sse_events.sse(sse_events.NOTICE, content="på dansk æøå")
        self.assertTrue(frame.startswith("data: "))
        self.assertTrue(frame.endswith("\n\n"))
        payload = json.loads(frame[len("data: "):].strip())
        self.assertEqual(payload["type"], "notice")
        self.assertEqual(payload["content"], "på dansk æøå")

    def test_ui_actions_match_tool_enum(self):
        # open_in_app's enum must be a subset of the shared UI_ACTIONS list.
        self.assertIn("view_product", sse_events.UI_ACTIONS)
        self.assertIn("open_compare", sse_events.UI_ACTIONS)


# ── confirm_store in-process fallback ─────────────────────────────────────

class ConfirmStoreTests(unittest.TestCase):
    def setUp(self):
        confirm_store.clear_all()

    def test_store_and_pop(self):
        tok = confirm_store.store_pending("sess-1", "employee", "manage_my_order", {"order_id": "x"})
        entry = confirm_store.pop_pending("sess-1", tok)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["tool_name"], "manage_my_order")
        self.assertEqual(entry["args"], {"order_id": "x"})

    def test_wrong_session_rejected(self):
        tok = confirm_store.store_pending("sess-1", "employee", "t", {})
        self.assertIsNone(confirm_store.pop_pending("sess-OTHER", tok))

    def test_double_pop_is_none(self):
        tok = confirm_store.store_pending("sess-1", "employee", "t", {})
        self.assertIsNotNone(confirm_store.pop_pending("sess-1", tok))
        self.assertIsNone(confirm_store.pop_pending("sess-1", tok))

    def test_unknown_token(self):
        self.assertIsNone(confirm_store.pop_pending("sess-1", "deadbeef"))


# ── Regression guard: learning_path_card not shadowed (review finding) ─────

class LearningPathEventRoutingTests(unittest.TestCase):
    """The adversarial review caught that suggest_learning_path, being in the
    _PRODUCT_CARD_TOOLS tuple, was matched by the earlier `if fn in
    _PRODUCT_CARD_TOOLS` branch and so could never reach the
    learning_path_card `elif` (if/elif chain). It also returns `steps`, not
    `results`, so the product-card branch was a silent no-op. Guard the fix at
    the source level (the routing lives in a closure, so this mirrors the repo's
    gdpr/drift source-inspection tests)."""

    def setUp(self):
        with open(os.path.join(REPO_ROOT, "app1", "agent.py"), encoding="utf-8") as f:
            self.src = f.read()

    def test_suggest_learning_path_not_in_product_card_tools(self):
        import re
        m = re.search(r"_PRODUCT_CARD_TOOLS\s*=\s*\((.*?)\)", self.src, re.DOTALL)
        self.assertIsNotNone(m, "_PRODUCT_CARD_TOOLS tuple not found")
        self.assertNotIn("suggest_learning_path", m.group(1),
                         "suggest_learning_path must NOT be in _PRODUCT_CARD_TOOLS or it shadows the learning_path_card branch")

    def test_learning_path_branch_handles_suggest(self):
        self.assertIn('elif fn in ("suggest_learning_path"', self.src.replace("'", '"'))


# ── Fallback suggestions guidance guarantee ───────────────────────────────

class FallbackSuggestionsTests(unittest.TestCase):
    def test_profiler_targets_missing(self):
        s = agent._fallback_suggestions(mode="profiler", completeness={"missing": ["Erfaring"], "weighted_pct": 30})
        self.assertTrue(any("erfaring" in x.lower() for x in s))

    def test_cards_shown_offers_compare(self):
        s = agent._fallback_suggestions(mode="default", had_cards=True, logged_in=True)
        self.assertTrue(len(s) >= 2)

    def test_default_never_empty(self):
        self.assertTrue(agent._fallback_suggestions(mode="default", had_cards=False, logged_in=False))


if __name__ == "__main__":
    unittest.main()
