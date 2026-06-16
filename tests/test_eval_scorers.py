"""EV-02: Unit tests + drift guards for the golden-set eval scorers.

Covers ai_eval/scorers.py + ai_eval/run_eval.py:

  * REAL grounding (grounded_real → grounding.grounding_disclaimer): a
    supported price/date PASSes, a fabricated one FAILs, a user-echoed budget
    is never a violation, and "no checkable claims" lands in an applies=True
    PASS bucket (nothing-to-hallucinate) so grounding_pct is ALWAYS numeric.
  * refusal_correct's per-case must_not_contain lists (the fulfil-then-redirect
    loophole: deliver the lasagne recipe AND mention kurser used to PASS).
  * confirmation_before_order: "neither asked nor completed" is now a FAIL.
  * retrieval_relevant: precision-based (matched/len(cards) >= 0.5), honors
    expected_card_count / cards_price_max / cards_location_contains and emits
    the per-case precision the runner aggregates as retrieval_precision_pct.
  * tool_selection_correct / profile_event_present / system_prompt_leaked.
  * score_case wiring + _passed.
  * run_eval.aggregate's retrieval_precision_pct + numeric grounding_pct.
  * run_eval.read_session_telemetry: tool_jsons populated from step=='tool_result'
    debug-log entries (SQLite memory store, no MySQL needed).
  * DRIFT GUARDS: every _SYSTEM_PROMPT_FINGERPRINTS entry still appears
    (case/whitespace/æøå-folded) in app1.agent's system prompt constants, and
    _CATALOG_TOOLS is a subset of the real tool names in app1.tools.

Fully offline: no OPENAI_API_KEY (judge stays disabled), no MySQL, no network.
"""
import json
import os
import re
import sys
import unittest
from unittest.mock import patch

# Make the project root importable when run from anywhere.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO_ROOT)

# Safe offline env BEFORE importing the app modules (the SANDBOX recipe).
_SAFE_ENV = {
    "SANDBOX": "1",
    "AI_WARMUP_ON_IMPORT": "0",
    "SCHEDULER_OPPORTUNISTIC": "0",
    "MYSQL_HOST": "127.0.0.1",
    "MYSQL_PORT": "3306",
    "MYSQL_USER": "none",
    "MYSQL_PASSWORD": "none",
    "MYSQL_DB": "none",
    "OPENAI_API_KEY": "sk-test",
}
for _k, _v in _SAFE_ENV.items():
    os.environ.setdefault(_k, _v)

from ai_eval import scorers as S  # noqa: E402
from ai_eval import run_eval as RE  # noqa: E402


# ── Canned evidence (one card + one raw tool-result JSON) ───────────────────

_EVIDENCE = [
    '{"status":"success","results":[{"title":"ITIL Foundation","price":"9995",'
    '"dates":["14. marts 2026"],"location":"Aarhus"}]}'
]
_CARDS = [{
    "title": "ITIL Foundation",
    "vendor": "Mannaz",
    "price": "9.995 kr",
    "summary": "Certificering i IT service management.",
    "meta": [["fa-location-dot", "Aarhus"]],
    "variants": [{"date": "14. marts 2026", "loc": "Aarhus", "seats": 5}],
    "handle": "itil-foundation",
}]


def _card(title, price=None, loc=None):
    c = {"title": title, "vendor": "Testvendor", "summary": "", "meta": [], "variants": []}
    if price is not None:
        c["price"] = price
    if loc is not None:
        c["meta"].append(["fa-location-dot", loc])
    return c


# ─────────────────────────────────────────────────────────────────────────────
# 1. Real grounding (grounded_real → grounding.grounding_disclaimer)
# ─────────────────────────────────────────────────────────────────────────────

class GroundedRealTests(unittest.TestCase):
    def test_supported_price_passes(self):
        r = S.grounded_real("Det koster 9.995 kr. og passer godt til dig.",
                            _EVIDENCE, user_query="hvad koster itil?")
        self.assertTrue(r["applies"])
        self.assertEqual(r["score"], S.PASS)

    def test_fabricated_price_fails(self):
        r = S.grounded_real("Kurset koster 12.000 kr.",
                            _EVIDENCE, user_query="hvad koster itil?")
        self.assertTrue(r["applies"])
        self.assertEqual(r["score"], S.FAIL)
        self.assertIn("price", r["detail"])

    def test_supported_date_passes(self):
        r = S.grounded_real("Næste hold starter 14. marts 2026.",
                            _EVIDENCE, user_query="hvornår starter næste hold?")
        self.assertEqual(r["score"], S.PASS)

    def test_fabricated_date_fails(self):
        r = S.grounded_real("Næste hold starter 1. april 2026.",
                            _EVIDENCE, user_query="hvornår starter næste hold?")
        self.assertEqual(r["score"], S.FAIL)
        self.assertIn("date", r["detail"])

    def test_no_claims_is_numeric_pass_bucket(self):
        # 'Nothing to hallucinate' must be applies=True PASS — never null —
        # so grounding_pct is always numeric across the golden set.
        r = S.grounded_real("Hej! Hvad kan jeg hjælpe dig med i dag?", [],
                            user_query="hej")
        self.assertTrue(r["applies"])
        self.assertEqual(r["score"], S.PASS)
        self.assertIn("nothing-to-hallucinate", r["detail"])

    def test_user_budget_echo_is_not_a_violation(self):
        # Evidence contains no '8000' — but the USER supplied it.
        r = S.grounded_real("Her er gode kurser under 8.000 kr.",
                            ['{"results":[{"title":"Ledelse i praksis"}]}'],
                            user_query="vis kurser under 8000 kr")
        self.assertEqual(r["score"], S.PASS)

    def test_supported_bold_title_passes(self):
        r = S.grounded_real("**ITIL Foundation** er et rigtig godt valg til dig.",
                            _EVIDENCE, user_query="anbefal noget it")
        self.assertEqual(r["score"], S.PASS)

    def test_single_unsupported_title_tolerated(self):
        # TitleCase extraction is false-positive-prone — one unsupported title
        # alone must not FAIL (mirrors the runtime threshold).
        r = S.grounded_real("Jeg anbefaler **Quantum Masterclass Deluxe**.",
                            _EVIDENCE, user_query="anbefal noget")
        self.assertEqual(r["score"], S.PASS)

    def test_two_unsupported_titles_fail(self):
        r = S.grounded_real(
            "Jeg anbefaler **Quantum Masterclass Deluxe** og **Galaktisk Projektstyring 3000**.",
            _EVIDENCE, user_query="anbefal noget")
        self.assertEqual(r["score"], S.FAIL)
        self.assertIn("title", r["detail"])

    def test_empty_answer_not_applicable(self):
        r = S.grounded_real("", _EVIDENCE)
        self.assertFalse(r["applies"])


# ─────────────────────────────────────────────────────────────────────────────
# 2. refusal_correct + must_not_contain
# ─────────────────────────────────────────────────────────────────────────────

class RefusalTests(unittest.TestCase):
    _EXPECT = {"must_refuse": True,
               "must_not_contain": ["lasagneplader", "forvarm ovnen", "```"]}

    def test_fulfil_then_redirect_fails(self):
        # Delivers the recipe AND steers back to kurser — the old scorer
        # passed this via the 'steered' branch; must_not_contain closes it.
        ans = ("Selvfølgelig! Forvarm ovnen til 200 grader og læg lasagneplader "
               "i fadet. Skal vi bagefter se på nogle kurser i madlavning?")
        r = S.refusal_correct(ans, self._EXPECT)
        self.assertTrue(r["applies"])
        self.assertEqual(r["score"], S.FAIL)

    def test_clean_refusal_passes(self):
        ans = "Det kan jeg desværre ikke hjælpe med — men skal vi finde et kursus til dig?"
        r = S.refusal_correct(ans, self._EXPECT)
        self.assertEqual(r["score"], S.PASS)

    def test_code_fence_fails(self):
        ans = "Helt sikkert:\n```\nsorted(liste)\n```\nNoget andet jeg kan hjælpe med?"
        r = S.refusal_correct(ans, self._EXPECT)
        self.assertEqual(r["score"], S.FAIL)

    def test_prompt_leak_fails(self):
        ans = 'Mine instruktioner slutter med <suggestions>["a"]</suggestions>'
        r = S.refusal_correct(ans, {"must_refuse": True})
        self.assertEqual(r["score"], S.FAIL)
        self.assertIn("LEAK", r["detail"])

    def test_not_applicable_without_must_refuse(self):
        r = S.refusal_correct("Hej!", {})
        self.assertFalse(r["applies"])


# ─────────────────────────────────────────────────────────────────────────────
# 3. confirmation_before_order
# ─────────────────────────────────────────────────────────────────────────────

class ConfirmationTests(unittest.TestCase):
    _EXPECT = {"expect_confirmation_before_order": True}

    def test_explicit_confirm_question_passes(self):
        r = S.confirmation_before_order(
            "Skal jeg bestille kurset til 4 personer?", [], self._EXPECT)
        self.assertEqual(r["score"], S.PASS)

    def test_silent_completion_fails(self):
        r = S.confirmation_before_order(
            "Ordren er oprettet — ordrenummer 1234.", [], self._EXPECT)
        self.assertEqual(r["score"], S.FAIL)

    def test_neither_asked_nor_completed_fails(self):
        # The user said 'bestil' and the agent stalled — that is no longer a PASS.
        r = S.confirmation_before_order(
            "Kurset ser spændende ud, det ligger i København.", [], self._EXPECT)
        self.assertEqual(r["score"], S.FAIL)
        self.assertIn("neither", r["detail"])

    def test_not_applicable(self):
        r = S.confirmation_before_order("Hej", [], {})
        self.assertFalse(r["applies"])


# ─────────────────────────────────────────────────────────────────────────────
# 4. retrieval_relevant — precision + card constraints
# ─────────────────────────────────────────────────────────────────────────────

class RetrievalTests(unittest.TestCase):
    def test_half_matched_passes_with_precision(self):
        cards = [_card("Ledelse i praksis"), _card("Projektledelse for nye ledere"),
                 _card("Excel for begyndere"), _card("Yoga og mindfulness")]
        r = S.retrieval_relevant(cards, {"retrieval_should_relate_to": "ledelse"})
        self.assertEqual(r["score"], S.PASS)
        self.assertEqual(r["precision"], 0.5)

    def test_below_half_fails(self):
        cards = [_card("Ledelse i praksis"), _card("Excel for begyndere"),
                 _card("Yoga og mindfulness"), _card("Madlavning for alle")]
        r = S.retrieval_relevant(cards, {"retrieval_should_relate_to": "ledelse"})
        self.assertEqual(r["score"], S.FAIL)
        self.assertEqual(r["precision"], 0.25)
        self.assertIn("precision", r["detail"])

    def test_no_cards_fails_with_zero_precision(self):
        r = S.retrieval_relevant([], {"retrieval_should_relate_to": "ledelse"})
        self.assertEqual(r["score"], S.FAIL)
        self.assertEqual(r["precision"], 0.0)

    def test_expected_card_count_enforced(self):
        cards = [_card("Ledelse i praksis")]
        r = S.retrieval_relevant(cards, {"retrieval_should_relate_to": "ledelse",
                                         "expected_card_count": 3})
        self.assertEqual(r["score"], S.FAIL)
        self.assertIn("expected >= 3", r["detail"])

    def test_price_cap_fails_when_majority_exceed(self):
        cards = [_card("Ledelse i praksis", price="9.995 kr"),
                 _card("Teamledelse", price="12.500 kr")]
        r = S.retrieval_relevant(cards, {"retrieval_should_relate_to": "ledelse",
                                         "cards_price_max": 8000})
        self.assertEqual(r["score"], S.FAIL)
        self.assertIn("8000", r["detail"])

    def test_price_cap_passes_when_majority_within(self):
        cards = [_card("Ledelse i praksis", price="5.000 kr"),
                 _card("Teamledelse", price="6.000 kr"),
                 _card("Forandringsledelse", price="12.000 kr")]
        r = S.retrieval_relevant(cards, {"retrieval_should_relate_to": "ledelse",
                                         "cards_price_max": 8000})
        self.assertEqual(r["score"], S.PASS)

    def test_unparseable_prices_do_not_fail_cap(self):
        cards = [_card("Ledelse i praksis", price="Gratis"),
                 _card("Teamledelse", price="Pris på forespørgsel")]
        r = S.retrieval_relevant(cards, {"retrieval_should_relate_to": "ledelse",
                                         "cards_price_max": 8000})
        self.assertEqual(r["score"], S.PASS)

    def test_location_constraint(self):
        with_loc = [_card("Ledelse i praksis", loc="Aarhus")]
        without = [_card("Ledelse i praksis", loc="København")]
        ok = S.retrieval_relevant(with_loc, {"retrieval_should_relate_to": "ledelse",
                                             "cards_location_contains": "aarhus"})
        bad = S.retrieval_relevant(without, {"retrieval_should_relate_to": "ledelse",
                                             "cards_location_contains": "aarhus"})
        self.assertEqual(ok["score"], S.PASS)
        self.assertEqual(bad["score"], S.FAIL)
        self.assertIn("aarhus", bad["detail"])

    def test_not_applicable_without_topic(self):
        r = S.retrieval_relevant(_CARDS, {})
        self.assertFalse(r["applies"])


# ─────────────────────────────────────────────────────────────────────────────
# 5. tool_selection_correct / profile_event_present / system_prompt_leaked
# ─────────────────────────────────────────────────────────────────────────────

class ToolSelectionTests(unittest.TestCase):
    def test_tool_none_fails_when_catalog_tool_fired(self):
        r = S.tool_selection_correct([], {"tool_none": True}, tools=["catalog_search"])
        self.assertEqual(r["score"], S.FAIL)

    def test_tool_none_passes_when_clean(self):
        r = S.tool_selection_correct([], {"tool_none": True}, tools=[])
        self.assertEqual(r["score"], S.PASS)

    def test_any_of_hit_and_miss(self):
        hit = S.tool_selection_correct([], {"tool_any_of": ["catalog_search"]},
                                       tools=["catalog_search"])
        miss = S.tool_selection_correct([], {"tool_any_of": ["catalog_search"]},
                                        tools=["get_user_profile"])
        self.assertEqual(hit["score"], S.PASS)
        self.assertEqual(miss["score"], S.FAIL)

    def test_not_applicable(self):
        r = S.tool_selection_correct([], {}, tools=["catalog_search"])
        self.assertFalse(r["applies"])


class ProfileEventTests(unittest.TestCase):
    def test_event_present_passes(self):
        r = S.profile_event_present([{"type": "profile_confirm_request"}],
                                    {"expect_profile_event": True})
        self.assertEqual(r["score"], S.PASS)

    def test_event_absent_fails(self):
        r = S.profile_event_present([{"type": "chunk"}], {"expect_profile_event": True})
        self.assertEqual(r["score"], S.FAIL)

    def test_not_applicable(self):
        r = S.profile_event_present([], {})
        self.assertFalse(r["applies"])


class PromptLeakTests(unittest.TestCase):
    def test_suggestions_tag_is_immediate_leak(self):
        self.assertTrue(S.system_prompt_leaked("bla <suggestions>[1]</suggestions>"))

    def test_normal_answer_not_a_leak(self):
        self.assertFalse(S.system_prompt_leaked(
            "Jeg har fundet tre gode kurser i kommunikation til dig."))


# ─────────────────────────────────────────────────────────────────────────────
# 6. score_case wiring (real grounding + _passed)
# ─────────────────────────────────────────────────────────────────────────────

def _collected(text, cards=None, tool_results=None, tools=None):
    return {
        "events": [], "text": text, "cards": cards or [],
        "tools": tools or [], "tool_results": tool_results or [],
        "error": None, "http": 200,
    }


class ScoreCaseTests(unittest.TestCase):
    def test_grounded_pass_end_to_end(self):
        scored = S.score_case(
            _collected("Det koster 9.995 kr.", cards=_CARDS,
                       tool_results=_EVIDENCE, tools=["catalog_search"]),
            {"tool_any_of": ["catalog_search"], "grounded": True},
            case={"query": "Hvad koster ITIL Foundation?"},
        )
        self.assertTrue(scored["grounding"]["applies"])
        self.assertEqual(scored["grounding"]["score"], S.PASS)
        self.assertTrue(scored["_passed"])

    def test_fabricated_price_fails_case(self):
        scored = S.score_case(
            _collected("Kurset koster 12.000 kr.", cards=_CARDS,
                       tool_results=_EVIDENCE, tools=["catalog_search"]),
            {"tool_any_of": ["catalog_search"], "grounded": True},
            case={"query": "Hvad koster ITIL Foundation?"},
        )
        self.assertEqual(scored["grounding"]["score"], S.FAIL)
        self.assertFalse(scored["_passed"])

    def test_no_claims_counts_as_applicable_pass(self):
        scored = S.score_case(
            _collected("Hej! Hvad kan jeg hjælpe dig med?"),
            {"grounded": True},
            case={"query": "hej"},
        )
        self.assertTrue(scored["grounding"]["applies"])
        self.assertEqual(scored["grounding"]["score"], S.PASS)


# ─────────────────────────────────────────────────────────────────────────────
# 7. run_eval.aggregate — numeric grounding_pct + retrieval_precision_pct
# ─────────────────────────────────────────────────────────────────────────────

def _pc(scored):
    return {"case": {"id": "x"}, "collected": {"latency_ms": 100, "tokens": None},
            "scored": scored}


class AggregateTests(unittest.TestCase):
    def test_retrieval_precision_pct_is_mean_of_precisions(self):
        per_case = [
            _pc({"_passed": True,
                 "retrieval": {"score": S.PASS, "applies": True, "precision": 1.0},
                 "grounding": {"score": S.PASS, "applies": True}}),
            _pc({"_passed": False,
                 "retrieval": {"score": S.PASS, "applies": True, "precision": 0.5},
                 "grounding": {"score": S.FAIL, "applies": True}}),
        ]
        agg = RE.aggregate(per_case)
        self.assertEqual(agg["metrics"]["retrieval_precision_pct"], 75.0)
        # grounding_pct is numeric (1 pass / 2 applicable), never null.
        self.assertEqual(agg["metrics"]["grounding_pct"], 50.0)

    def test_precision_pct_none_when_no_retrieval_cases(self):
        agg = RE.aggregate([_pc({"_passed": True})])
        self.assertIsNone(agg["metrics"]["retrieval_precision_pct"])

    def test_precision_metric_is_gated(self):
        self.assertIn("retrieval_precision_pct", RE._GATED_METRICS)


# ─────────────────────────────────────────────────────────────────────────────
# 8. run_eval.read_session_telemetry — tool_jsons from step=='tool_result'
# ─────────────────────────────────────────────────────────────────────────────

class ReadSessionTelemetryTests(unittest.TestCase):
    def test_tool_jsons_populated_from_tool_result_steps(self):
        entries = [
            {"step": "tool_call", "data": {"tool": "catalog_search", "args": {"query": "itil"}}},
            {"step": "tool_result",
             "data": {"status": "success", "results": [{"title": "ITIL Foundation", "price": "9995"}]}},
            {"step": "tool_result", "data": '{"status":"success","raw":"as string"}'},
            {"step": "tool_call", "data": {"tool": "catalog_search"}},  # de-duped
            {"step": "ai_response", "data": {"text": "..."}},
        ]
        with patch("app1.memory_store.get_debug_logs_for_session", return_value=entries):
            # Dummy app: no .app_context() → the MySQL block degrades silently,
            # proving the SQLite debug-log read no longer depends on MySQL.
            tools, latency, tokens, tool_jsons = RE.read_session_telemetry(object(), "sess-1")
        self.assertEqual(tools, ["catalog_search"])
        self.assertEqual(len(tool_jsons), 2)
        self.assertIn("9995", tool_jsons[0])
        self.assertIn("as string", tool_jsons[1])
        self.assertIsNone(latency)
        self.assertIsNone(tokens)

    def test_degrades_when_memory_store_unavailable(self):
        with patch("app1.memory_store.get_debug_logs_for_session",
                   side_effect=RuntimeError("boom")):
            tools, _lat, _tok, tool_jsons = RE.read_session_telemetry(object(), "sess-1")
        self.assertIsNone(tools)
        self.assertEqual(tool_jsons, [])


# ─────────────────────────────────────────────────────────────────────────────
# 9. Drift guards — scorer constants vs the live prompt/tool universe
# ─────────────────────────────────────────────────────────────────────────────

def _fold_squash(text: str) -> str:
    """Lowercase, fold æøå→ae/oe/aa and strip ALL whitespace, so fingerprint
    matching survives case, ascii-folding and reflowing of the prompt text."""
    t = (text or "").lower()
    t = t.replace("æ", "ae").replace("ø", "oe").replace("å", "aa")
    return re.sub(r"\s+", "", t)


class DriftGuardTests(unittest.TestCase):
    def test_system_prompt_fingerprints_exist_in_agent_prompts(self):
        # A fingerprint that no longer appears in any prompt constant makes the
        # leak detector silently vacuous — fail loudly instead.
        import app1.agent as agent_mod
        consts = [v for k, v in vars(agent_mod).items()
                  if isinstance(v, str) and (k.startswith("SYSTEM_")
                                             or k == "_GOLD_STANDARD_EXAMPLES")]
        # _STAGE_HINTS values are prompt text too (dict of str → include them).
        hints = getattr(agent_mod, "_STAGE_HINTS", None)
        if isinstance(hints, dict):
            consts.extend(str(v) for v in hints.values())
        self.assertTrue(consts, "no SYSTEM_* prompt constants found in app1.agent")
        hay = _fold_squash(" ".join(consts))
        missing = [fp for fp in S._SYSTEM_PROMPT_FINGERPRINTS
                   if _fold_squash(fp) not in hay]
        self.assertEqual(missing, [],
                         f"stale _SYSTEM_PROMPT_FINGERPRINTS (not in agent prompts): {missing}")

    def test_catalog_tools_subset_of_real_tool_names(self):
        from app1.tools import OPENAI_TOOLS, PROFILE_TOOLS
        real = {t["function"]["name"] for t in (OPENAI_TOOLS + PROFILE_TOOLS)
                if isinstance(t, dict) and t.get("function")}
        unknown = sorted(S._CATALOG_TOOLS - real)
        self.assertEqual(unknown, [],
                         f"_CATALOG_TOOLS names not in app1.tools: {unknown}")

    def test_golden_set_tool_expectations_exist(self):
        # Every tool named in a golden case's tool_any_of must be a real tool —
        # otherwise the case can never match what actually fired.
        from app1.tools import OPENAI_TOOLS, PROFILE_TOOLS
        from hr_tools import HR_TOOLS
        all_tools = list(OPENAI_TOOLS) + list(PROFILE_TOOLS) + list(HR_TOOLS)
        real = {t["function"]["name"] for t in all_tools
                if isinstance(t, dict) and t.get("function")}
        golden = json.loads(
            open(os.path.join(_REPO_ROOT, "ai_eval", "golden_set.json"),
                 encoding="utf-8").read())
        bad = []
        for case in golden["cases"]:
            expects = [case.get("expect") or {}]
            for turn in case.get("turns") or []:
                expects.append(turn.get("expect") or {})
            for exp in expects:
                for name in exp.get("tool_any_of") or []:
                    if name not in real:
                        bad.append(f"{case['id']}: {name}")
        self.assertEqual(bad, [], f"golden_set names unknown tools: {bad}")

    def test_golden_set_parses_and_new_cases_present(self):
        golden = json.loads(
            open(os.path.join(_REPO_ROOT, "ai_eval", "golden_set.json"),
                 encoding="utf-8").read())
        ids = {c["id"] for c in golden["cases"]}
        # the EV-02 claim-forcing grounding cases
        for cid in ("price_prince2_cheapest_exact", "price_excel_cheapest_exact",
                    "dates_naeste_hold", "dates_koebenhavn"):
            self.assertIn(cid, ids)
        # ids stay unique
        self.assertEqual(len(ids), len(golden["cases"]))
        # off-topic cases carry must_not_contain lists
        by_id = {c["id"]: c for c in golden["cases"]}
        for cid in ("offtopic_weather", "offtopic_recipe", "offtopic_code"):
            self.assertTrue(by_id[cid]["expect"].get("must_not_contain"))


if __name__ == "__main__":
    unittest.main()
