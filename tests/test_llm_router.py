"""Unit tests for the LLM-as-router for ambiguous intents (item #2).

No live API: the gpt-4o-mini classification call is mocked via a fake OpenAI
client (mirrors the fakes in test_ai_runtime.py). These assert:
  (a) the router only fires on the ambiguous "discovery" catch-all and is
      skipped for a confident regex intent,
  (b) a valid label refines the intent + changes tool/model selection,
  (c) an error/timeout/garbage response falls back to the regex result,
  (d) AI_LLM_ROUTER=0 disables it.
"""
import os
import unittest
from unittest.mock import patch

import ai_runtime
from ai_runtime import (
    ROUTER_INTENT_ENUM,
    choose_turn_model,
    classify_intent_llm,
    fast_model,
    llm_router_enabled,
    main_model,
)
from ai_tool_registry import get_employee_tool_selection, tool_name


def _make_resp(content):
    """Build a minimal object shaped like an OpenAI chat completion response."""
    message = type("Msg", (), {"content": content})()
    choice = type("Choice", (), {"message": message})()
    return type("ChatResp", (), {"choices": [choice]})()


class _FakeChatCompletions:
    """Records calls; returns a queued response, or raises a queued Exception."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return _make_resp(item)


class _FakeClient:
    """Stand-in for the shared OpenAI client; supports with_options(timeout=...)."""

    def __init__(self, completions):
        self._completions = completions
        self.chat = type("Chat", (), {"completions": completions})()

    def with_options(self, **kwargs):
        # Per-request override returns a client that shares the same completions
        # object so the test can inspect the recorded call (model, max_tokens, ...).
        return self


def _install_fake(outputs):
    completions = _FakeChatCompletions(outputs)
    return _FakeClient(completions), completions


class LLMRouterCoreTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("AI_LLM_ROUTER", None)
        ai_runtime._ROUTER_CACHE.clear()

    def tearDown(self):
        os.environ.pop("AI_LLM_ROUTER", None)
        ai_runtime._ROUTER_CACHE.clear()

    # (d) Env gate -----------------------------------------------------------
    def test_default_enabled(self):
        self.assertTrue(llm_router_enabled())

    def test_disabled_with_zero(self):
        with patch.dict(os.environ, {"AI_LLM_ROUTER": "0"}):
            self.assertFalse(llm_router_enabled())

    def test_disabled_skips_api_and_returns_fallback(self):
        client, completions = _install_fake(["comparison"])
        with patch.dict(os.environ, {"AI_LLM_ROUTER": "0"}), \
                patch("ai_runtime._openai_client", return_value=client):
            out = classify_intent_llm("hvad er forskellen?", fallback="discovery")
        self.assertEqual(out, "discovery")
        self.assertEqual(completions.calls, [])  # no API round-trip when disabled

    # (b) Valid label refines the intent ------------------------------------
    def test_valid_label_refines_intent(self):
        client, completions = _install_fake(["comparison"])
        with patch("ai_runtime._openai_client", return_value=client):
            out = classify_intent_llm("hvad passer bedst til mig?", fallback="discovery")
        self.assertEqual(out, "comparison")
        # Cheap + bounded: gpt-4o-mini, tiny max_tokens.
        self.assertEqual(completions.calls[0]["model"], fast_model())
        self.assertLessEqual(completions.calls[0]["max_tokens"], 8)

    def test_label_with_extra_prose_is_parsed(self):
        client, _ = _install_fake(["Intent: skill_gap\n"])
        with patch("ai_runtime._openai_client", return_value=client):
            out = classify_intent_llm("hvad mangler jeg?", fallback="discovery")
        self.assertEqual(out, "skill_gap")

    def test_all_enum_labels_round_trip(self):
        for label in ROUTER_INTENT_ENUM:
            ai_runtime._ROUTER_CACHE.clear()
            client, _ = _install_fake([label])
            with patch("ai_runtime._openai_client", return_value=client):
                out = classify_intent_llm("noget tvetydigt " + label, fallback="discovery")
            self.assertEqual(out, label)

    # (c) Error / timeout / garbage -> fallback -----------------------------
    def test_api_error_falls_back(self):
        client, _ = _install_fake([RuntimeError("boom")])
        with patch("ai_runtime._openai_client", return_value=client):
            out = classify_intent_llm("noget", fallback="discovery")
        self.assertEqual(out, "discovery")

    def test_timeout_falls_back(self):
        client, _ = _install_fake([TimeoutError("timed out")])
        with patch("ai_runtime._openai_client", return_value=client):
            out = classify_intent_llm("noget", fallback="discovery")
        self.assertEqual(out, "discovery")

    def test_garbage_label_falls_back(self):
        client, _ = _install_fake(["totally-not-an-intent"])
        with patch("ai_runtime._openai_client", return_value=client):
            out = classify_intent_llm("noget", fallback="discovery")
        self.assertEqual(out, "discovery")

    def test_empty_response_falls_back(self):
        client, _ = _install_fake([""])
        with patch("ai_runtime._openai_client", return_value=client):
            out = classify_intent_llm("noget", fallback="discovery")
        self.assertEqual(out, "discovery")

    def test_empty_query_skips_api(self):
        client, completions = _install_fake(["comparison"])
        with patch("ai_runtime._openai_client", return_value=client):
            out = classify_intent_llm("   ", fallback="discovery")
        self.assertEqual(out, "discovery")
        self.assertEqual(completions.calls, [])

    # Cache: same text within a session does not re-call the API ------------
    def test_cache_avoids_repeat_calls(self):
        client, completions = _install_fake(["comparison"])  # only ONE response queued
        with patch("ai_runtime._openai_client", return_value=client):
            first = classify_intent_llm("samme tekst", fallback="discovery")
            second = classify_intent_llm("samme tekst", fallback="discovery")
        self.assertEqual(first, "comparison")
        self.assertEqual(second, "comparison")
        self.assertEqual(len(completions.calls), 1)  # second served from cache


class LLMRouterGateTests(unittest.TestCase):
    """(a) The router only fires on the ambiguous 'discovery' catch-all.

    Exercises the real regex classifier + the same gate the agent uses, without
    importing the whole Flask agent module (which pulls in heavy deps). The gate
    logic mirrors handle_agentic_ask: route ONLY when regex_intent == 'discovery'.
    """

    def setUp(self):
        ai_runtime._ROUTER_CACHE.clear()

    def _route_like_agent(self, regex_intent, user_query, router_label):
        """Replicate the agent's gate: only call the router on 'discovery'."""
        client, completions = _install_fake([router_label])
        with patch("ai_runtime._openai_client", return_value=client):
            if regex_intent == "discovery" and llm_router_enabled():
                refined = classify_intent_llm(user_query, fallback=regex_intent)
            else:
                refined = regex_intent
        return refined, completions

    def test_confident_intent_skips_router(self):
        from app1.agent import _classify_intent_local
        # A clear comparison turn (2 shown courses) -> confident regex intent.
        regex_intent = _classify_intent_local(
            "Hvad er forskellen på de to første?", messages=[], shown_count=2
        )
        self.assertEqual(regex_intent, "comparison")
        refined, completions = self._route_like_agent(
            regex_intent, "Hvad er forskellen på de to første?", "skill_gap"
        )
        # Router never fired; the confident regex intent is preserved.
        self.assertEqual(refined, "comparison")
        self.assertEqual(completions.calls, [])

    def test_ambiguous_discovery_fires_router(self):
        from app1.agent import _classify_intent_local
        # A bare topic keyword with no context -> the ambiguous "discovery" catch-all.
        regex_intent = _classify_intent_local("ledelse", messages=[], shown_count=0)
        self.assertEqual(regex_intent, "discovery")
        refined, completions = self._route_like_agent(regex_intent, "ledelse", "learning_path")
        # Router fired and refined the catch-all into a real intent.
        self.assertEqual(refined, "learning_path")
        self.assertEqual(len(completions.calls), 1)


class LLMRouterDownstreamTests(unittest.TestCase):
    """(b) The refined intent actually changes tool + model selection."""

    def test_model_tier_changes_with_refined_intent(self):
        # In balanced mode (default): "discovery" -> fast model; the refined
        # synthesis intents -> main model. So the router flips the tier.
        with patch.dict(os.environ, {"AI_MODEL_ROUTING": "balanced"}):
            self.assertEqual(
                choose_turn_model(intent="discovery", tool_count=1, token_estimate=2000),
                fast_model(),
            )
            for refined in ("comparison", "learning_path", "skill_gap", "profile_and_search"):
                self.assertEqual(
                    choose_turn_model(intent=refined, tool_count=1, token_estimate=2000),
                    main_model(),
                    f"{refined} should route to the main model",
                )

    def _tool_names(self, intent):
        _tools, meta = get_employee_tool_selection(
            logged_in=True,
            company_id=1,
            intent=intent,
            user_query="ledelse",  # neutral query: differences come from the intent
            shown_count=0,
        )
        return set(meta["tool_names"])

    def test_comparison_adds_compare_tool(self):
        discovery_tools = self._tool_names("discovery")
        comparison_tools = self._tool_names("comparison")
        self.assertNotIn("catalog_compare_products", discovery_tools)
        self.assertIn("catalog_compare_products", comparison_tools)

    def test_skill_gap_adds_gap_tool(self):
        self.assertNotIn("analyze_skill_gaps", self._tool_names("discovery"))
        self.assertIn("analyze_skill_gaps", self._tool_names("skill_gap"))

    def test_learning_path_adds_path_tools(self):
        path_tools = self._tool_names("learning_path")
        self.assertNotIn("suggest_learning_path", self._tool_names("discovery"))
        self.assertIn("suggest_learning_path", path_tools)
        self.assertIn("recommend_for_profile", path_tools)

    def test_profile_and_search_adds_profile_tools(self):
        ps_tools = self._tool_names("profile_and_search")
        self.assertIn("update_user_profile", ps_tools)
        self.assertIn("catalog_search", ps_tools)


if __name__ == "__main__":
    unittest.main()
