"""Unit tests for the runtime hallucination circuit-breaker (Item #1).

Covers grounding.grounding_disclaimer: it must trip on a fabricated price/date/
title that is NOT in the tool results, but stay quiet for a grounded answer.
Pure functions — no live DB / API. Mirrors the dependency-free style of the
existing eval scorers.
"""
import unittest

import grounding


# A realistic tool-result payload (the chain-of-custody source of truth) the
# answer must be grounded in. Shaped like a catalog_search result.
_TOOL_RESULTS = [
    '{"status":"success","count":1,"results":[{"title":"ITIL Foundation",'
    '"handle":"itil-foundation","price":"9.995","vendor":"Mannaz",'
    '"locations":["København"]}]}'
]


class GroundingDisclaimerTests(unittest.TestCase):
    def test_fabricated_price_triggers_disclaimer(self):
        # The answer invents a price (12.500 kr) that is NOT in the tool results.
        answer = "Kurset koster 12.500 kr og er et godt valg."
        verdict = grounding.grounding_disclaimer(answer, _TOOL_RESULTS)
        self.assertTrue(verdict["violation"])
        self.assertTrue(verdict["disclaimer"])
        self.assertEqual(verdict["disclaimer"], grounding.GROUNDING_DISCLAIMER_DA)
        # The offending claim is reported for telemetry.
        types = {u["type"] for u in verdict["unsupported"]}
        self.assertIn("price", types)

    def test_grounded_price_does_not_trigger(self):
        # The answer states the SAME price that is present in the tool results.
        answer = "Det her forløb ligger på 9.995 kr — solid værdi."
        verdict = grounding.grounding_disclaimer(answer, _TOOL_RESULTS)
        self.assertFalse(verdict["violation"])
        self.assertEqual(verdict["disclaimer"], "")
        self.assertEqual(verdict["unsupported"], [])

    def test_fabricated_date_triggers_disclaimer(self):
        answer = "Næste hold starter 14. marts 2027."
        verdict = grounding.grounding_disclaimer(answer, _TOOL_RESULTS)
        self.assertTrue(verdict["violation"])
        self.assertIn("date", {u["type"] for u in verdict["unsupported"]})

    def test_no_concrete_claims_is_safe(self):
        # A pure conversational answer asserts no checkable facts -> no violation.
        answer = "Godt spørgsmål! Hvad er vigtigst for dig — pris eller niveau?"
        verdict = grounding.grounding_disclaimer(answer, _TOOL_RESULTS)
        self.assertFalse(verdict["violation"])
        self.assertEqual(verdict["disclaimer"], "")

    def test_empty_inputs_never_raise(self):
        for ans, tr in (("", None), (None, []), ("12.500 kr", None)):
            verdict = grounding.grounding_disclaimer(ans, tr)
            self.assertIn("violation", verdict)
            self.assertIsInstance(verdict["violation"], bool)

    def test_disclaimer_is_danish_and_about_verification(self):
        self.assertIn("Bekræft", grounding.GROUNDING_DISCLAIMER_DA)
        self.assertIn("kursussiden", grounding.GROUNDING_DISCLAIMER_DA)


if __name__ == "__main__":
    unittest.main()
