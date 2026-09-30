"""N-5.5: the provider fallback must not replay a tool loop that already changed data."""

import os
import types
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

import ai_provider  # noqa: E402
import ai_runtime  # noqa: E402


def _call(name):
    return types.SimpleNamespace(name=name, function=types.SimpleNamespace(name=name), arguments={})


def _run(tool_name, side_effect):
    """Claude's loop runs one tool then dies; returns (outcome, run_chat_agent mock)."""
    def anthropic(**kw):
        kw["tool_executor"](_call(tool_name))
        raise RuntimeError("anthropic went down mid-loop")

    chat = mock.Mock(return_value=types.SimpleNamespace(fallback_reason="", runtime_path=""))
    calls = []
    executor = lambda tc, *a, **k: calls.append(tc.name) or "{}"      # noqa: E731
    with mock.patch.object(ai_provider, "provider", return_value=ai_provider.PROVIDER_ANTHROPIC), \
            mock.patch("ai_provider_anthropic.run_anthropic_agent", side_effect=anthropic), \
            mock.patch("ai_provider_anthropic.is_permanent_request_error", return_value=False), \
            mock.patch.object(ai_runtime, "run_chat_agent", chat), \
            mock.patch.object(ai_runtime, "tool_display_metadata", return_value={"side_effect": side_effect}):
        try:
            out = ai_runtime.run_agent_with_fallback(
                messages=[], tools=[], tool_executor=executor, username="u", session_id="s")
        except RuntimeError as exc:
            out = exc
    return out, chat, calls


class FallbackGuardTests(unittest.TestCase):
    def test_read_only_loop_still_falls_back(self):
        out, chat, _ = _run("catalog_search", side_effect=False)
        chat.assert_called_once()
        self.assertEqual(out.runtime_path, "anthropic-openai-fallback")

    def test_no_replay_after_a_side_effect_tool_ran(self):
        out, chat, calls = _run("save_profile_item", side_effect=True)
        chat.assert_not_called()
        self.assertIsInstance(out, RuntimeError)
        self.assertEqual(calls, ["save_profile_item"])           # ran exactly once

    def test_tooler2_is_ga_with_explicit_opt_out(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_TOOLER2", None)
            self.assertTrue(ai_runtime.ai_tooler2_enabled())
        with mock.patch.dict(os.environ, {"AI_TOOLER2": "off"}):
            self.assertFalse(ai_runtime.ai_tooler2_enabled())


if __name__ == "__main__":
    unittest.main()
