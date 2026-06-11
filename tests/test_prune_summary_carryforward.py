"""MC-01: repeated pruning must not destroy earlier conversation summaries.

Offline-only: forces the rule-based summarizer path (AI_SUMMARY_MODE=rules),
no OpenAI calls, no MySQL.
"""
import os
import unittest
from unittest.mock import patch

from ai_context import (
    _CARRYFORWARD_PREFIX,
    prune_conversation_memory,
    summarize_pruned_messages_rule_based,
)


def _summary_messages(messages):
    return [
        m for m in messages
        if m.get("role") == "system" and str(m.get("content") or "").startswith("SAMTALEOVERSIGT")
    ]


def _filler_pairs(count, start=0):
    out = []
    for i in range(start, start + count):
        out.append({"role": "user", "content": f"spørgsmål {i}"})
        out.append({"role": "assistant", "content": f"svar {i}"})
    return out


class PruneSummaryCarryforwardTests(unittest.TestCase):
    def setUp(self):
        self._old_mode = os.environ.get("AI_SUMMARY_MODE")
        os.environ["AI_SUMMARY_MODE"] = "rules"

    def tearDown(self):
        if self._old_mode is None:
            os.environ.pop("AI_SUMMARY_MODE", None)
        else:
            os.environ["AI_SUMMARY_MODE"] = self._old_mode

    def test_fact_from_first_third_survives_two_prunes(self):
        messages = [{"role": "system", "content": "sys"}]
        messages.append({"role": "user", "content": "Mit budget er 8000 kr til kurset"})
        messages.append({"role": "assistant", "content": "Noteret — budget 8000 kr."})
        messages.extend(_filler_pairs(3, start=0))
        messages.extend(_filler_pairs(6, start=10))  # recent tail for prune 1
        self.assertGreater(len(messages), 18)

        pruned_once = prune_conversation_memory(messages, keep_recent=12, trigger_at=18)
        first_summaries = _summary_messages(pruned_once)
        self.assertEqual(len(first_summaries), 1)
        self.assertIn("8000", first_summaries[0]["content"])

        # Grow the conversation past the trigger again and prune a second time.
        pruned_once.extend(_filler_pairs(8, start=20))
        self.assertGreater(len(pruned_once), 36 - 12)
        pruned_twice = prune_conversation_memory(pruned_once, keep_recent=12, trigger_at=18)

        final_summaries = _summary_messages(pruned_twice)
        self.assertEqual(len(final_summaries), 1)
        self.assertIn(
            "8000",
            final_summaries[0]["content"],
            "Fakta fra samtalens første tredjedel forsvandt ved anden pruning",
        )
        # The new summary must keep the marker prefix so a third prune finds it.
        self.assertTrue(final_summaries[0]["content"].startswith("SAMTALEOVERSIGT"))

    def test_old_summary_message_is_replaced_not_duplicated(self):
        messages = [{"role": "system", "content": "sys"}]
        messages.append({"role": "system", "content": "SAMTALEOVERSIGT (kompakt):\nBruger sagde: budget 8000 kr"})
        messages.extend(_filler_pairs(12, start=0))
        pruned = prune_conversation_memory(messages, keep_recent=12, trigger_at=18)
        self.assertEqual(len(_summary_messages(pruned)), 1)
        self.assertIn("8000", _summary_messages(pruned)[0]["content"])

    def test_rule_based_summarizer_folds_carryforward_line(self):
        msgs = [
            {"role": "user", "content": f"{_CARRYFORWARD_PREFIX} SAMTALEOVERSIGT (kompakt):\nBruger sagde: budget 8000 kr"},
            {"role": "user", "content": "vis mig flere kurser"},
            {"role": "user", "content": "gerne i Aarhus"},
            {"role": "user", "content": "hvad med e-learning?"},
            {"role": "user", "content": "og prisen?"},
            {"role": "assistant", "content": "Her er flere bud."},
        ]
        summary = summarize_pruned_messages_rule_based(msgs)
        self.assertIsNotNone(summary)
        # Carryforward must survive even with >4 newer user messages in the window.
        self.assertIn("8000", summary)
        self.assertTrue(summary.startswith("SAMTALEOVERSIGT"))

    def test_summarizer_none_falls_back_to_prior_summary(self):
        prior = "SAMTALEOVERSIGT (kompakt):\nBruger sagde: budget 8000 kr"
        messages = [{"role": "system", "content": "sys"}]
        messages.append({"role": "system", "content": prior})
        messages.extend(_filler_pairs(12, start=0))
        with patch("ai_context.summarize_pruned_messages_smart", return_value=None):
            pruned = prune_conversation_memory(messages, keep_recent=12, trigger_at=18)
        summaries = _summary_messages(pruned)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["content"], prior)

    def test_profile_markers_remain_protected(self):
        profile = "BRUGERENS NUVÆRENDE PROFIL: budgetansvarlig"
        messages = [{"role": "system", "content": "sys"}]
        messages.append({"role": "system", "content": profile})
        messages.append({"role": "system", "content": "SAMTALEOVERSIGT (kompakt):\nBruger sagde: budget 8000 kr"})
        messages.extend(_filler_pairs(12, start=0))
        pruned = prune_conversation_memory(messages, keep_recent=12, trigger_at=18)
        self.assertTrue(any((m.get("content") or "") == profile for m in pruned))
        # The profile message must not be folded into the carryforward line.
        self.assertEqual(len(_summary_messages(pruned)), 1)

    def test_prune_without_prior_summary_unchanged(self):
        messages = [{"role": "system", "content": "sys"}]
        messages.extend(_filler_pairs(13, start=0))
        pruned = prune_conversation_memory(messages, keep_recent=8, trigger_at=18)
        self.assertLess(len(pruned), len(messages))
        summaries = _summary_messages(pruned)
        self.assertEqual(len(summaries), 1)
        self.assertNotIn(_CARRYFORWARD_PREFIX, summaries[0]["content"])


if __name__ == "__main__":
    unittest.main()
