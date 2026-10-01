"""N-5.7: fluency scorer, assistant scopes and golden-set coverage."""

import json
import os
import unittest

from ai_eval import scorers as S

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EXPECT = {"fluent": True}


class FluencyScorerTests(unittest.TestCase):
    def test_not_applicable_unless_requested(self):
        self.assertFalse(S.fluency_ok("Hvad er dit navn? Hvad er din titel? Hvad er din mail?", {})["applies"])

    def test_conversational_answer_passes(self):
        r = S.fluency_ok("Det lyder som en spændende rolle. Hvad fylder mest i hverdagen lige nu?", EXPECT)
        self.assertEqual(r["score"], S.PASS)

    def test_form_phrase_fails(self):
        r = S.fluency_ok("For at kunne hjælpe dig har jeg brug for følgende oplysninger.", EXPECT)
        self.assertEqual(r["score"], S.FAIL)

    def test_three_questions_in_one_turn_fails(self):
        r = S.fluency_ok("Hvad hedder du? Hvad er din titel? Hvor arbejder du?", EXPECT)
        self.assertEqual(r["score"], S.FAIL)
        self.assertIn("questions", r["detail"])

    def test_profile_field_enumeration_fails(self):
        r = S.fluency_ok("Fortæl mig om din erfaring, uddannelse, kompetencer og sprog.", EXPECT)
        self.assertEqual(r["score"], S.FAIL)

    def test_list_of_things_to_supply_fails(self):
        text = "Jeg skal bruge:\n- Dit navn?\n- Din titel?\n- Din afdeling: hvilken?\n- Dit mål: hvad?"
        self.assertEqual(S.fluency_ok(text, EXPECT)["score"], S.FAIL)

    def test_wired_into_score_case(self):
        out = S.score_case({"events": [], "text": "Fortæl om din erfaring, uddannelse, kompetencer og sprog.",
                            "cards": [], "tool_results": [], "http": 200}, EXPECT)
        self.assertEqual(out["fluency"]["score"], S.FAIL)
        self.assertFalse(out["_passed"])

    def test_skipped_scope_never_fails(self):
        out = S.score_case({"skipped": "no vendor login", "events": [], "text": "", "http": 200}, EXPECT)
        self.assertTrue(out["_passed"])
        self.assertTrue(out["_skipped"])


class ScopeAndCoverageTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "ai_eval", "golden_set.json"), encoding="utf-8") as fh:
            self.cases = json.load(fh)["cases"]

    def test_hr_and_vendor_cases_exist_and_declare_scope(self):
        scopes = [c.get("scope", "employee") for c in self.cases]
        self.assertGreaterEqual(scopes.count("hr"), 3)
        self.assertGreaterEqual(scopes.count("vendor"), 3)

    def test_profiler_coverage_grew(self):
        self.assertGreaterEqual(sum(1 for c in self.cases if c.get("mode") == "profiler"), 6)

    def test_runner_routes_each_scope_to_its_endpoint(self):
        from ai_eval import run_eval
        self.assertEqual(run_eval.SCOPE_ENDPOINTS["hr"], "/hr/chatbot/ask")
        self.assertEqual(run_eval.SCOPE_ENDPOINTS["vendor"], "/vendor/ask")

    def test_vendor_scope_is_skipped_without_credentials(self):
        from ai_eval import run_eval
        for k in ("EVAL_VENDOR_EMAIL", "EVAL_VENDOR_PASSWORD"):
            os.environ.pop(k, None)
        self.assertIsNone(run_eval.fresh_client(object(), scope="vendor"))


if __name__ == "__main__":
    unittest.main()
