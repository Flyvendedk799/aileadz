"""Unit tests for the runtime hallucination circuit-breaker (Item #1 + AG-03).

Covers grounding.grounding_disclaimer: it must trip on a fabricated price/date
that is NOT in the tool results, but stay quiet for a grounded answer. AG-03
additions: tool-call ARGUMENTS never count as evidence (the argument-echo
loophole), bare TitleCase runs are catalog-validated via an injected
known_titles_fn, and a single weak title mismatch no longer trips the breaker
(titles only at >=2; prices/dates still at >=1).
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

# AG-03: a small canonical catalog-title list, injected as known_titles_fn the
# same way app1/agent.py injects its rag-index lookup.
_KNOWN_TITLES = ["ITIL Foundation", "Agil Projektledelse", "Ledelse i Praksis"]


def _known_titles_fn():
    return list(_KNOWN_TITLES)


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


class ArgumentEchoLoopholeTests(unittest.TestCase):
    """AG-03 (a): tool-call arguments must NEVER count as supporting evidence —
    they echo the model's (often user-supplied) input, not catalog facts."""

    def test_argument_echoed_price_is_not_support_dict_shape(self):
        # The user-typed budget 12.500 was echoed into the search arguments;
        # the OUTPUT only contains 9.995. The answer's 12.500 kr is unverified.
        tool_results = [{
            "name": "catalog_search",
            "arguments": {"query": "ledelse", "max_price": "12.500 kr"},
            "output": '{"results":[{"title":"ITIL Foundation","price":"9.995"}]}',
        }]
        answer = "Kurset koster 12.500 kr og passer til dit budget."
        verdict = grounding.grounding_disclaimer(answer, tool_results)
        self.assertTrue(verdict["violation"])
        self.assertIn("price", {u["type"] for u in verdict["unsupported"]})

    def test_argument_echoed_price_is_not_support_object_shape(self):
        # ToolCallResult-like object: .arguments must be ignored, only .output read.
        class _FakeToolResult:
            name = "catalog_search"
            arguments = {"max_price": "12.500 kr"}
            output = '{"results":[{"title":"ITIL Foundation","price":"9.995"}]}'

        answer = "Det ligger på 12.500 kr i alt."
        verdict = grounding.grounding_disclaimer(answer, [_FakeToolResult()])
        self.assertTrue(verdict["violation"])
        self.assertIn("price", {u["type"] for u in verdict["unsupported"]})

    def test_output_in_dict_shape_still_supports(self):
        # The same dict shape must keep supporting claims grounded in the OUTPUT.
        tool_results = [{
            "name": "catalog_search",
            "arguments": {"query": "itil"},
            "output": '{"results":[{"title":"ITIL Foundation","price":"9.995"}]}',
        }]
        answer = "Det her forløb ligger på 9.995 kr."
        verdict = grounding.grounding_disclaimer(answer, tool_results)
        self.assertFalse(verdict["violation"])
        self.assertEqual(verdict["unsupported"], [])


class CatalogValidatedTitleTests(unittest.TestCase):
    """AG-03 (b): bare TitleCase runs only count as title claims when they
    fuzzy-match a known catalog title (injected via known_titles_fn)."""

    def test_prose_titlecase_is_not_a_claim_with_catalog(self):
        # 'God Fornøjelse' is capitalised prose, not a course — with the catalog
        # lookup injected it must not be extracted as a title claim at all.
        answer = "God Fornøjelse med kurset — det bliver rigtig godt!"
        claims = grounding.extract_factual_claims(answer, known_titles_fn=_known_titles_fn)
        self.assertEqual(claims["titles"], [])
        verdict = grounding.grounding_disclaimer(
            answer, _TOOL_RESULTS, known_titles_fn=_known_titles_fn
        )
        self.assertFalse(verdict["violation"])
        self.assertEqual(verdict["unsupported"], [])

    def test_known_title_is_still_a_claim_with_catalog(self):
        # A real catalog title NOT in this turn's tool results is still an
        # unsupported claim (telemetry), even below the disclaimer threshold.
        answer = "Jeg vil pege på Agil Projektledelse som næste skridt."
        claims = grounding.extract_factual_claims(answer, known_titles_fn=_known_titles_fn)
        self.assertIn("Agil Projektledelse", claims["titles"])
        verdict = grounding.grounding_disclaimer(
            answer, _TOOL_RESULTS, known_titles_fn=_known_titles_fn
        )
        self.assertIn("title", {u["type"] for u in verdict["unsupported"]})

    def test_quoted_bold_titles_bypass_catalog_gate(self):
        # Quoted/bold extraction is unchanged — high precision already.
        answer = "Jeg anbefaler **Quantum Masterclass Deluxe** til dig."
        claims = grounding.extract_factual_claims(answer, known_titles_fn=_known_titles_fn)
        self.assertIn("Quantum Masterclass Deluxe", claims["titles"])

    def test_empty_catalog_falls_back_to_heuristic(self):
        # known_titles_fn returning [] (index unavailable) must degrade to the
        # legacy TitleCase heuristic, not silently disable title extraction.
        answer = "Jeg anbefaler Strategisk Forhandlingsteknik til dit team."
        claims = grounding.extract_factual_claims(answer, known_titles_fn=lambda: [])
        self.assertIn("Strategisk Forhandlingsteknik", claims["titles"])

    def test_raising_known_titles_fn_never_breaks(self):
        def _boom():
            raise RuntimeError("index down")

        verdict = grounding.grounding_disclaimer(
            "Helt almindeligt svar uden tal.", _TOOL_RESULTS, known_titles_fn=_boom
        )
        self.assertFalse(verdict["violation"])


class DisclaimerThresholdTests(unittest.TestCase):
    """AG-03 (c): >=1 unsupported price/date trips the breaker; titles only at >=2."""

    def test_single_unsupported_title_does_not_trigger(self):
        answer = "Mange vælger Strategisk Forhandlingsteknik som næste skridt."
        verdict = grounding.grounding_disclaimer(answer, _TOOL_RESULTS)
        self.assertFalse(verdict["violation"])
        self.assertEqual(verdict["disclaimer"], "")
        # ...but the weak claim is still reported for telemetry/eval.
        self.assertIn("title", {u["type"] for u in verdict["unsupported"]})

    def test_two_unsupported_titles_trigger(self):
        answer = ("Jeg anbefaler **Quantum Masterclass Deluxe** og "
                  "**Galaktisk Projektstyring 3000**.")
        verdict = grounding.grounding_disclaimer(answer, _TOOL_RESULTS)
        self.assertTrue(verdict["violation"])
        self.assertEqual(verdict["disclaimer"], grounding.GROUNDING_DISCLAIMER_DA)
        titles = [u for u in verdict["unsupported"] if u["type"] == "title"]
        self.assertGreaterEqual(len(titles), 2)

    def test_single_unsupported_price_still_triggers(self):
        # Prices/dates remain hard errors at >=1 — unchanged by the title rule.
        answer = "Prisen er 4.250 kr for hele forløbet."
        verdict = grounding.grounding_disclaimer(answer, _TOOL_RESULTS)
        self.assertTrue(verdict["violation"])

    def test_one_title_plus_one_price_triggers_via_price(self):
        answer = "Strategisk Forhandlingsteknik koster 4.250 kr."
        verdict = grounding.grounding_disclaimer(answer, _TOOL_RESULTS)
        self.assertTrue(verdict["violation"])
        self.assertIn("price", {u["type"] for u in verdict["unsupported"]})


if __name__ == "__main__":
    unittest.main()
