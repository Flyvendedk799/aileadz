"""Pure-logic tests for the advisor's profile-conditioned retrieval depth
(value-5) and the comparison guardrails (value-4).

These cover ONLY the extracted pure helpers, so they need no live MySQL / OpenAI:
  * app1.rag.profile_candidate_limit — candidate-pool scaling
  * app1.tools._comparison_guardrails — 4-course cap + single-vendor notice
"""
import unittest

from app1.rag import profile_candidate_limit, PROFILE_CANDIDATE_LIMIT_CAP
from app1.tools import _comparison_guardrails, COMPARE_MAX_COURSES


class ProfileCandidateLimitTests(unittest.TestCase):
    """Retrieval pool scales with profile richness; thin/anon stay small."""

    def test_anonymous_profile_keeps_base_limit(self):
        # No profile_boost (anonymous user) → no widening, no cost regression.
        self.assertEqual(profile_candidate_limit(None, 4), 4)

    def test_empty_profile_keeps_base_limit(self):
        self.assertEqual(profile_candidate_limit({}, 4), 4)
        self.assertEqual(profile_candidate_limit({"target_terms": set()}, 4), 4)

    def test_single_target_term_is_thin_and_not_widened(self):
        thin = {"target_terms": {"ledelse"}}
        self.assertEqual(profile_candidate_limit(thin, 4), 4)

    def test_rich_profile_widens_pool_above_base(self):
        rich = {"target_terms": {"ledelse", "scrum", "python", "kommunikation", "gdpr"}}
        widened = profile_candidate_limit(rich, 4)
        self.assertGreater(widened, 4)
        self.assertLessEqual(widened, PROFILE_CANDIDATE_LIMIT_CAP)

    def test_richer_profile_widens_at_least_as_much(self):
        less = {"target_terms": {"a", "b", "c", "d", "e"}}
        more = {"target_terms": {chr(c) for c in range(ord("a"), ord("a") + 14)}}
        self.assertGreaterEqual(
            profile_candidate_limit(more, 4),
            profile_candidate_limit(less, 4),
        )

    def test_pool_never_exceeds_cap(self):
        huge = {"target_terms": {str(i) for i in range(200)}}
        self.assertEqual(
            profile_candidate_limit(huge, 4),
            PROFILE_CANDIDATE_LIMIT_CAP,
        )
        # And not even a tiny base nor a custom cap is exceeded.
        self.assertLessEqual(profile_candidate_limit(huge, 1, cap=24), 24)

    def test_pool_never_below_base(self):
        # Even a small base with a rich profile returns >= base.
        rich = {"target_terms": {"a", "b", "c", "d", "e", "f"}}
        self.assertGreaterEqual(profile_candidate_limit(rich, 3), 3)


class ComparisonGuardrailTests(unittest.TestCase):
    """4-course hard cap + single-vendor diversity notice in the tool result."""

    def test_cap_constant_is_four(self):
        self.assertEqual(COMPARE_MAX_COURSES, 4)

    def test_no_truncation_note_within_cap(self):
        guards = _comparison_guardrails(3, ["A", "B", "C"])
        self.assertIsNone(guards["_truncation_note"])
        self.assertEqual(guards["_compare_cap"], 4)

    def test_truncation_note_when_over_cap(self):
        guards = _comparison_guardrails(6, ["A", "B", "C", "D"])
        self.assertIsNotNone(guards["_truncation_note"])
        # Danish notice mentions both the requested count and the cap.
        self.assertIn("6", guards["_truncation_note"])
        self.assertIn(str(COMPARE_MAX_COURSES), guards["_truncation_note"])

    def test_single_vendor_notice_when_all_same_vendor(self):
        guards = _comparison_guardrails(3, ["Teknologisk Institut"] * 3)
        self.assertIsNotNone(guards["single_vendor_notice"])
        # Mentions the vendor and nudges toward another supplier (Danish).
        self.assertIn("Teknologisk Institut", guards["single_vendor_notice"])
        self.assertIn("anden udbyder", guards["single_vendor_notice"])

    def test_no_single_vendor_notice_when_vendors_differ(self):
        guards = _comparison_guardrails(2, ["Teknologisk Institut", "SuperUsers"])
        self.assertIsNone(guards["single_vendor_notice"])

    def test_no_single_vendor_notice_for_a_lone_course(self):
        # A single course is never a "no diversity" complaint.
        guards = _comparison_guardrails(1, ["Teknologisk Institut"])
        self.assertIsNone(guards["single_vendor_notice"])

    def test_blank_vendors_do_not_trigger_single_vendor_notice(self):
        guards = _comparison_guardrails(2, ["", "  "])
        self.assertIsNone(guards["single_vendor_notice"])


if __name__ == "__main__":
    unittest.main()
