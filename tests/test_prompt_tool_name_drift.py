"""AG-01: Drift guard — prompts must only name tools the v2 selector can serve.

Background: the four action-critical _STAGE_HINTS instructed the model to call
the legacy search_courses/filter_courses/compare_courses/get_course_details
tools, which the futurematch-tools-v2 selector never puts on the menu — the
prompt contradicted SYSTEM_CORE exactly where it mattered most. This guard
extracts every tool-name-shaped token from _STAGE_HINTS, _GOLD_STANDARD_EXAMPLES
and SYSTEM_CORE and asserts:

  (a) every mentioned tool exists in the selectable universe
      (app1.tools OPENAI_TOOLS + PROFILE_TOOLS), and
  (b) no mentioned tool is tagged 'legacy_search' in the registry metadata
      (those schemas still exist for backwards compat but are never selected).

Also covers the rejection-context regression: a rejection following an executed
catalog_search must repopulate the 'Søgning: …' line in the negative-context
block (AG-01 part 3 — session-scoped LAST_SEARCH_QUERIES capture).

Fully offline: no OPENAI_API_KEY, no MySQL.
"""
import json
import os
import re
import sys
import unittest

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

from ai_tool_registry import get_tool_meta  # noqa: E402
from app1.tools import OPENAI_TOOLS, PROFILE_TOOLS  # noqa: E402
import app1.agent as agent_mod  # noqa: E402


# Tool-name-shaped token: lowercase snake_case with at least one underscore.
# Uppercase markers (UNTRUSTED_DATA, SAMTALEOVERSIGT) deliberately don't match.
_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")


def _all_tool_schemas():
    return list(OPENAI_TOOLS) + list(PROFILE_TOOLS)


def _schema_tool_names():
    return {t["function"]["name"] for t in _all_tool_schemas()}


def _collect_property_names(schema, acc):
    """Recursively collect parameter property names from a JSON schema."""
    if not isinstance(schema, dict):
        return
    for key, sub in (schema.get("properties") or {}).items():
        acc.add(key)
        _collect_property_names(sub, acc)
    items = schema.get("items")
    if isinstance(items, dict):
        _collect_property_names(items, acc)


def _argument_names():
    """Every parameter name any tool accepts — legitimate snake_case tokens
    in schema-accurate prompt examples like catalog_search(price_max=5000)."""
    acc = set()
    for tool in _all_tool_schemas():
        _collect_property_names(tool["function"].get("parameters") or {}, acc)
    return acc


def _legacy_tool_names():
    return {
        name for name in _schema_tool_names()
        if "legacy_search" in (get_tool_meta(name, "employee").toolset_tags or ())
    }


def _prompt_sources():
    """The prompt surfaces the drift guard covers: {label: text}."""
    sources = {f"_STAGE_HINTS[{stage}]": hint
               for stage, hint in agent_mod._STAGE_HINTS.items()}
    sources["_GOLD_STANDARD_EXAMPLES"] = agent_mod._GOLD_STANDARD_EXAMPLES
    sources["SYSTEM_CORE"] = agent_mod.SYSTEM_CORE
    return sources


class PromptToolNameDriftTests(unittest.TestCase):
    """Every tool-shaped token in the prompts must be a real, non-legacy tool."""

    def test_legacy_search_tools_exist_as_guard_fixture(self):
        # Sanity: the legacy schemas are still registered (backwards compat),
        # so assertion (b) below is a real check, not vacuously true.
        legacy = _legacy_tool_names()
        self.assertTrue(
            {"search_courses", "filter_courses", "compare_courses",
             "get_course_details"} <= legacy,
            f"Forventede legacy_search-værktøjer i registret, fandt: {legacy}",
        )

    def test_prompt_tool_mentions_exist_and_are_not_legacy(self):
        tool_names = _schema_tool_names()
        arg_names = _argument_names()
        legacy = _legacy_tool_names()
        failures = []
        for label, text in _prompt_sources().items():
            for token in _TOKEN_RE.findall(text or ""):
                if token in arg_names and token not in tool_names:
                    continue  # schema parameter name in an example call
                if token not in tool_names:
                    failures.append(
                        f"{label}: '{token}' findes ikke i OPENAI_TOOLS/PROFILE_TOOLS"
                    )
                elif token in legacy:
                    failures.append(
                        f"{label}: '{token}' er et legacy_search-værktøj som "
                        "v2-selectoren aldrig serverer — brug catalog_*"
                    )
        self.assertEqual(failures, [], "Prompt/tool-drift:\n" + "\n".join(failures))

    def test_action_stage_hints_name_the_v2_tools(self):
        # The four action-critical hints must point at the v2 toolset.
        hints = agent_mod._STAGE_HINTS
        self.assertIn("catalog_search", hints["searching"])
        self.assertIn("catalog_compare_products", hints["comparing"])
        self.assertIn("catalog_get_product", hints["ready_to_buy"])
        self.assertIn("check_course_readiness", hints["ready_to_buy"])
        self.assertIn("request_user_input", hints["profile_and_search"])
        self.assertIn("catalog_search", hints["profile_and_search"])


class RejectionSearchQueryTests(unittest.TestCase):
    """A rejection after a catalog_search must regain its 'Søgning: …' line."""

    SID = "test-ag01-rejection"

    def setUp(self):
        agent_mod.REJECTED_SEARCHES.pop(self.SID, None)
        agent_mod.LAST_SEARCH_QUERIES.pop(self.SID, None)
        agent_mod.SHOWN_PRODUCTS.pop(self.SID, None)

    tearDown = setUp

    def test_remember_search_query_captures_catalog_search(self):
        agent_mod._remember_search_query(
            self.SID, "catalog_search", {"query": "ledelse", "price_max": 5000})
        self.assertEqual(agent_mod.LAST_SEARCH_QUERIES.get(self.SID), "ledelse")

    def test_remember_search_query_ignores_non_search_tools(self):
        agent_mod._remember_search_query(
            self.SID, "catalog_get_product", {"handle": "itil-foundation"})
        agent_mod._remember_search_query(self.SID, "catalog_search", {})
        agent_mod._remember_search_query(self.SID, "catalog_search", {"query": "  "})
        self.assertNotIn(self.SID, agent_mod.LAST_SEARCH_QUERIES)

    def test_rejection_after_catalog_search_has_soegning_line(self):
        # Turn 1: the agent executed a catalog_search (capture seam from the
        # tool-result loop in the SSE generator).
        agent_mod._remember_search_query(
            self.SID, "catalog_search", {"query": "projektledelse"})
        # Turn 2: the user rejects — conversation memory holds no tool_calls
        # messages, so the session capture is the only source of the query.
        agent_mod._track_rejection(self.SID, "nej det var slet ikke det jeg mente", [])
        ctx = agent_mod._build_rejection_context(self.SID)
        self.assertIsNotNone(ctx)
        self.assertIn('Søgning: "projektledelse"', ctx["content"])
        self.assertIn("nej det var slet ikke det jeg mente", ctx["content"])

    def test_compare_call_never_blanks_a_captured_search_query(self):
        # catalog_compare_products carries no 'query' argument — a comparison
        # after the search must not overwrite the captured query with ''.
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "function": {
                        "name": "catalog_search",
                        "arguments": json.dumps({"query": "ledelse"}),
                    },
                }],
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_2",
                    "function": {
                        "name": "catalog_compare_products",
                        "arguments": json.dumps({"handles": ["a", "b"]}),
                    },
                }],
            },
        ]
        agent_mod._track_rejection(self.SID, "for dyrt begge to", messages)
        ctx = agent_mod._build_rejection_context(self.SID)
        self.assertIsNotNone(ctx)
        self.assertIn('Søgning: "ledelse"', ctx["content"])

    def test_rejection_reads_catalog_search_from_messages_too(self):
        # When the transcript DOES carry the assistant tool_calls message
        # (responses-runtime path post RT-01), the scan must match the v2 name.
        messages = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_1",
                "function": {
                    "name": "catalog_search",
                    "arguments": json.dumps({"query": "excel"}),
                },
            }],
        }]
        agent_mod._track_rejection(self.SID, "ikke relevant", messages)
        ctx = agent_mod._build_rejection_context(self.SID)
        self.assertIsNotNone(ctx)
        self.assertIn('Søgning: "excel"', ctx["content"])


if __name__ == "__main__":
    unittest.main()
