"""AI Tooler 2 Phase 4: tool-description trim + Danish decompounding.

Both features are env-gated and default OFF, so these tests toggle the flags
explicitly and assert the default-off path is unchanged.
"""
import unittest
from unittest import mock

import ai_tool_registry as reg
import grounding


LONG_DESC = (
    "Search the course catalog using a hybrid semantic plus keyword query. "
    "This tool supports filters for price, city, vendor, category and format, and "
    "returns ranked results with full metadata for each course in the catalog, "
    "including vendor reputation, upcoming dates, seat availability and pricing tiers."
)


class ToolDescriptionTrimTests(unittest.TestCase):
    def _tool(self):
        return {
            "type": "function",
            "function": {
                "name": "catalog_search",
                "description": LONG_DESC,
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
                "strict": True,
            },
        }

    def test_default_off_keeps_full_description(self):
        with mock.patch.dict("os.environ", {"AI_TRIM_TOOL_DESCRIPTIONS": ""}):
            out = reg._normalize_chat_tool(self._tool())
        self.assertEqual(out["function"]["description"], LONG_DESC)

    def test_trim_clips_to_first_sentence(self):
        with mock.patch.dict("os.environ", {"AI_TRIM_TOOL_DESCRIPTIONS": "on"}):
            out = reg._normalize_chat_tool(self._tool())
            resp = reg.to_responses_tool(self._tool())
        trimmed = out["function"]["description"]
        self.assertLess(len(trimmed), len(LONG_DESC))
        self.assertTrue(trimmed.startswith("Search the course catalog"))
        # responses-tool path trims identically
        self.assertEqual(resp["description"], trimmed)

    def test_short_description_untouched(self):
        short = {"type": "function", "function": {"name": "x", "description": "Short.", "parameters": {"type": "object", "properties": {}}}}
        with mock.patch.dict("os.environ", {"AI_TRIM_TOOL_DESCRIPTIONS": "on"}):
            out = reg._normalize_chat_tool(short)
        self.assertEqual(out["function"]["description"], "Short.")


class DecompoundTests(unittest.TestCase):
    def test_splits_known_compound(self):
        parts = grounding.decompound_da("erhvervserfaring")
        self.assertIn("erhvervserfaring", parts)
        self.assertIn("erfaring", parts)
        self.assertIn("erhverv", parts)

    def test_linking_s_stripped(self):
        parts = grounding.decompound_da("lederuddannelse")
        self.assertIn("uddannelse", parts)
        self.assertIn("leder", parts)

    def test_non_compound_returns_self(self):
        self.assertEqual(grounding.decompound_da("erfaring"), ["erfaring"])
        self.assertEqual(grounding.decompound_da("kort"), ["kort"])

    def test_never_raises(self):
        self.assertEqual(grounding.decompound_da(""), [])
        self.assertEqual(grounding.decompound_da(None), [])

    def test_title_match_uses_decompound_when_enabled(self):
        # A claim word "erhvervserfaring" should match a catalog title that only
        # contains "erfaring" once decompounding is on.
        title_keys = ["praktisk erfaring i salg"]
        with mock.patch.dict("os.environ", {"AI_GROUNDING_DECOMPOUND": ""}):
            off = grounding._matches_known_title("erhvervserfaring i salg", title_keys)
        with mock.patch.dict("os.environ", {"AI_GROUNDING_DECOMPOUND": "on"}):
            on = grounding._matches_known_title("erhvervserfaring i salg", title_keys)
        # With decompounding on, the compound word now contributes a hit.
        self.assertTrue(on)
        self.assertFalse(off)


if __name__ == "__main__":
    unittest.main()
