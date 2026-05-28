import unittest

from ai_context import (
    build_few_shot_message,
    choose_max_iterations,
    prune_conversation_memory,
    summarize_pruned_messages_rule_based,
    summarize_pruned_messages_smart,
)
from ai_runtime import run_direct_completion, user_facing_error_message


class AIContextTests(unittest.TestCase):
    def test_rule_based_summary_is_compact(self):
        messages = [
            {"role": "user", "content": "Jeg leder efter ITIL kurser i København"},
            {"role": "assistant", "content": "Her er nogle bud."},
            {"role": "tool", "content": '{"results":[{"title":"ITIL Foundation"}]}'},
        ]
        summary = summarize_pruned_messages_rule_based(messages)
        self.assertIn("SAMTALEOVERSIGT", summary or "")
        self.assertIn("ITIL", summary or "")

    def test_smart_summary_uses_rules_for_small_blocks(self):
        messages = [{"role": "user", "content": "Hej"}]
        summary = summarize_pruned_messages_smart(messages)
        self.assertIn("Bruger sagde", summary or "")

    def test_prune_conversation_memory_injects_summary(self):
        messages = [{"role": "system", "content": "sys"}]
        for i in range(25):
            messages.append({"role": "user", "content": f"question {i}"})
            messages.append({"role": "assistant", "content": f"answer {i}"})
        pruned = prune_conversation_memory(messages, keep_recent=8, trigger_at=18)
        self.assertLess(len(pruned), len(messages))
        self.assertTrue(any("SAMTALEOVERSIGT" in (m.get("content") or "") for m in pruned if m.get("role") == "system"))

    def test_few_shot_compact_mode(self):
        msg = build_few_shot_message("FULL")
        self.assertIsNotNone(msg)
        self.assertIn("EKSEMPLER", msg["content"])

    def test_choose_max_iterations_by_intent(self):
        self.assertGreaterEqual(choose_max_iterations("comparison"), choose_max_iterations("chit_chat"))

    def test_user_facing_error_message_for_rate_limit(self):
        msg = user_facing_error_message(Exception("Error code: 429 - tokens per min"))
        self.assertIn("grænse", msg.lower())


if __name__ == "__main__":
    unittest.main()
