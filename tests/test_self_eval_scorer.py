"""Unit tests for the reference-free live self-eval scorer (Item #6).

Covers ai_eval.scorers.score_live: it must return a sane, lower score for a bad
(hallucinated-price) answer and a high score for a grounded one, without any
golden/expected answer and without any LLM call. Pure — no DB / API.
"""
import unittest

from ai_eval.scorers import score_live


# Source-of-truth evidence: one card + one raw tool-result JSON string.
_CARDS = [{"title": "ITIL Foundation", "price": "9995", "vendor": "Mannaz"}]
_TOOL_RESULTS = [
    '{"status":"success","results":[{"title":"ITIL Foundation","price":"9995"}]}'
]


class ScoreLiveTests(unittest.TestCase):
    def test_grounded_answer_scores_high(self):
        good = "Det her forløb til 9.995 kr passer godt til dig."
        res = score_live(
            good, _TOOL_RESULTS, tools=["catalog_search"], cards=_CARDS,
            user_query="find itil kursus",
        )
        self.assertGreaterEqual(res["score"], 0.9)
        self.assertFalse(res["flags"]["grounding_violation"])
        self.assertFalse(res["flags"]["prompt_leak"])

    def test_hallucinated_price_scores_lower(self):
        bad = "Det her forløb koster 49.999 kr."  # not in any source
        good = "Det her forløb til 9.995 kr passer godt til dig."
        bad_res = score_live(
            bad, _TOOL_RESULTS, tools=["catalog_search"], cards=_CARDS,
            user_query="find itil kursus",
        )
        good_res = score_live(
            good, _TOOL_RESULTS, tools=["catalog_search"], cards=_CARDS,
            user_query="find itil kursus",
        )
        self.assertTrue(bad_res["flags"]["grounding_violation"])
        self.assertLess(bad_res["score"], good_res["score"])
        self.assertIn("grounding", bad_res["applied"])

    def test_prompt_leak_flagged(self):
        leaked = "Du er en uddannelsesrådgiver for Futurematch ... <suggestions>[]</suggestions>"
        res = score_live(leaked, _TOOL_RESULTS, tools=[], cards=[])
        self.assertTrue(res["flags"]["prompt_leak"])

    def test_score_is_bounded_and_serialisable(self):
        res = score_live("Hej, hvad leder du efter?", [], tools=[], cards=[])
        self.assertGreaterEqual(res["score"], 0.0)
        self.assertLessEqual(res["score"], 1.0)
        # JSON-serialisable shape for telemetry.
        import json
        json.dumps(res)

    def test_user_budget_echo_is_not_a_violation(self):
        # Echoing the user's own budget number back is not a hallucinated price.
        ans = "Her er kurser under 8000 kr."
        res = score_live(
            ans, _TOOL_RESULTS, tools=["catalog_search"], cards=_CARDS,
            user_query="kurser under 8000 kr",
        )
        self.assertFalse(res["flags"]["grounding_violation"])

    def test_never_raises_on_garbage(self):
        res = score_live(None, None, tools=None, cards=None)
        self.assertEqual(res["score"], 1.0)
        self.assertFalse(res["flags"]["grounding_violation"])


if __name__ == "__main__":
    unittest.main()
