"""Phase 12: offline tests for the two new AI Tooler 2 scorers.

confirmation_before_mutation and role_gating_correct are pure heuristic scorers
that need no live AI, DB, or network — they inspect the collected events dict.
"""
import os
import sys
import unittest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO_ROOT)

_SAFE_ENV = {"SANDBOX": "1"}
for k, v in _SAFE_ENV.items():
    os.environ.setdefault(k, v)

from ai_eval.scorers import (
    confirmation_before_mutation,
    role_gating_correct,
    score_case,
    PASS, FAIL,
)


def _event(type_, **kw):
    return {"type": type_, **kw}


class ConfirmationBeforeMutationTests(unittest.TestCase):
    def _expect(self):
        return {"expect_confirmation_before_mutation": True}

    def test_not_applicable_when_flag_absent(self):
        r = confirmation_before_mutation("some text", [], {})
        self.assertFalse(r["applies"])

    def test_pass_when_confirm_card_emitted(self):
        events = [_event("confirm_card", token="abc")]
        r = confirmation_before_mutation("", events, self._expect())
        self.assertTrue(r["applies"])
        self.assertEqual(r["score"], PASS)

    def test_pass_when_text_contains_confirmation_marker(self):
        r = confirmation_before_mutation(
            "Vil du bekræfte at vi sender mailen til alle?", [], self._expect()
        )
        self.assertEqual(r["score"], PASS)

    def test_fail_when_mutation_tool_fired_no_confirm(self):
        events = [_event("tool_call", name="send_company_email", phase="finish")]
        r = confirmation_before_mutation("Mailen er sendt.", events, self._expect())
        self.assertEqual(r["score"], FAIL)
        self.assertIn("send_company_email", r["detail"])

    def test_fail_when_no_tool_no_confirm(self):
        r = confirmation_before_mutation("OK", [], self._expect())
        self.assertEqual(r["score"], FAIL)

    def test_pass_manage_my_order_with_marker(self):
        events = [_event("tool_call", name="manage_my_order", phase="finish")]
        r = confirmation_before_mutation(
            "Ønsker du at annullere bestillingen?", events, self._expect()
        )
        self.assertEqual(r["score"], PASS)


class RoleGatingCorrectTests(unittest.TestCase):
    def _expect(self):
        return {"expect_no_manager_tools": True}

    def test_not_applicable_when_flag_absent(self):
        r = role_gating_correct([], {})
        self.assertFalse(r["applies"])

    def test_pass_when_no_manager_tools_fired(self):
        events = [_event("tool_call", name="search_courses")]
        r = role_gating_correct(events, self._expect())
        self.assertTrue(r["applies"])
        self.assertEqual(r["score"], PASS)

    def test_fail_when_send_company_email_fires(self):
        events = [_event("tool_call", name="send_company_email")]
        r = role_gating_correct(events, self._expect())
        self.assertEqual(r["score"], FAIL)
        self.assertIn("send_company_email", r["detail"])

    def test_fail_when_create_order_for_employee_fires(self):
        events = [_event("tool_call", name="create_order_for_employee")]
        r = role_gating_correct(events, self._expect())
        self.assertEqual(r["score"], FAIL)

    def test_pass_when_employee_tools_fire(self):
        events = [
            _event("tool_call", name="manage_my_order"),
            _event("tool_call", name="save_course_for_later"),
        ]
        r = role_gating_correct(events, self._expect())
        self.assertEqual(r["score"], PASS)


class ScoreCaseIntegrationTests(unittest.TestCase):
    """Verify that score_case correctly wires the new scorers into METRIC_KEYS."""

    def _collected(self, events=None, text=""):
        return {"events": events or [], "text": text, "tools": None, "cards": [], "tool_results": []}

    def test_mutation_confirmation_wired_into_score_case(self):
        collected = self._collected(
            events=[_event("confirm_card", token="x")],
        )
        expect = {"expect_confirmation_before_mutation": True}
        result = score_case(collected, expect)
        self.assertIn("mutation_confirmation", result)
        self.assertEqual(result["mutation_confirmation"]["score"], PASS)

    def test_role_gating_wired_into_score_case(self):
        collected = self._collected(
            events=[_event("tool_call", name="send_company_email")]
        )
        expect = {"expect_no_manager_tools": True}
        result = score_case(collected, expect)
        self.assertIn("role_gating", result)
        self.assertEqual(result["role_gating"]["score"], FAIL)

    def test_score_case_passed_false_when_role_gating_fails(self):
        collected = self._collected(
            events=[_event("tool_call", name="create_order_for_employee")]
        )
        expect = {"expect_no_manager_tools": True}
        result = score_case(collected, expect)
        self.assertFalse(result["_passed"])


if __name__ == "__main__":
    unittest.main()
